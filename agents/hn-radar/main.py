"""
Hacker News Radar — DispatchKit Agent

Watches Hacker News around the clock for configured keywords (brands,
products, competitors, topics):

- scan: search new stories and comments past a per-keyword cursor, record
  each fresh mention with thread context; hot threads (points above
  HOT_POINTS) are flagged immediately
- digest: on a fixed cadence (or on demand via chat), Claude compiles the
  pending mentions into a community-intelligence brief

Findings are emitted as structured records: kind="mention", kind="hot",
kind="digest".
"""

import asyncio
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "lib"))

from dispatchkit import Agent, phase, util

import hn
from digest import build_prompt, stub_digest

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

MAX_PENDING = 400
MAX_SEEN = 2000


class HNRadarAgent(Agent):
    def __init__(self):
        super().__init__()
        self.keywords: list = []
        self.scan_minutes: float = 20.0
        self.digest_every_hours: float = 24.0
        self.hot_points: int = 50
        self.max_mentions_per_scan: int = 25
        self.max_items: int = 60

    async def setup(self):
        env_keywords = [k.strip() for k in os.environ.get("KEYWORDS", "").split(",") if k.strip()]
        extra = self.state.get("extra_keywords", [])
        self.keywords = list(dict.fromkeys(env_keywords + extra))
        self.scan_minutes = float(os.environ.get("SCAN_MINUTES", "20"))
        self.digest_every_hours = float(os.environ.get("DIGEST_EVERY_HOURS", "24"))
        self.hot_points = int(os.environ.get("HOT_POINTS", "50"))
        self.max_mentions_per_scan = int(os.environ.get("MAX_MENTIONS_PER_SCAN", "25"))
        self.max_items = int(os.environ.get("MAX_ITEMS_PER_DIGEST", "60"))
        logger.info(
            f"HN radar ready | keywords: {', '.join(self.keywords) or 'none'} | "
            f"scan every {self.scan_minutes}m | digest every {self.digest_every_hours}h"
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

        system, prompt = build_prompt(items, self.keywords)
        digest_md = await self.ask_llm(prompt, system=system, max_tokens=3000, stub=stub_digest(items))

        record = self.record(
            "digest",
            payload={
                "digest_md": digest_md,
                "item_count": len(items),
                "keywords": self.keywords,
                "items": [{"title": i["title"], "hn_link": i["hn_link"], "points": i["points"]} for i in items],
            },
            title=f"HN brief — {len(items)} mentions across {len(self.keywords)} keywords",
            tags=["digest"],
        )
        await self.emit("hn.digest", {"record_id": record["id"], "item_count": len(items)})

        state["pending"] = []
        state["digest_requested"] = False
        logger.info(f"Digest written: {len(items)} mentions")
        return {"result": "digest", "items": len(items)}

    @phase("scan", every_hours=lambda a: a.scan_minutes / 60, when=lambda a: bool(a.keywords))
    async def scan(self) -> dict:
        state = self.state
        seen = set(state.get("seen_ids", []))
        cursors = state.get("cursors", {})

        items, cursors = await asyncio.to_thread(hn.scan, self.keywords, cursors)
        fresh = [i for i in items if i["id"] not in seen]

        recorded = 0
        hot = 0
        for item in fresh:
            seen.add(item["id"])
            if item["points"] >= self.hot_points:
                kind, tags = "hot", ["hot", item["keyword"]]
            else:
                kind, tags = "mention", ["mention", item["keyword"]]

            if recorded < self.max_mentions_per_scan and not self.has_recent_record(f"hn:{item['id']}", within_hours=72):
                self.record(
                    kind,
                    payload={
                        "title": item["title"],
                        "type": item["type"],
                        "keyword": item["keyword"],
                        "points": item["points"],
                        "num_comments": item["num_comments"],
                        "author": item["author"],
                        "url": item["url"],
                        "hn_link": item["hn_link"],
                        "excerpt": item["excerpt"],
                    },
                    title=f"{'HOT ' if kind == 'hot' else ''}[{item['keyword']}] {item['title']}",
                    score=float(item["points"]),
                    tags=tags,
                    dedupe_key=f"hn:{item['id']}",
                )
                recorded += 1
                if kind == "hot":
                    hot += 1
                    await self.emit("hn.mention", {"id": item["id"], "keyword": item["keyword"], "points": item["points"]})

        pending = state.get("pending", []) + fresh
        state["pending"] = pending[-MAX_PENDING:]
        state["seen_ids"] = list(seen)[-MAX_SEEN:]
        state["cursors"] = cursors

        if fresh:
            logger.info(f"Scan: {len(fresh)} new mentions ({hot} hot, {recorded} recorded), {len(state['pending'])} pending")
        return {"result": "scanned", "new": len(fresh), "hot": hot}

    # --- chat ---

    async def on_command(self, command: dict):
        text = command.get("text", "").strip()
        state = self.state
        lower = text.lower()

        if lower == "digest":
            if not state.get("pending"):
                return "No pending mentions yet — I'll keep watching Hacker News."
            state["digest_requested"] = True
            self.save_state(state)
            return (f"Compiling a brief from **{len(state['pending'])} pending mentions** — "
                    f"it will appear in Records within a minute.")

        if lower.startswith("watch "):
            kw = text[6:].strip()
            if not kw:
                return "Give me a keyword, e.g. `watch dispatcher.build`."
            extras = state.setdefault("extra_keywords", [])
            if kw not in extras and kw not in self.keywords:
                extras.append(kw)
                self.keywords.append(kw)
                self.save_state(state)
            return f"Now watching **{kw}** ({len(self.keywords)} keywords total)."

        if lower.startswith("unwatch "):
            kw = text[8:].strip()
            self.keywords = [k for k in self.keywords if k != kw]
            state["extra_keywords"] = [k for k in state.get("extra_keywords", []) if k != kw]
            state.get("cursors", {}).pop(kw, None)
            self.save_state(state)
            return f"Stopped watching **{kw}** ({len(self.keywords)} keywords remain)."

        if lower == "status":
            return (
                f"**Status**\n"
                f"- Keywords: {', '.join(self.keywords) or 'none'}\n"
                f"- Pending mentions: {len(state.get('pending', []))}\n"
                f"- Hot threshold: {self.hot_points} points\n"
                f"- Last scan: {state.get('last_scan', 'never')}\n"
                f"- Last digest: {state.get('last_digest', 'never')}"
            )

        return await super().on_command(command)


if __name__ == "__main__":
    agent = HNRadarAgent()
    asyncio.run(agent.run())
