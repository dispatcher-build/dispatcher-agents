"""
Stock Research — DispatchKit Agent

Hunts for undervalued companies across semiconductors and the data-center
buildout supply chain. Runs around the clock on the SDK tick loop:

- fundamentals: daily universe refresh (yfinance + SEC EDGAR) + quantitative
  screen; top passers are queued for deep dives
- news: frequent RSS scans; keyword-relevant headlines queue deep dives
- deep_dive: Claude (Bedrock) writes a thesis with a conviction score,
  capped per day and cooled down per ticker

Findings are emitted as structured records (data/records.jsonl) and
research.finding events.
"""

import asyncio
import json
import logging
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent / "lib"))

from dispatchkit import Agent, util

import market_data
import news
import screener
from analyst import deep_dive

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

MAX_QUEUE = 10
MAX_SEEN_NEWS = 500
RESEARCH_CMD_RE = re.compile(r"(?i)^research\s+([A-Za-z.\-]{1,6})$")


class StockResearchAgent(Agent):
    def __init__(self):
        super().__init__()
        self.segments: dict = {}
        self.tickers: list = []
        self.news_feeds: list = []
        self.trigger_keywords: list = []
        self.fundamentals_refresh_hours: float = 24.0
        self.news_scan_minutes: float = 45.0
        self.max_deep_dives_per_day: int = 3
        self.cooldown_days: float = 7.0
        self.metrics_cache: dict = {}
        self.fundamentals_file = self.config.data_dir / "fundamentals.json"

    async def setup(self):
        universe = json.loads((Path(__file__).parent / "universe.json").read_text())
        self.segments = universe["segments"]
        self.news_feeds = universe.get("news_feeds", [])
        self.trigger_keywords = universe.get("trigger_keywords", [])

        for extra in os.environ.get("EXTRA_TICKERS", "").split(","):
            if ":" in extra:
                ticker, segment = extra.strip().split(":", 1)
                self.segments.setdefault(segment, []).append(ticker.upper())

        self.tickers = sorted({t for tickers in self.segments.values() for t in tickers})

        self.fundamentals_refresh_hours = float(os.environ.get("FUNDAMENTALS_REFRESH_HOURS", "24"))
        self.news_scan_minutes = float(os.environ.get("NEWS_SCAN_MINUTES", "45"))
        self.max_deep_dives_per_day = int(os.environ.get("MAX_DEEP_DIVES_PER_DAY", "3"))
        self.cooldown_days = float(os.environ.get("DEEP_DIVE_COOLDOWN_DAYS", "7"))

        if self.fundamentals_file.exists():
            self.metrics_cache = json.loads(self.fundamentals_file.read_text())

        logger.info(
            f"Stock research ready | {len(self.tickers)} tickers across {len(self.segments)} segments | "
            f"max {self.max_deep_dives_per_day} deep dives/day"
        )

    async def teardown(self):
        self._persist()

    # --- observe/think/act ---

    async def observe(self) -> dict:
        state = self.state
        today = datetime.utcnow().strftime("%Y-%m-%d")

        if state.get("deep_dive_date") != today:
            state["deep_dive_date"] = today
            state["deep_dives_today"] = 0

        candidate = self._next_candidate(state)
        if candidate and state.get("deep_dives_today", 0) < self.max_deep_dives_per_day:
            return {"phase": "deep_dive", "candidate": candidate}

        if util.due(state, "last_fundamentals_refresh", self.fundamentals_refresh_hours):
            return {"phase": "fundamentals"}

        if util.due(state, "last_news_scan", self.news_scan_minutes / 60):
            return {"phase": "news"}

        return {"phase": "idle"}

    async def think(self, obs: dict) -> Optional[dict]:
        if obs["phase"] == "idle":
            return None
        return obs

    async def act(self, decision: dict) -> dict:
        phase = decision["phase"]
        if phase == "fundamentals":
            result = self._run_fundamentals()
        elif phase == "news":
            result = self._run_news_scan()
        elif phase == "deep_dive":
            result = await self._run_deep_dive(decision["candidate"])
        else:
            result = {"result": "unknown_phase"}
        self._persist()
        return result

    # --- phases ---

    def _run_fundamentals(self) -> dict:
        logger.info(f"Refreshing fundamentals for {len(self.tickers)} tickers...")
        cik_map = market_data.load_cik_map(self.config.data_dir / "cik_map.json")
        metrics = market_data.refresh_universe(self.tickers, cik_map)
        if metrics:
            self.metrics_cache = metrics
            self.fundamentals_file.write_text(json.dumps(metrics, indent=2, default=str))

        rows = screener.rank(self.metrics_cache, self.segments)
        passers = [r for r in rows if r["passes"]]
        queued = 0
        for row in passers:
            if self._enqueue(row["ticker"], "screen", f"composite {row['composite']}"):
                queued += 1

        state = self.state
        util.mark(state, "last_fundamentals_refresh")

        today = datetime.utcnow().strftime("%Y-%m-%d")
        if rows and not self.has_recent_record(f"screen:{today}", within_hours=20):
            self.record(
                "screen",
                payload={
                    "body_md": screener.to_markdown_table(rows),
                    "passers": [r["ticker"] for r in passers],
                    "universe_size": len(self.tickers),
                    "covered": len(self.metrics_cache),
                },
                title=f"Daily screen: {len(passers)} candidates pass "
                      f"({', '.join(r['ticker'] for r in passers[:5])}{'…' if len(passers) > 5 else ''})",
                tags=["screen"],
                dedupe_key=f"screen:{today}",
            )

        logger.info(f"Screen complete: {len(rows)} ranked, {len(passers)} pass, {queued} queued for deep dive")
        return {"result": "screened", "ranked": len(rows), "passers": len(passers)}

    def _run_news_scan(self) -> dict:
        state = self.state
        seen = set(state.get("seen_news_ids", []))
        name_map = news.build_name_map(self.metrics_cache)

        items, next_offset = news.scan(
            self.news_feeds,
            self.tickers,
            name_map,
            self.trigger_keywords,
            seen,
            ticker_offset=state.get("news_ticker_offset", 0),
        )

        queued = 0
        for item in items:
            seen.add(item["id"])
            if self._enqueue(item["ticker"], "news", item["title"], link=item["link"]):
                queued += 1

        state["seen_news_ids"] = list(seen)[-MAX_SEEN_NEWS:]
        state["news_ticker_offset"] = next_offset
        util.mark(state, "last_news_scan")

        if items:
            logger.info(f"News scan: {len(items)} relevant items, {queued} queued")
        return {"result": "news_scanned", "items": len(items), "queued": queued}

    async def _run_deep_dive(self, candidate: dict) -> dict:
        ticker = candidate["ticker"]
        logger.info(f"Deep dive: {ticker} (reason: {candidate['reason']})")

        metrics = self.metrics_cache.get(ticker, {})
        rows = screener.rank(self.metrics_cache, self.segments)
        segment = next((s for s, tickers in self.segments.items() if ticker in tickers), "")
        peer_rows = [r for r in rows if r["segment"] == segment]

        cik_map = market_data.load_cik_map(self.config.data_dir / "cik_map.json")
        filings = market_data.recent_filings(ticker, cik_map)
        headlines = [candidate["context"]] if candidate.get("context") else []
        article_urls = [candidate["link"]] if candidate.get("link") else []

        state = self.state
        try:
            analysis = deep_dive(ticker, metrics, peer_rows, filings, headlines, article_urls)
        except Exception as e:
            logger.error(f"Deep dive failed for {ticker}: {e}")
            if not candidate.get("retried"):
                candidate["retried"] = True
                state.setdefault("deep_dive_queue", []).append(candidate)
            return {"result": "deep_dive_failed", "ticker": ticker}

        state["deep_dives_today"] = state.get("deep_dives_today", 0) + 1

        sources = [f["description"] for f in filings[:3]] + article_urls
        record = self.record(
            "finding",
            payload={
                "ticker": ticker,
                "segment": segment,
                "verdict": analysis["verdict"],
                "conviction": analysis["conviction"],
                "thesis_md": analysis["thesis_md"],
                "risks": analysis["risks"],
                "catalysts": analysis["catalysts"],
                "trigger": {"reason": candidate["reason"], "context": candidate.get("context", "")},
                "metrics": metrics,
                "sources": sources,
            },
            title=f"{ticker}: {analysis['verdict']} — {analysis['one_liner']}",
            score=analysis["conviction"] / 100,
            tags=[ticker, segment, analysis["verdict"]],
            dedupe_key=f"finding:{ticker}",
        )
        await self.emit("research.finding", {
            "ticker": ticker,
            "verdict": analysis["verdict"],
            "conviction": analysis["conviction"],
            "record_id": record["id"],
        })

        logger.info(f"Finding: {record['title']}")
        return {"result": "finding", "ticker": ticker, "verdict": analysis["verdict"]}

    # --- helpers ---

    def _next_candidate(self, state: dict) -> Optional[dict]:
        queue = state.get("deep_dive_queue", [])
        while queue:
            candidate = queue.pop(0)
            cooldown_hours = 24 * self.cooldown_days
            if candidate.get("force") or not self.has_recent_record(
                f"finding:{candidate['ticker']}", within_hours=cooldown_hours
            ):
                state["deep_dive_queue"] = queue
                return candidate
        state["deep_dive_queue"] = queue
        return None

    def _enqueue(self, ticker: str, reason: str, context: str, link: str = "") -> bool:
        state = self.state
        queue = state.setdefault("deep_dive_queue", [])
        if len(queue) >= MAX_QUEUE or any(c["ticker"] == ticker for c in queue):
            return False
        if self.has_recent_record(f"finding:{ticker}", within_hours=24 * self.cooldown_days):
            return False
        queue.append({"ticker": ticker, "reason": reason, "context": context, "link": link})
        return True

    def _persist(self):
        self.save_state(self.state)

    async def on_command(self, command: dict):
        text = command.get("text", "").strip()
        state = self.state

        match = RESEARCH_CMD_RE.match(text)
        if match:
            ticker = match.group(1).upper()
            queue = state.setdefault("deep_dive_queue", [])
            queue.insert(0, {"ticker": ticker, "reason": "user", "context": "user request",
                             "link": "", "force": True})
            self._persist()
            return (f"Queued a deep dive for **{ticker}** at the front of the queue "
                    f"({len(queue)} queued). The finding will appear in Records within a few minutes.")

        if text.lower() == "status":
            queue = state.get("deep_dive_queue", [])
            return (
                f"**Status**\n"
                f"- Deep dives today: {state.get('deep_dives_today', 0)}/{self.max_deep_dives_per_day}\n"
                f"- Queue: {', '.join(c['ticker'] for c in queue) or 'empty'}\n"
                f"- Last fundamentals refresh: {state.get('last_fundamentals_refresh', 'never')}\n"
                f"- Last news scan: {state.get('last_news_scan', 'never')}\n"
                f"- Universe: {len(self.tickers)} tickers across {len(self.segments)} segments"
            )

        return await super().on_command(command)


if __name__ == "__main__":
    agent = StockResearchAgent()
    asyncio.run(agent.run())
