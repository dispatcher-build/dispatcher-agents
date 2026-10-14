"""
Vendor Status Watch — DispatchKit Agent

Watches the public status pages of the SaaS vendors a business depends on
(Statuspage-compatible sites — GitHub, Cloudflare, Zoom, Twilio and thousands
more) and turns them into one feed:

- scan: poll each vendor's status API; record new incidents the moment they
  open, and recoveries when they resolve
- report: on a fixed cadence (or on demand via chat), Claude compiles a
  vendor-health brief of what broke, what recovered, and what is degraded

Findings are emitted as structured records: kind="baseline", kind="incident",
kind="recovered", kind="report".
"""

import asyncio
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "lib"))

from dispatchkit import Agent, phase, util

import statuspage
from report import build_prompt, stub_report

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

MAX_EVENTS = 200

IMPACT_SCORE = {"critical": 100.0, "major": 75.0, "minor": 40.0}


class VendorStatusWatchAgent(Agent):
    def __init__(self):
        super().__init__()
        self.vendors: list = []
        self.scan_minutes: float = 5.0
        self.report_every_hours: float = 24.0

    async def setup(self):
        env_vendors = statuspage.parse_vendors(os.environ.get("VENDORS", ""))
        extra = self.state.get("extra_vendors", [])
        merged = {v["name"]: v for v in env_vendors + extra}
        self.vendors = list(merged.values())
        self.scan_minutes = float(os.environ.get("SCAN_MINUTES", "5"))
        self.report_every_hours = float(os.environ.get("REPORT_EVERY_HOURS", "24"))

        if "last_report" not in self.state:
            util.mark(self.state, "last_report")
            self.save_state(self.state)

        logger.info(
            f"Vendor status watch ready | {len(self.vendors)} vendor(s) | scan every "
            f"{self.scan_minutes}m | brief every {self.report_every_hours}h"
        )

    # --- phases ---

    @phase("report", when=lambda a: bool(
        a.state.get("report_requested") or util.due(a.state, "last_report", a.report_every_hours)))
    async def report(self) -> dict:
        state = self.state
        health = state.get("health", {})
        events = state.get("events", [])
        if not health:
            state["report_requested"] = False
            return {"result": "nothing_to_report"}

        system, prompt = build_prompt(health, events)
        report_md = await self.ask_llm(prompt, system=system, max_tokens=2000, stub=stub_report(health, events))

        degraded = [n for n, v in health.items() if v["indicator"] not in ("none", "unknown")]
        self.record(
            "report",
            payload={"report_md": report_md, "vendors": list(health), "degraded": degraded,
                     "event_count": len(events)},
            title=f"Vendor health brief — {len(events)} event(s), "
                  + (f"{len(degraded)} degraded" if degraded else "all operational"),
            tags=["report"],
        )

        state["events"] = []
        state["report_requested"] = False
        logger.info(f"Brief written: {len(events)} events across {len(health)} vendors")
        return {"result": "report", "events": len(events)}

    @phase("scan", every_hours=lambda a: a.scan_minutes / 60, when=lambda a: bool(a.vendors))
    async def scan(self) -> dict:
        state = self.state
        health = state.get("health", {})
        open_incidents = state.get("open_incidents", {})
        first_scan = not health
        new = 0
        recovered = 0

        for vendor in self.vendors:
            name = vendor["name"]
            try:
                summary = await asyncio.to_thread(statuspage.fetch_summary, vendor["url"])
            except Exception as e:
                logger.warning(f"{name}: status fetch failed: {e}")
                continue

            health[name] = {
                "indicator": summary["indicator"],
                "description": summary["description"],
                "url": vendor["url"],
            }

            current_ids = set()
            for inc in summary["incidents"]:
                key = f"{name}:{inc['id']}"
                current_ids.add(key)
                if key in open_incidents:
                    continue
                open_incidents[key] = {"vendor": name, "name": inc["name"], "impact": inc["impact"]}
                new += 1
                self._log_event(state, "incident", name, inc["name"])
                self.record(
                    "incident",
                    payload={"vendor": name, "impact": inc["impact"], "status": inc["status"],
                             "url": inc["url"], "updated_at": inc["updated_at"]},
                    title=f"{name}: {inc['name']}",
                    score=IMPACT_SCORE.get(inc["impact"], 20.0),
                    tags=["incident", name.lower()],
                    dedupe_key=key,
                )

            for key in [k for k in list(open_incidents) if k.startswith(f"{name}:") and k not in current_ids]:
                inc = open_incidents.pop(key)
                recovered += 1
                self._log_event(state, "recovered", name, inc["name"])
                self.record(
                    "recovered",
                    payload={"vendor": name, "incident": inc["name"], "impact": inc.get("impact", "")},
                    title=f"{name} recovered: {inc['name']}",
                    tags=["recovered", name.lower()],
                    dedupe_key=f"{key}:recovered",
                )

        state["health"] = health
        state["open_incidents"] = open_incidents

        if first_scan and health:
            degraded = [n for n, v in health.items() if v["indicator"] not in ("none", "unknown")]
            self.record(
                "baseline",
                payload={"vendors": health, "degraded": degraded},
                title=f"Watching {len(health)} vendors — "
                      + (f"{len(degraded)} degraded right now" if degraded else "all operational"),
                tags=["baseline"],
            )

        if new or recovered:
            logger.info(f"Scan: {new} new incident(s), {recovered} recovered, {len(open_incidents)} open")
        return {"result": "scanned", "new": new, "recovered": recovered}

    def _log_event(self, state: dict, kind: str, vendor: str, title: str):
        events = state.setdefault("events", [])
        events.append({"kind": kind, "vendor": vendor, "title": title})
        state["events"] = events[-MAX_EVENTS:]

    # --- chat ---

    async def on_command(self, command: dict):
        text = command.get("text", "").strip()
        state = self.state
        lower = text.lower()

        if lower == "report":
            state["report_requested"] = True
            self.save_state(state)
            return "Compiling a vendor-health brief now — it will appear in Records within a minute."

        if lower.startswith("watch "):
            raw = text[6:].strip()
            parsed = statuspage.parse_vendors(raw)
            if not parsed:
                return "Give me a vendor, e.g. `watch Datadog|https://status.datadoghq.com`."
            extras = state.setdefault("extra_vendors", [])
            for vendor in parsed:
                if all(v["name"] != vendor["name"] for v in self.vendors):
                    extras.append(vendor)
                    self.vendors.append(vendor)
            self.save_state(state)
            return f"Now watching **{parsed[0]['name']}** ({len(self.vendors)} vendors total)."

        if lower.startswith("unwatch "):
            name = text[8:].strip()
            self.vendors = [v for v in self.vendors if v["name"].lower() != name.lower()]
            state["extra_vendors"] = [v for v in state.get("extra_vendors", []) if v["name"].lower() != name.lower()]
            state.get("health", {}).pop(name, None)
            self.save_state(state)
            return f"Stopped watching **{name}** ({len(self.vendors)} vendors remain)."

        if lower == "status":
            health = state.get("health", {})
            lines = [f"**Watching {len(self.vendors)} vendor(s)**"]
            for vendor in self.vendors:
                v = health.get(vendor["name"])
                lines.append(f"- {vendor['name']}: {v['description'] or v['indicator'] if v else 'not checked yet'}")
            lines.append(f"- Open incidents: {len(state.get('open_incidents', {}))}")
            lines.append(f"- Last scan: {state.get('last_scan', 'never')}")
            return "\n".join(lines)

        return await super().on_command(command)


if __name__ == "__main__":
    agent = VendorStatusWatchAgent()
    asyncio.run(agent.run())
