"""
Site Sentinel — DispatchKit Agent

Watches the sites a business depends on and raises an alarm the moment one
stops working:

- check: every few minutes, fetch each URL and record status, response time,
  and whether an expected phrase is still on the page (catches "the site
  loads but the quote form is gone"). Repeated failures open an incident with
  an AI explanation in plain language; the first good check closes it with the
  downtime duration.
- certs: TLS certificates are checked daily and flagged before they expire,
  which is the outage every business forgets to prevent.
- report: a rolling uptime report on a fixed cadence, or on demand via chat.

Findings are emitted as structured records: kind="incident", kind="recovery",
kind="cert", kind="report".
"""

import asyncio
import logging
import os
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent / "lib"))

from dispatchkit import Agent, util

import checks
from report import incident_prompt, report_prompt, stub_incident, stub_report

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

MAX_LATENCY_SAMPLES = 500


def _parse_targets(raw: str) -> list:
    """URLS format: "https://a.com, https://b.com|Book now" — the optional
    part after | is text that must be present for the page to count as up."""
    targets = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        url, _, must = chunk.partition("|")
        url = url.strip()
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        targets.append({"url": url, "must_contain": must.strip()})
    return targets


class SiteSentinelAgent(Agent):
    def __init__(self):
        super().__init__()
        self.targets: list = []
        self.check_minutes: float = 5.0
        self.failures_before_alert: int = 2
        self.cert_warn_days: int = 21
        self.latency_warn_ms: int = 5000
        self.report_every_hours: float = 24.0

    async def setup(self):
        self.targets = _parse_targets(os.environ.get("URLS", ""))
        extra = self.state.get("extra_targets", [])
        known = {t["url"] for t in self.targets}
        self.targets += [t for t in extra if t["url"] not in known]

        self.check_minutes = float(os.environ.get("CHECK_MINUTES", "5"))
        self.failures_before_alert = int(os.environ.get("FAILURES_BEFORE_ALERT", "2"))
        self.cert_warn_days = int(os.environ.get("CERT_WARN_DAYS", "21"))
        self.latency_warn_ms = int(os.environ.get("LATENCY_WARN_MS", "5000"))
        self.report_every_hours = float(os.environ.get("REPORT_EVERY_HOURS", "24"))
        logger.info(
            f"Site sentinel ready | {len(self.targets)} target(s) | check every "
            f"{self.check_minutes}m | alert after {self.failures_before_alert} failures | "
            f"cert warning at {self.cert_warn_days}d"
        )

    # --- observe/think/act ---

    async def observe(self) -> dict:
        """Every due phase runs in the same tick. Selecting only one would let
        a short CHECK_MINUTES starve certificates and reports forever."""
        state = self.state
        if not self.targets:
            return {}
        return {
            "check": util.due(state, "last_check", self.check_minutes / 60),
            "certs": util.due(state, "last_cert_check", 24.0),
            "report": bool(state.get("report_requested")) or util.due(state, "last_report", self.report_every_hours),
        }

    async def think(self, obs: dict) -> Optional[dict]:
        return obs if any(obs.values()) else None

    async def act(self, decision: dict) -> dict:
        results = {}
        if decision.get("check"):
            results["check"] = await self._run_check()
        if decision.get("certs"):
            results["certs"] = await self._run_certs()
        if decision.get("report"):
            results["report"] = await self._run_report()
        self.save_state(self.state)
        return results

    # --- phases ---

    async def _run_check(self) -> dict:
        state = self.state
        sites = state.setdefault("sites", {})
        down = 0
        recovered = 0

        for target in self.targets:
            url = target["url"]
            site = sites.setdefault(url, {})
            check = await asyncio.to_thread(checks.run_checks, url, target["must_contain"])

            site["checks"] = site.get("checks", 0) + 1
            samples = site.setdefault("latency_samples", [])
            if check["ok"]:
                samples.append(check["latency_ms"])
                del samples[:-MAX_LATENCY_SAMPLES]

            if check["ok"]:
                if site.get("down_since"):
                    recovered += 1
                    await self._record_recovery(url, site, check)
                site["consecutive_failures"] = 0
                site["down_since"] = None
            else:
                site["failures"] = site.get("failures", 0) + 1
                site["consecutive_failures"] = site.get("consecutive_failures", 0) + 1
                if site["consecutive_failures"] == self.failures_before_alert:
                    down += 1
                    site["down_since"] = check["checked_at"]
                    site["incidents"] = site.get("incidents", 0) + 1
                    await self._record_incident(url, site, check)

            site["last_check"] = check

        util.mark(state, "last_check")
        if down or recovered:
            logger.info(f"Check: {down} new incident(s), {recovered} recovery(ies)")
        return {"result": "checked", "targets": len(self.targets), "down": down, "recovered": recovered}

    async def _record_incident(self, url: str, site: dict, check: dict):
        system, prompt = incident_prompt(url, check, site["consecutive_failures"])
        note = await self.ask_llm(prompt, system=system, max_tokens=700, stub=stub_incident(url, check))
        record = self.record(
            "incident",
            payload={
                "url": url, "status": check["status"], "error": check["error"],
                "latency_ms": check["latency_ms"], "dns": check.get("dns"),
                "consecutive_failures": site["consecutive_failures"], "note": note,
            },
            title=f"DOWN: {url} — {check['error'] or 'not responding'}",
            score=float(site["consecutive_failures"]),
            tags=["incident", "down"],
        )
        await self.emit("site.incident", {"record_id": record["id"], "url": url, "status": check["status"]})
        logger.warning(f"INCIDENT {url}: {check['error']}")

    async def _record_recovery(self, url: str, site: dict, check: dict):
        started = site.get("down_since")
        minutes = 0.0
        if started:
            delta = datetime.now(timezone.utc) - datetime.fromisoformat(started)
            minutes = round(delta.total_seconds() / 60, 1)
        self.record(
            "recovery",
            payload={"url": url, "downtime_minutes": minutes, "status": check["status"],
                     "latency_ms": check["latency_ms"]},
            title=f"Recovered: {url} — down for {minutes} min",
            tags=["recovery"],
        )
        await self.emit("site.recovery", {"url": url, "downtime_minutes": minutes})
        logger.info(f"RECOVERED {url} after {minutes} min")

    async def _run_certs(self) -> dict:
        state = self.state
        sites = state.setdefault("sites", {})
        flagged = 0
        # Certificates belong to a host, not a URL — several watched pages on
        # one domain share one cert and must not produce duplicate warnings.
        by_host: dict = {}

        for target in self.targets:
            url = target["url"]
            host = checks.hostname_of(url)
            if host not in by_host:
                by_host[host] = await asyncio.to_thread(checks.check_cert, url)
            cert = by_host[host]
            if not cert or cert.get("error"):
                continue
            sites.setdefault(url, {})["cert"] = cert

        for host, cert in by_host.items():
            if not cert or cert.get("error"):
                continue
            days = cert["days_left"]
            if days <= self.cert_warn_days and not self.has_recent_record(f"cert:{host}", within_hours=24):
                flagged += 1
                expired = days < 0
                self.record(
                    "cert",
                    payload={"host": host, "days_left": days, "expires": cert["expires"],
                             "issuer": cert["issuer"]},
                    title=(f"Certificate EXPIRED for {host}" if expired
                           else f"Certificate for {host} expires in {days} days"),
                    score=float(max(0, self.cert_warn_days - days)),
                    tags=["cert", "expiring"],
                    dedupe_key=f"cert:{host}",
                )
                logger.warning(f"CERT {host}: {days} days left")

        util.mark(state, "last_cert_check")
        return {"result": "certs_checked", "flagged": flagged}

    def _stats(self) -> list:
        sites = self.state.get("sites", {})
        stats = []
        for target in self.targets:
            url = target["url"]
            site = sites.get(url, {})
            total = site.get("checks", 0)
            failures = site.get("failures", 0)
            samples = site.get("latency_samples", [])
            stats.append({
                "url": url,
                "checks": total,
                "failures": failures,
                "uptime_pct": round(100.0 * (total - failures) / total, 2) if total else 100.0,
                "median_ms": int(statistics.median(samples)) if samples else 0,
                "incidents": site.get("incidents", 0),
                "cert_days": (site.get("cert") or {}).get("days_left"),
                "currently_down": bool(site.get("down_since")),
            })
        return stats

    async def _run_report(self) -> dict:
        state = self.state
        stats = self._stats()
        if not any(s["checks"] for s in stats):
            state["report_requested"] = False
            util.mark(state, "last_report")
            return {"result": "no_data_yet"}

        system, prompt = report_prompt(stats, self.report_every_hours)
        body = await self.ask_llm(prompt, system=system, max_tokens=1200,
                                  stub=stub_report(stats, self.report_every_hours))
        worst = min((s["uptime_pct"] for s in stats), default=100.0)
        record = self.record(
            "report",
            payload={"report_md": body, "sites": stats, "window_hours": self.report_every_hours},
            title=f"Uptime report — {len(stats)} site(s), lowest {worst}%",
            score=worst,
            tags=["report"],
        )
        await self.emit("site.report", {"record_id": record["id"], "sites": len(stats)})

        # Reset the rolling window so each report covers its own period.
        for site in state.get("sites", {}).values():
            site["checks"] = 0
            site["failures"] = 0
            site["incidents"] = 0
            site["latency_samples"] = []
        state["report_requested"] = False
        util.mark(state, "last_report")
        logger.info(f"Report written for {len(stats)} site(s)")
        return {"result": "report", "sites": len(stats)}

    # --- chat ---

    async def on_command(self, command: dict):
        text = command.get("text", "").strip()
        state = self.state
        lower = text.lower()

        if lower == "report":
            state["report_requested"] = True
            self.save_state(state)
            return "Compiling an uptime report — it will appear in Records within a minute."

        if lower.startswith("watch "):
            spec = text[6:].strip()
            if not spec:
                return "Give me a URL, e.g. `watch example.com` or `watch example.com|Request a quote`."
            new = _parse_targets(spec)
            extras = state.setdefault("extra_targets", [])
            added = []
            for target in new:
                if all(t["url"] != target["url"] for t in self.targets):
                    self.targets.append(target)
                    extras.append(target)
                    added.append(target["url"])
            self.save_state(state)
            if not added:
                return "Already watching that."
            return f"Now watching **{', '.join(added)}** ({len(self.targets)} sites total)."

        if lower.startswith("unwatch "):
            url = text[8:].strip()
            before = len(self.targets)
            self.targets = [t for t in self.targets if t["url"] != url and t["url"] != f"https://{url}"]
            state["extra_targets"] = [t for t in state.get("extra_targets", [])
                                      if t["url"] != url and t["url"] != f"https://{url}"]
            self.save_state(state)
            if len(self.targets) == before:
                return f"I wasn't watching {url}."
            return f"Stopped watching **{url}** ({len(self.targets)} sites remain)."

        if lower == "status":
            if not self.targets:
                return "No sites configured yet. Add one with `watch example.com`."
            lines = ["**Status**"]
            for s in self._stats():
                mark = "DOWN" if s["currently_down"] else "up"
                cert = f", cert {s['cert_days']}d" if s["cert_days"] is not None else ""
                lines.append(
                    f"- {s['url']}: {mark}, {s['uptime_pct']}% over {s['checks']} checks, "
                    f"median {s['median_ms']} ms{cert}"
                )
            lines.append(f"- Last check: {state.get('last_check', 'never')}")
            lines.append(f"- Last report: {state.get('last_report', 'never')}")
            return "\n".join(lines)

        return await super().on_command(command)


if __name__ == "__main__":
    agent = SiteSentinelAgent()
    asyncio.run(agent.run())
