"""
Website Change Monitor — DispatchKit Agent

Watches configured web pages around the clock:

- check: fetch each page on a cadence, extract visible text, and compare
  against the stored snapshot; meaningful changes get a unified diff plus a
  Claude summary of what changed and why it matters
- incidents: non-200s and network errors are recorded once per outage, with
  a recovery record when the page comes back

Findings are emitted as structured records: kind="change", kind="incident",
kind="recovery", kind="baseline".
"""

import asyncio
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "lib"))

from dispatchkit import Agent, phase, util

import pages

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


class SiteMonitorAgent(Agent):
    def __init__(self):
        super().__init__()
        self.urls: list = []
        self.check_minutes: float = 30.0
        self.selector: str = ""
        self.min_change_ratio: float = 0.02
        self.snapshots_dir = self.config.data_dir / "snapshots"

    async def setup(self):
        env_urls = [u.strip() for u in os.environ.get("URLS", "").split(",") if u.strip()]
        extra = self.state.get("extra_urls", [])
        self.urls = list(dict.fromkeys(env_urls + extra))
        self.check_minutes = float(os.environ.get("CHECK_MINUTES", "30"))
        self.selector = os.environ.get("CSS_SELECTOR", "").strip()
        self.min_change_ratio = float(os.environ.get("MIN_CHANGE_RATIO", "0.02"))
        self.snapshots_dir.mkdir(parents=True, exist_ok=True)
        logger.info(f"Site monitor ready | {len(self.urls)} pages | check every {self.check_minutes}m")

    # --- phases ---

    @phase("check", when=lambda a: bool(a.urls) and (
        a.state.get("check_requested") or util.due(a.state, "last_check", a.check_minutes / 60)))
    async def check(self) -> dict:
        state = self.state
        changes = incidents = 0
        for url in self.urls:
            outcome = await self._check_url(url)
            changes += outcome == "change"
            incidents += outcome == "incident"

        state["check_requested"] = False
        if changes or incidents:
            logger.info(f"Check complete: {changes} changes, {incidents} incidents")
        return {"result": "checked", "pages": len(self.urls), "changes": changes, "incidents": incidents}

    # --- per-page check ---

    async def _check_url(self, url: str) -> str:
        state = self.state
        down = state.setdefault("down", {})
        status, text = await asyncio.to_thread(pages.fetch_text, url, self.selector)

        if status != 200:
            if url not in down:
                down[url] = util.now_iso()
                self.record(
                    "incident",
                    payload={"url": url, "status": status or "network error", "since": down[url]},
                    title=f"Page down: {url} ({status or 'network error'})",
                    tags=["incident"],
                    dedupe_key=f"incident:{url}:{down[url]}",
                )
                await self.emit("site.incident", {"url": url, "status": status})
                return "incident"
            return "still_down"

        if url in down:
            since = down.pop(url)
            self.record(
                "recovery",
                payload={"url": url, "down_since": since, "recovered": util.now_iso()},
                title=f"Page recovered: {url}",
                tags=["recovery"],
            )

        snapshot_file = self.snapshots_dir / f"{pages.url_key(url)}.txt"
        old = snapshot_file.read_text() if snapshot_file.exists() else ""
        if not old:
            snapshot_file.write_text(text)
            self.record(
                "baseline",
                payload={"url": url, "chars": len(text)},
                title=f"Now watching {url}",
                tags=["baseline"],
                dedupe_key=f"baseline:{url}",
            )
            return "baseline"

        ratio = pages.change_ratio(old, text)
        if ratio < self.min_change_ratio:
            return "unchanged"

        snapshot_file.write_text(text)
        diff = pages.unified_diff(old, text, url)
        summary_md = await self.ask_llm(
            f"This page changed: {url}\n\nUnified diff of the visible text:\n\n{diff}",
            system=("You monitor web pages for a business audience. Summarize what changed "
                    "and why it might matter in 2-4 short bullet points of markdown. "
                    "Ignore cosmetic or boilerplate changes. No preamble."),
            max_tokens=800,
            stub=f"Change of {ratio:.0%} detected (LLM disabled — raw diff in payload).",
        )
        record = self.record(
            "change",
            payload={"url": url, "summary_md": summary_md, "diff": diff, "change_ratio": round(ratio, 4)},
            title=f"Change detected on {url} ({ratio:.0%} of content)",
            score=min(1.0, ratio * 5),
            tags=["change"],
        )
        await self.emit("site.change", {"url": url, "record_id": record["id"], "ratio": ratio})
        logger.info(f"Change on {url}: {ratio:.1%}")
        return "change"

    # --- chat ---

    async def on_command(self, command: dict):
        text = command.get("text", "").strip()
        state = self.state

        if text.lower() == "check":
            state["check_requested"] = True
            self.save_state(state)
            return f"Checking **{len(self.urls)} pages** now — results land in Records within a minute."

        if text.lower().startswith("watch "):
            url = text[6:].strip()
            if not url.startswith("http"):
                return "Give me a full URL, e.g. `watch https://example.com/pricing`."
            extras = state.setdefault("extra_urls", [])
            if url not in extras and url not in self.urls:
                extras.append(url)
                self.urls.append(url)
                self.save_state(state)
            return f"Now watching **{url}** ({len(self.urls)} pages total)."

        if text.lower() == "status":
            down = state.get("down", {})
            return (
                f"**Status**\n"
                f"- Watched pages: {len(self.urls)}\n"
                f"- Currently down: {', '.join(down) or 'none'}\n"
                f"- Check cadence: every {self.check_minutes:.0f}m\n"
                f"- Last check: {state.get('last_check', 'never')}"
            )

        return await super().on_command(command)


if __name__ == "__main__":
    agent = SiteMonitorAgent()
    asyncio.run(agent.run())
