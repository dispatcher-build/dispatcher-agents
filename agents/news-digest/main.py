"""
AI News Digest — DispatchKit Agent

Watches configured RSS/Atom feeds around the clock:

- scan: fetch feeds on a short cadence, dedupe against seen items, queue new
  ones; items matching ALERT_KEYWORDS are surfaced immediately as alerts
- digest: on a fixed cadence (or on demand via chat), Claude compiles the
  queue into a themed executive brief with why-it-matters context and links

Findings are emitted as structured records: kind="alert" and kind="digest".
"""

import asyncio
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "lib"))

from dispatchkit import Agent, phase, util

import feeds
from digest import build_prompt, stub_digest

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

MAX_PENDING = 200
MAX_SEEN = 1000


class NewsDigestAgent(Agent):
    def __init__(self):
        super().__init__()
        self.feed_urls: list = []
        self.topic: str = ""
        self.scan_minutes: float = 30.0
        self.digest_every_hours: float = 24.0
        self.alert_keywords: list = []
        self.max_items: int = 40

    async def setup(self):
        env_feeds = [u.strip() for u in os.environ.get("FEEDS", "").split(",") if u.strip()]
        extra = self.state.get("extra_feeds", [])
        self.feed_urls = list(dict.fromkeys(env_feeds + extra))
        self.topic = os.environ.get("TOPIC", "technology and AI")
        self.scan_minutes = float(os.environ.get("SCAN_MINUTES", "30"))
        self.digest_every_hours = float(os.environ.get("DIGEST_EVERY_HOURS", "24"))
        self.alert_keywords = [k.strip().lower() for k in os.environ.get("ALERT_KEYWORDS", "").split(",") if k.strip()]
        self.max_items = int(os.environ.get("MAX_ITEMS_PER_DIGEST", "40"))
        logger.info(
            f"News digest ready | {len(self.feed_urls)} feeds | scan every {self.scan_minutes}m | "
            f"digest every {self.digest_every_hours}h"
        )

    # --- phases ---

    @phase("digest", when=lambda a: bool(a.state.get("pending")) and (
        a.state.get("digest_requested") or util.due(a.state, "last_digest", a.digest_every_hours)))
    async def digest(self) -> dict:
        state = self.state
        items = state.get("pending", [])[-self.max_items:]
        if not items:
            state["digest_requested"] = False
            return {"result": "nothing_to_digest"}

        system, prompt = build_prompt(items, self.topic)
        digest_md = await self.ask_llm(prompt, system=system, max_tokens=6000, stub=stub_digest(items))

        sources = sorted({i["source"] for i in items})
        record = self.record(
            "digest",
            payload={
                "digest_md": digest_md,
                "item_count": len(items),
                "sources": sources,
                "items": [{"title": i["title"], "link": i["link"], "source": i["source"]} for i in items],
            },
            title=f"News digest — {len(items)} items from {len(sources)} sources",
            tags=["digest"],
        )
        await self.emit("news.digest", {"record_id": record["id"], "item_count": len(items)})

        state["pending"] = []
        state["digest_requested"] = False
        logger.info(f"Digest written: {len(items)} items from {len(sources)} sources")
        return {"result": "digest", "items": len(items)}

    @phase("scan", every_hours=lambda a: a.scan_minutes / 60)
    async def scan(self) -> dict:
        state = self.state
        seen = set(state.get("seen_ids", []))
        items = await asyncio.to_thread(feeds.fetch, self.feed_urls)

        fresh = [i for i in items if i["id"] not in seen]
        alerts = 0
        for item in fresh:
            seen.add(item["id"])
            matched = feeds.matched_keywords(item, self.alert_keywords)
            if matched and not self.has_recent_record(f"alert:{item['id']}", within_hours=72):
                self.record(
                    "alert",
                    payload={
                        "title": item["title"],
                        "summary": item["summary"],
                        "link": item["link"],
                        "source": item["source"],
                        "keywords": matched,
                    },
                    title=f"Alert [{', '.join(matched)}]: {item['title']}",
                    tags=["alert"] + matched,
                    dedupe_key=f"alert:{item['id']}",
                )
                alerts += 1

        pending = state.get("pending", []) + fresh
        state["pending"] = pending[-MAX_PENDING:]
        state["seen_ids"] = list(seen)[-MAX_SEEN:]

        if fresh:
            logger.info(f"Scan: {len(fresh)} new items ({alerts} alerts), {len(state['pending'])} pending")
        return {"result": "scanned", "new": len(fresh), "alerts": alerts}

    # --- chat ---

    async def on_command(self, command: dict):
        text = command.get("text", "").strip()
        state = self.state

        if text.lower() == "digest":
            if not state.get("pending"):
                return "Nothing pending to digest yet — I'll keep scanning the feeds."
            state["digest_requested"] = True
            self.save_state(state)
            return (f"Compiling a digest from **{len(state['pending'])} pending items** — "
                    f"it will appear in Records within a minute.")

        if text.lower().startswith("feed "):
            url = text[5:].strip()
            if not url.startswith("http"):
                return "Give me a full feed URL, e.g. `feed https://example.com/rss`."
            extras = state.setdefault("extra_feeds", [])
            if url not in extras and url not in self.feed_urls:
                extras.append(url)
                self.feed_urls.append(url)
                self.save_state(state)
            return f"Now watching **{url}** ({len(self.feed_urls)} feeds total)."

        if text.lower() == "status":
            return (
                f"**Status**\n"
                f"- Feeds: {len(self.feed_urls)}\n"
                f"- Pending items: {len(state.get('pending', []))}\n"
                f"- Alert keywords: {', '.join(self.alert_keywords) or 'none'}\n"
                f"- Last scan: {state.get('last_scan', 'never')}\n"
                f"- Last digest: {state.get('last_digest', 'never')}"
            )

        return await super().on_command(command)


if __name__ == "__main__":
    agent = NewsDigestAgent()
    asyncio.run(agent.run())
