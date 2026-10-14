"""
Competitor Price Watch — DispatchKit Agent

Watches competitor product pages around the clock and reads the live price
from each one:

- scan: every CHECK_MINUTES, each page runs through an extraction ladder
  (Shopify product JSON, schema.org JSON-LD, price meta tags, price-marked
  markup, then a budget-capped LLM fallback). The first scan seeds the
  baseline; every later change is recorded, and moves past ALERT_PCT alert
  immediately with AI context. Three consecutive extraction failures surface
  as a scrape_issue instead of failing silently.
- report: on a fixed cadence (or on demand via chat), Claude compiles the
  window's movement into a pricing report.

Findings are emitted as structured records: kind="price_change",
kind="price_alert", kind="scrape_issue", kind="report". Record score is the
absolute percent move, so NOTIFY_MIN_SCORE acts as a percent floor.
"""

import asyncio
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent / "lib"))

from dispatchkit import Agent, llm, phase, util

import extract
from report import context_prompt, report_prompt, stub_context, stub_report

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

MAX_HISTORY = 500
FAILURES_BEFORE_ISSUE = 3


def _parse_targets(raw: str) -> list:
    """PRODUCT_URLS format: "https://a.com/products/x|Acme widget, https://b.com/p/y"
    — the optional part after | is a display label."""
    targets = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        url, _, label = chunk.partition("|")
        url = url.strip()
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        targets.append({"url": url, "label": label.strip() or url})
    return targets


