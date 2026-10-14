"""
Review Radar — DispatchKit Agent

Watches the business's Google reviews around the clock:

- resolve: business lookups from LOCATIONS ("Joe's Pizza, Brooklyn NY", or a
  literal "place_id:..." entry) are resolved once via Places Text Search and
  cached
- scan: every SCAN_MINUTES, Place Details is polled per location; every new
  review is recorded with a drafted owner reply, reviews at or below
  ALERT_BELOW_STARS and drops in the aggregate rating alert immediately
- report: on a fixed cadence (or on demand via chat), Claude compiles the
  window's activity into a reputation report

Findings are emitted as structured records: kind="review", kind="alert",
kind="rating_drop", kind="report". Needs one customer key
(GOOGLE_PLACES_API_KEY); without it the agent boots, explains itself, and
stays idle.
"""

import asyncio
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "lib"))

from dispatchkit import Agent, phase, util

import places
from report import reply_prompt, report_prompt, stub_reply, stub_report

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

MAX_SEEN_HASHES = 200
MAX_PENDING = 200
MAX_RESOLVE_ATTEMPTS = 3
RATING_DROP_MIN = 0.1


def _parse_locations(raw: str) -> list:
    """LOCATIONS is semicolon-separated because a single lookup usually
    contains a comma: "Joe's Pizza, Brooklyn NY; Mario's Deli, Queens NY"."""
    return [chunk.strip() for chunk in raw.split(";") if chunk.strip()]


