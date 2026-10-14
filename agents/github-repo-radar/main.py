"""
GitHub Repo Radar — DispatchKit Agent

Tracks configured public GitHub repositories around the clock:

- check: poll repo stats and the latest release on a cadence (conditional
  requests keep unauthenticated API quota usage near zero); new releases are
  announced immediately, and a star/issue history accumulates in state
- report: on a fixed cadence (or on demand via chat), Claude writes a
  momentum report comparing star velocity, releases and issue movement
  across the watched repos

Findings are emitted as structured records: kind="release", kind="report",
kind="baseline".
"""

import asyncio
import logging
import os
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "lib"))

from dispatchkit import Agent, phase, util

import gh

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

REPO_RE = re.compile(r"^[\w.\-]+/[\w.\-]+$")
MAX_HISTORY = 120


class RepoRadarAgent(Agent):
    def __init__(self):
        super().__init__()
        self.repos: list = []
        self.token: str = ""
        self.check_hours: float = 6.0
        self.report_every_hours: float = 168.0

    async def setup(self):
        env_repos = [r.strip() for r in os.environ.get("REPOS", "").split(",") if r.strip()]
        extra = self.state.get("extra_repos", [])
        self.repos = list(dict.fromkeys(env_repos + extra))
        self.token = os.environ.get("GITHUB_TOKEN", "").strip()
        self.check_hours = float(os.environ.get("CHECK_HOURS", "6"))
        self.report_every_hours = float(os.environ.get("REPORT_EVERY_HOURS", "168"))
        logger.info(
            f"Repo radar ready | {len(self.repos)} repos | check every {self.check_hours}h | "
            f"report every {self.report_every_hours}h | token: {'yes' if self.token else 'no'}"
        )

    # --- phases ---

    @phase("report", when=lambda a: bool(a.state.get("report_requested")) or (
        bool(a.state.get("history")) and util.due(a.state, "last_report", a.report_every_hours)))
    async def report(self) -> dict:
        state = self.state
        history = state.get("history", {})
        if not history:
            state["report_requested"] = False
            return {"result": "no_history"}

        lines = []
        for repo, entries in history.items():
            if not entries:
                continue
            first, last = entries[0], entries[-1]
            hours = max(1.0, util.hours_since(first["ts"]))
            velocity = (last["stars"] - first["stars"]) / (hours / 24)
            lines.append(
                f"- {repo}: {last['stars']:,} stars ({velocity:+.0f}/day over {hours / 24:.1f}d), "
                f"{last['open_issues']:,} open issues (was {first['open_issues']:,}), "
                f"forks {last['forks']:,}, last release {state.get('last_release', {}).get(repo, 'none seen')}"
            )

        report_md = await self.ask_llm(
            "Tracked repository history:\n\n" + "\n".join(lines),
            system=("You write a concise momentum report on GitHub repositories for a technical "
                    "audience. Compare the repos: who is growing fastest, notable releases, issue "
                    "backlog direction, and one takeaway each. Clean markdown, no preamble."),
            max_tokens=1500,
            stub="## Momentum report (LLM disabled — raw data)\n\n" + "\n".join(lines),
        )
        record = self.record(
            "report",
            payload={"report_md": report_md, "repos": list(history.keys())},
            title=f"Momentum report — {len(history)} repos",
            tags=["report"],
        )
        await self.emit("github.report", {"record_id": record["id"], "repos": len(history)})

        state["report_requested"] = False
        logger.info(f"Report written for {len(history)} repos")
        return {"result": "report", "repos": len(history)}

    @phase("check", every_hours=lambda a: a.check_hours, when=lambda a: bool(a.repos))
    async def check(self) -> dict:
        state = self.state
        etags = state.setdefault("etags", {})
        history = state.setdefault("history", {})
        releases = 0

        for repo in self.repos:
            snap = await asyncio.to_thread(gh.snapshot, repo, self.token, etags)

            if snap.get("error") and not self.has_recent_record(f"error:{repo}", within_hours=24):
                self.record(
                    "note",
                    payload={"repo": repo, "error": snap["error"]},
                    title=f"Cannot read {repo}: {snap['error']}",
                    tags=["error"],
                    dedupe_key=f"error:{repo}",
                )
                continue

            if snap.get("stats"):
                entries = history.setdefault(repo, [])
                first = not entries
                entries.append({"ts": util.now_iso(), **snap["stats"]})
                history[repo] = entries[-MAX_HISTORY:]
                if first:
                    self.record(
                        "baseline",
                        payload={"repo": repo, **snap["stats"]},
                        title=f"Now tracking {repo} ({snap['stats']['stars']:,} stars)",
                        tags=["baseline"],
                        dedupe_key=f"baseline:{repo}",
                    )

            release = snap.get("release")
            if release and release["tag"]:
                key = f"release:{repo}:{release['tag']}"
                if state.get("last_release", {}).get(repo) != release["tag"]:
                    state.setdefault("last_release", {})[repo] = release["tag"]
                    if not self.has_recent_record(key, within_hours=24 * 30):
                        notes_md = release.pop("notes", "")
                        self.record(
                            "release",
                            payload={"repo": repo, **release, "notes_md": notes_md},
                            title=f"{repo} released {release['tag']}",
                            tags=["release", repo],
                            dedupe_key=key,
                        )
                        await self.emit("github.release", {"repo": repo, "tag": release["tag"]})
                        releases += 1

        logger.info(f"Check complete: {len(self.repos)} repos, {releases} new releases")
        return {"result": "checked", "repos": len(self.repos), "releases": releases}

    # --- chat ---

    async def on_command(self, command: dict):
        text = command.get("text", "").strip()
        state = self.state

        if text.lower() == "report":
            if not state.get("history"):
                return "No history yet — the first check runs within a few minutes of deploy."
            state["report_requested"] = True
            self.save_state(state)
            return "Writing the momentum report now — it will appear in Records within a few minutes."

        if text.lower().startswith("track "):
            repo = text[6:].strip().removeprefix("https://github.com/").strip("/")
            if not REPO_RE.match(repo):
                return "Give me `owner/repo`, e.g. `track vercel/next.js`."
            extras = state.setdefault("extra_repos", [])
            if repo not in extras and repo not in self.repos:
                extras.append(repo)
                self.repos.append(repo)
                self.save_state(state)
            return f"Now tracking **{repo}** ({len(self.repos)} repos total)."

        if text.lower() == "status":
            return (
                f"**Status**\n"
                f"- Tracked: {', '.join(self.repos) or 'none'}\n"
                f"- Check cadence: every {self.check_hours:.0f}h (conditional requests, quota-friendly)\n"
                f"- Last check: {state.get('last_check', 'never')}\n"
                f"- Last report: {state.get('last_report', 'never')}\n"
                f"- GitHub token: {'configured' if self.token else 'none (60 req/h anonymous limit)'}"
            )

        return await super().on_command(command)


if __name__ == "__main__":
    agent = RepoRadarAgent()
    asyncio.run(agent.run())