class PriceWatchAgent(Agent):
    def __init__(self):
        super().__init__()
        self.targets: list = []
        self.check_minutes: float = 60.0
        self.alert_pct: float = 5.0
        self.max_llm_extractions: int = 10
        self.report_every_hours: float = 168.0

    async def setup(self):
        self.targets = _parse_targets(os.environ.get("PRODUCT_URLS", ""))
        extra = self.state.get("extra_targets", [])
        known = {t["url"] for t in self.targets}
        self.targets += [t for t in extra if t["url"] not in known]

        self.check_minutes = float(os.environ.get("CHECK_MINUTES", "60"))
        self.alert_pct = float(os.environ.get("ALERT_PCT", "5"))
        self.max_llm_extractions = int(os.environ.get("MAX_LLM_EXTRACTIONS_PER_DAY", "10"))
        self.report_every_hours = float(os.environ.get("REPORT_EVERY_HOURS", "168"))

        # A missing mark reads as due, which would fire the cadence report on
        # first boot before any scan; start the report clock now instead.
        if "last_report" not in self.state:
            util.mark(self.state, "last_report")
            self.save_state(self.state)

        logger.info(
            f"Price watch ready | {len(self.targets)} product(s) | check every "
            f"{self.check_minutes}m | alert at {self.alert_pct}% | "
            f"report every {self.report_every_hours}h"
        )

    # --- phases ---

    @phase("report", when=lambda a: bool(a.targets) and bool(a.state.get("prices")) and (
        a.state.get("report_requested") or util.due(a.state, "last_report", a.report_every_hours)))
    async def report(self) -> dict:
        state = self.state
        rows = self._rows(self.report_every_hours)
        if not rows:
            state["report_requested"] = False
            return {"result": "no_data_yet"}

        system, prompt = report_prompt(rows, self.report_every_hours)
        body = await self.ask_llm(prompt, system=system, max_tokens=2000, stub=stub_report(rows))
        movers = [r for r in rows if r["window_pct"]]
        record = self.record(
            "report",
            payload={"report_md": body, "products": rows, "window_hours": self.report_every_hours},
            title=f"Pricing report — {len(rows)} product(s), {len(movers)} moved this window",
            tags=["report"],
        )
        await self.emit("price.report", {"record_id": record["id"], "products": len(rows)})

        state["report_requested"] = False
        logger.info(f"Report written for {len(rows)} product(s)")
        return {"result": "report", "products": len(rows)}

    @phase("scan", every_hours=lambda a: a.check_minutes / 60, when=lambda a: bool(a.targets))
    async def scan(self) -> dict:
        state = self.state
        prices = state.setdefault("prices", {})
        changes = 0
        alerts = 0
        baselines = []

        for target in self.targets:
            url = target["url"]
            entry = prices.setdefault(url, {"label": target["label"]})
            entry["label"] = target["label"]

            try:
                result, page_text = await asyncio.to_thread(extract.extract, url, entry.get("method"))
            except extract.ExtractError as e:
                await self._record_failure(entry, url, f"page unreachable: {e}")
                continue

            if result is None and page_text:
                result = await self._llm_extract(url, page_text)
            if result is None:
                await self._record_failure(entry, url, "no price found on the page")
                continue

            entry["consecutive_failures"] = 0
            if result["method"] in extract.RUNGS:
                entry["method"] = result["method"]
            if result.get("currency"):
                entry["currency"] = result["currency"]

            new_price = round(float(result["price"]), 2)
            old_price = entry.get("last")
            history = entry.setdefault("history", [])
            if old_price is None:
                logger.info(f"Baseline for {entry['label']}: {new_price} via {result['method']}")
                baselines.append({"label": entry["label"], "url": url, "price": new_price,
                                  "currency": entry.get("currency", ""), "method": result["method"]})
            elif new_price != old_price:
                changes += 1
                alerts += await self._record_change(entry, url, old_price, new_price)
            entry["last"] = new_price
            if not history or history[-1][1] != new_price:
                history.append([util.now_iso(), new_price])
                del history[:-MAX_HISTORY]

        if baselines:
            self.record(
                "baseline",
                payload={"products": baselines},
                title=f"Watching {len(baselines)} product(s) — "
                      + ", ".join(f"{b['label']} at {b['price']}" for b in baselines[:3])
                      + ("…" if len(baselines) > 3 else ""),
                tags=["baseline"],
            )
        if changes:
            logger.info(f"Scan: {changes} price change(s), {alerts} alert(s)")
        return {"result": "scanned", "targets": len(self.targets), "changes": changes, "alerts": alerts}

    async def _llm_extract(self, url: str, page_text: str) -> Optional[dict]:
        today = datetime.now(timezone.utc).date().isoformat()
        counter = self.state.setdefault("llm_extractions_today", {})
        if counter.get("date") != today:
            counter.update(date=today, count=0)
        if counter["count"] >= self.max_llm_extractions:
            return None

        system = (
            "You read the visible text of a product page and return the current "
            "price of the main product as JSON: {\"price\": <number>, "
            "\"currency\": \"<ISO code or empty>\"}. If no price is present, "
            "return {\"price\": null}."
        )
        counter["count"] += 1
        answer = await asyncio.to_thread(
            llm.ask_json, f"Page: {url}\n\n{page_text}",
            system=system, max_tokens=200, data_dir=self.config.data_dir, stub={},
        )
        price = answer.get("price") if isinstance(answer, dict) else None
        if not isinstance(price, (int, float)) or price <= 0:
            return None
        return {"price": float(price), "currency": str(answer.get("currency") or ""), "method": "llm"}

    async def _record_failure(self, entry: dict, url: str, reason: str):
        entry["consecutive_failures"] = entry.get("consecutive_failures", 0) + 1
        if entry["consecutive_failures"] != FAILURES_BEFORE_ISSUE:
            return
        today = datetime.now(timezone.utc).date().isoformat()
        key = f"{url}:issue:{today}"
        if self.has_recent_record(key, within_hours=24):
            return
        self.record(
            "scrape_issue",
            payload={"url": url, "label": entry["label"], "reason": reason,
                     "consecutive_failures": entry["consecutive_failures"]},
            title=f"Can't read a price for {entry['label']} — {reason}",
            tags=["issue"],
            dedupe_key=key,
        )
        logger.warning(f"SCRAPE ISSUE {url}: {reason}")

    async def _record_change(self, entry: dict, url: str, old_price: float, new_price: float) -> int:
        pct = round((new_price - old_price) / old_price * 100, 2)
        is_alert = abs(pct) >= self.alert_pct
        kind = "price_alert" if is_alert else "price_change"
        key = f"{url}:{old_price}->{new_price}"
        if self.has_recent_record(key, within_hours=24):
            return 0

        payload = {
            "label": entry["label"], "url": url, "old_price": old_price,
            "new_price": new_price, "pct_change": pct,
            "currency": entry.get("currency", ""), "method": entry.get("method", "llm"),
        }
        if is_alert:
            system, prompt = context_prompt(entry["label"], url, old_price, new_price, pct)
            payload["context_md"] = await self.ask_llm(prompt, system=system, max_tokens=500,
                                                       stub=stub_context())

        direction = "cut" if new_price < old_price else "raised"
        record = self.record(
            kind,
            payload=payload,
            title=f"{entry['label']}: price {direction} {pct:+.1f}% to {new_price}",
            score=abs(pct),
            tags=[kind, entry["label"]],
            dedupe_key=key,
        )
        await self.emit("price.alert" if is_alert else "price.change",
                        {"record_id": record["id"], "url": url, "pct_change": pct})
        if is_alert:
            logger.warning(f"PRICE ALERT {entry['label']}: {old_price} -> {new_price} ({pct:+.1f}%)")
        return 1 if is_alert else 0

    def _rows(self, window_hours: Optional[float] = None) -> list:
        prices = self.state.get("prices", {})
        rows = []
        for target in self.targets:
            entry = prices.get(target["url"])
            if not entry or entry.get("last") is None:
                continue
            row = {"label": entry["label"], "url": target["url"], "last": entry["last"],
                   "currency": entry.get("currency", ""), "window_pct": None}
            history = entry.get("history", [])
            if window_hours and len(history) >= 2:
                cutoff = datetime.now(timezone.utc).timestamp() - window_hours * 3600
                window = [h for h in history
                          if datetime.fromisoformat(h[0].rstrip("Z")).timestamp() >= cutoff]
                base = window[0][1] if window else history[-1][1]
                if base:
                    row["window_pct"] = round((entry["last"] - base) / base * 100, 2)
            rows.append(row)
        return rows

    # --- chat ---

    def _price_table(self) -> str:
        rows = self._rows(self.report_every_hours)
        if not rows:
            return "No prices captured yet — the first scan seeds the baseline."
        lines = ["**Current prices**"]
        for r in rows:
            currency = f" {r['currency']}" if r["currency"] else ""
            move = f" ({r['window_pct']:+.1f}% this window)" if r["window_pct"] is not None else ""
            lines.append(f"- {r['label']}: {r['last']}{currency}{move}")
        return "\n".join(lines)

    async def on_command(self, command: dict):
        text = command.get("text", "").strip()
        state = self.state
        lower = text.lower()

        if lower == "prices":
            return self._price_table()

        if lower == "report":
            if not state.get("prices"):
                return "No prices captured yet — I'll report once the first scans land."
            state["report_requested"] = True
            self.save_state(state)
            return "Compiling a pricing report — it will appear in Records within a minute."

        if lower.startswith("watch "):
            spec = text[6:].strip()
            if not spec:
                return "Give me a product URL, e.g. `watch https://competitor.com/products/widget|Their widget`."
            new = _parse_targets(spec)
            extras = state.setdefault("extra_targets", [])
            added = []
            for target in new:
                if all(t["url"] != target["url"] for t in self.targets):
                    self.targets.append(target)
                    extras.append(target)
                    added.append(target["label"])
            self.save_state(state)
            if not added:
                return "Already watching that."
            return f"Now watching **{', '.join(added)}** ({len(self.targets)} products total)."

        if lower.startswith("unwatch "):
            url = text[8:].strip()
            before = len(self.targets)
            self.targets = [t for t in self.targets if t["url"] != url and t["url"] != f"https://{url}"]
            state["extra_targets"] = [t for t in state.get("extra_targets", [])
                                      if t["url"] != url and t["url"] != f"https://{url}"]
            state.get("prices", {}).pop(url, None)
            state.get("prices", {}).pop(f"https://{url}", None)
            self.save_state(state)
            if len(self.targets) == before:
                return f"I wasn't watching {url}."
            return f"Stopped watching **{url}** ({len(self.targets)} products remain)."

        if lower == "status":
            if not self.targets:
                return "No products configured yet. Add one with `watch https://competitor.com/products/widget`."
            prices = state.get("prices", {})
            lines = ["**Status**"]
            for target in self.targets:
                entry = prices.get(target["url"], {})
                if entry.get("last") is not None:
                    fails = entry.get("consecutive_failures", 0)
                    trouble = f", {fails} consecutive failures" if fails else ""
                    lines.append(f"- {target['label']}: {entry['last']} via {entry.get('method', 'llm')}{trouble}")
                else:
                    lines.append(f"- {target['label']}: no price yet")
            lines.append(f"- Last scan: {state.get('last_scan', 'never')}")
            lines.append(f"- Last report: {state.get('last_report', 'never')}")
            return "\n".join(lines)

        return await super().on_command(command)


if __name__ == "__main__":
    agent = PriceWatchAgent()
    asyncio.run(agent.run())