class ReviewRadarAgent(Agent):
    def __init__(self):
        super().__init__()
        self.locations: list = []
        self.api_key: str = ""
        self.scan_minutes: float = 60.0
        self.alert_below_stars: int = 4
        self.reply_drafts: bool = True
        self.max_drafts_per_day: int = 10
        self.report_every_hours: float = 168.0

    async def setup(self):
        env_locations = _parse_locations(os.environ.get("LOCATIONS", ""))
        extra = self.state.get("extra_locations", [])
        self.locations = list(dict.fromkeys(env_locations + extra))

        self.api_key = os.environ.get("GOOGLE_PLACES_API_KEY", "").strip()
        self.scan_minutes = float(os.environ.get("SCAN_MINUTES", "60"))
        self.alert_below_stars = int(os.environ.get("ALERT_BELOW_STARS", "4"))
        self.reply_drafts = os.environ.get("REPLY_DRAFTS", "1") == "1"
        self.max_drafts_per_day = int(os.environ.get("MAX_DRAFTS_PER_DAY", "10"))
        self.report_every_hours = float(os.environ.get("REPORT_EVERY_HOURS", "168"))

        # A missing mark reads as due, which would fire the cadence report as
        # soon as the first review lands; start the report clock now instead.
        if "last_report" not in self.state:
            util.mark(self.state, "last_report")
            self.save_state(self.state)

        if not self.api_key:
            self.needs_setup(
                "GOOGLE_PLACES_API_KEY",
                "Paste a Google Places API key (console.cloud.google.com → enable Places API) "
                "and set LOCATIONS to your business name and city to start watching reviews.",
            )
        else:
            self.clear_needs_setup()
        logger.info(
            f"Review radar ready | {len(self.locations)} location(s) | scan every "
            f"{self.scan_minutes}m | alert at <= {self.alert_below_stars} stars | "
            f"report every {self.report_every_hours}h"
        )

    # --- location resolution ---

    def _ready(self) -> bool:
        return bool(self.api_key and self.locations)

    def _places_state(self) -> dict:
        return self.state.setdefault("places", {})

    def _unresolved(self) -> list:
        placed = self._places_state()
        return [
            loc for loc in self.locations
            if not placed.get(loc, {}).get("place_id")
            and placed.get(loc, {}).get("resolve_attempts", 0) < MAX_RESOLVE_ATTEMPTS
        ]

    def _resolved(self) -> list:
        placed = self._places_state()
        return [loc for loc in self.locations if placed.get(loc, {}).get("place_id")]

    @phase("resolve", when=lambda a: a._ready() and bool(a._unresolved()))
    async def resolve(self) -> dict:
        placed = self._places_state()
        resolved = 0
        for loc in self._unresolved():
            entry = placed.setdefault(loc, {})
            if loc.startswith("place_id:"):
                entry["place_id"] = loc[len("place_id:"):].strip()
                entry["name"] = entry["place_id"]
                resolved += 1
                continue
            try:
                found = await asyncio.to_thread(places.resolve, loc, self.api_key)
                entry.update(place_id=found["place_id"], name=found["name"])
                resolved += 1
                logger.info(f"Resolved '{loc}' -> {found['name']} ({found['place_id']})")
            except places.PlacesError as e:
                entry["resolve_attempts"] = entry.get("resolve_attempts", 0) + 1
                logger.warning(f"Resolve failed for '{loc}' (attempt {entry['resolve_attempts']}): {e}")
                if entry["resolve_attempts"] >= MAX_RESOLVE_ATTEMPTS:
                    self.record(
                        "resolve_failed",
                        payload={"location": loc, "error": str(e)},
                        title=f"Could not find '{loc}' on Google Maps — check the spelling or use place_id:...",
                        tags=["issue"],
                        dedupe_key=f"resolve:{loc}",
                    )
        return {"result": "resolved", "count": resolved}

    # --- phases ---

    @phase("report", when=lambda a: a._ready() and bool(a.state.get("report_requested") or (
        a.state.get("pending") and util.due(a.state, "last_report", a.report_every_hours))))
    async def report(self) -> dict:
        state = self.state
        stats = self._stats()
        pending = state.get("pending", [])
        if not stats:
            state["report_requested"] = False
            return {"result": "no_data_yet"}

        system, prompt = report_prompt(stats, pending, self.report_every_hours)
        body = await self.ask_llm(prompt, system=system, max_tokens=2000,
                                  stub=stub_report(stats, pending))
        record = self.record(
            "report",
            payload={"report_md": body, "places": stats, "new_reviews": len(pending),
                     "window_hours": self.report_every_hours},
            title=f"Reputation report — {len(pending)} new review(s) across {len(stats)} location(s)",
            tags=["report"],
        )
        await self.emit("review.report", {"record_id": record["id"], "new_reviews": len(pending)})

        for entry in self._places_state().values():
            entry["missed_reviews"] = 0
        state["pending"] = []
        state["report_requested"] = False
        logger.info(f"Report written: {len(pending)} new reviews")
        return {"result": "report", "new_reviews": len(pending)}

    @phase("scan", every_hours=lambda a: a.scan_minutes / 60, when=lambda a: a._ready() and bool(a._resolved()))
    async def scan(self) -> dict:
        state = self.state
        placed = self._places_state()
        new_reviews = 0
        alerts = 0

        for loc in self._resolved():
            entry = placed[loc]
            try:
                details = await asyncio.to_thread(places.fetch_details, entry["place_id"], self.api_key)
            except places.PlacesError as e:
                logger.warning(f"Details failed for {entry.get('name', loc)}: {e}")
                continue

            entry["name"] = details["name"]
            seen = entry.setdefault("seen_hashes", [])
            first_scan = not entry.get("scanned_once")
            fresh = [r for r in details["reviews"] if r["id"] not in seen]

            for review in fresh:
                seen.append(review["id"])
                if first_scan:
                    continue
                new_reviews += 1
                alerts += await self._record_review(entry, review)

            # The API exposes at most 5 reviews, so the count delta is the
            # honest "new reviews" number; the overflow is surfaced in reports.
            old_count = entry.get("review_count")
            if not first_scan and old_count is not None:
                delta = details["review_count"] - old_count
                visible = len(fresh)
                if delta > visible:
                    entry["missed_reviews"] = entry.get("missed_reviews", 0) + (delta - visible)

            old_rating = entry.get("rating")
            if not first_scan and old_rating and details["rating"] and \
                    old_rating - details["rating"] >= RATING_DROP_MIN:
                await self._record_rating_drop(entry, old_rating, details["rating"])

            entry["rating"] = details["rating"]
            entry["review_count"] = details["review_count"]
            entry["maps_url"] = details["maps_url"]
            entry["scanned_once"] = True
            del seen[:-MAX_SEEN_HASHES]

        if new_reviews:
            logger.info(f"Scan: {new_reviews} new review(s), {alerts} alert(s)")
        return {"result": "scanned", "new": new_reviews, "alerts": alerts}

    async def _record_review(self, entry: dict, review: dict) -> int:
        if self.has_recent_record(review["id"], within_hours=720):
            return 0
        is_alert = review["stars"] <= self.alert_below_stars
        kind = "alert" if is_alert else "review"

        draft = stub_reply()
        if self.reply_drafts and self._draft_budget_left():
            system, prompt = reply_prompt(entry["name"], review)
            draft = await self.ask_llm(prompt, system=system, max_tokens=400, stub=stub_reply())
            self._count_draft()

        record = self.record(
            kind,
            payload={
                "place": entry["name"],
                "author": review["author"],
                "stars": review["stars"],
                "text": review["text"],
                "published": review["published"],
                "url": review["url"],
                "reply_draft_md": draft,
            },
            title=(f"{'NEGATIVE ' if is_alert else ''}{review['stars']}/5 review of "
                   f"{entry['name']} by {review['author']}"),
            score=float((5 - review["stars"]) * 25),
            tags=[kind, entry["name"]],
            dedupe_key=review["id"],
        )
        pending = self.state.setdefault("pending", [])
        pending.append({
            "place": entry["name"], "author": review["author"], "stars": review["stars"],
            "text": review["text"], "published": review["published"],
        })
        del pending[:-MAX_PENDING]
        await self.emit("review.alert" if is_alert else "review.new",
                        {"record_id": record["id"], "place": entry["name"], "stars": review["stars"]})
        if is_alert:
            logger.warning(f"NEGATIVE review ({review['stars']}/5) for {entry['name']} by {review['author']}")
        return 1 if is_alert else 0

    async def _record_rating_drop(self, entry: dict, old_rating: float, new_rating: float):
        today = datetime.now(timezone.utc).date().isoformat()
        key = f"{entry['place_id']}:rating:{today}"
        if self.has_recent_record(key, within_hours=24):
            return
        drop = round(old_rating - new_rating, 2)
        self.record(
            "rating_drop",
            payload={"place": entry["name"], "old_rating": old_rating,
                     "new_rating": new_rating, "drop": drop, "url": entry.get("maps_url", "")},
            title=f"Rating drop: {entry['name']} fell {drop} to {new_rating}/5",
            score=float(min(100.0, drop * 100)),
            tags=["rating_drop", entry["name"]],
            dedupe_key=key,
        )
        logger.warning(f"RATING DROP {entry['name']}: {old_rating} -> {new_rating}")

    def _stats(self) -> list:
        placed = self._places_state()
        return [
            {
                "name": entry.get("name", loc),
                "rating": entry.get("rating", 0.0),
                "review_count": entry.get("review_count", 0),
                "missed_reviews": entry.get("missed_reviews", 0),
            }
            for loc, entry in ((x, placed[x]) for x in self._resolved())
        ]

    # --- LLM draft budget ---

    def _draft_budget_left(self) -> bool:
        today = datetime.now(timezone.utc).date().isoformat()
        drafts = self.state.get("drafts_today", {})
        return drafts.get("date") != today or drafts.get("count", 0) < self.max_drafts_per_day

    def _count_draft(self):
        today = datetime.now(timezone.utc).date().isoformat()
        drafts = self.state.setdefault("drafts_today", {})
        if drafts.get("date") != today:
            drafts.update(date=today, count=0)
        drafts["count"] = drafts.get("count", 0) + 1

    # --- chat ---

    async def on_command(self, command: dict):
        text = command.get("text", "").strip()
        state = self.state
        lower = text.lower()

        if lower == "report":
            state["report_requested"] = True
            self.save_state(state)
            return "Compiling a reputation report — it will appear in Records within a minute."

        if lower.startswith("watch "):
            loc = text[6:].strip()
            if not loc:
                return "Give me a business, e.g. `watch Joe's Pizza, Brooklyn NY`."
            extras = state.setdefault("extra_locations", [])
            if loc not in extras and loc not in self.locations:
                extras.append(loc)
                self.locations.append(loc)
                self.save_state(state)
            return f"Now watching **{loc}** ({len(self.locations)} locations total)."

        if lower.startswith("unwatch "):
            loc = text[8:].strip()
            before = len(self.locations)
            self.locations = [x for x in self.locations if x.lower() != loc.lower()]
            state["extra_locations"] = [x for x in state.get("extra_locations", [])
                                        if x.lower() != loc.lower()]
            for key in list(state.get("places", {})):
                if key.lower() == loc.lower():
                    state["places"].pop(key, None)
            self.save_state(state)
            if len(self.locations) == before:
                return f"I wasn't watching {loc}."
            return f"Stopped watching **{loc}** ({len(self.locations)} locations remain)."

        if lower == "status":
            if not self.api_key:
                return ("No Google Places API key configured yet — paste one into the "
                        "agent config and I'll start watching your reviews.")
            if not self.locations:
                return "No locations configured yet. Add one with `watch Joe's Pizza, Brooklyn NY`."
            lines = ["**Status**"]
            for s in self._stats():
                missed = f", {s['missed_reviews']} recent without visible text" if s["missed_reviews"] else ""
                lines.append(f"- {s['name']}: {s['rating']}/5 across {s['review_count']} reviews{missed}")
            unresolved = [l for l in self.locations if not state.get("places", {}).get(l, {}).get("place_id")]
            if unresolved:
                lines.append(f"- Still resolving: {', '.join(unresolved)}")
            lines.append(f"- Pending reviews for the next report: {len(state.get('pending', []))}")
            lines.append(f"- Last scan: {state.get('last_scan', 'never')}")
            lines.append(f"- Last report: {state.get('last_report', 'never')}")
            return "\n".join(lines)

        return await super().on_command(command)


if __name__ == "__main__":
    agent = ReviewRadarAgent()
    asyncio.run(agent.run())
