"""
Outbound Marketing Email — DispatchKit Agent

Runs email campaigns against an audience you bring: a table in your own
database, a file, or addresses pasted into the worker's interface. Several
campaigns can exist at once; only the ones you arm actually send, and they
drip out within a daily cap and a rate you set rather than in one burst.

Sending is a deliberate act. Campaigns start as drafts, nothing sends until it
is armed from the interface (or chat), and a campaign can be paused mid-flight
without losing its place.

- import: pull an audience from postgres, a file, or a paste
- send:   drip armed campaigns out, batch by batch, one email per person ever
- report: a summary record when a campaign's queue drains

Records: kind="ready", kind="batch", kind="test", kind="blocked", kind="done".
"""

import asyncio
import json
import logging
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent / "lib"))

from dispatchkit import Agent, util

import campaigns as campaigns_lib
import providers as providers_lib
import sources as sources_lib

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

AGENT_DIR = Path(__file__).parent


def _flag(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


class OutboundMarketingEmailAgent(Agent):
    async def setup(self):
        self.provider_name = os.environ.get("PROVIDER", "resend").strip().lower()
        self.provider = providers_lib.build(self.provider_name, {
            "api_key": os.environ.get("RESEND_API_KEY", ""),
            "region": os.environ.get("AWS_REGION", "us-east-1"),
        })

        self.from_email = os.environ.get("FROM_EMAIL", "").strip()
        self.from_name = os.environ.get("FROM_NAME", "").strip()
        self.reply_to = os.environ.get("REPLY_TO", "").strip()
        self.unsubscribe_mailto = os.environ.get("UNSUBSCRIBE_MAILTO", "").strip() or self.reply_to
        self.unsubscribe_url = os.environ.get("UNSUBSCRIBE_URL", "").strip()

        self.batch_size = max(1, int(os.environ.get("BATCH_SIZE", "10")))
        self.send_interval_hours = float(os.environ.get("SEND_INTERVAL_MINUTES", "30")) / 60
        self.daily_cap = max(0, int(os.environ.get("DAILY_CAP", "200")))
        self.rate_per_second = max(0.05, float(os.environ.get("RATE_PER_SECOND", "1")))
        self.dry_run = _flag("DRY_RUN", "0")
        self.preflight_hours = float(os.environ.get("PREFLIGHT_HOURS", "6"))

        self.source_kind = os.environ.get("SOURCE", "").strip().lower()
        self.source_dsn = os.environ.get("SOURCE_DSN", "").strip()
        self.source_query = os.environ.get("SOURCE_QUERY", "").strip()
        self.source_file = os.environ.get("SOURCE_FILE", "").strip()

        missing = []
        if self.provider_name == "resend" and not os.environ.get("RESEND_API_KEY", "").strip():
            missing.append("RESEND_API_KEY")
        elif self.provider_name == "ses" and not os.environ.get("AWS_ACCESS_KEY_ID", "").strip():
            missing.extend(["AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"])
        if not self.from_email:
            missing.append("FROM_EMAIL")
        if missing:
            self.needs_setup(
                missing,
                "Connect your sending account: paste a Resend API key (resend.com → API Keys) "
                "or AWS SES credentials, and set FROM_EMAIL to a verified sender address.",
            )
        else:
            self.clear_needs_setup()

        self.campaigns = campaigns_lib.Campaigns(self.store)
        state = self.state
        state["load_requested"] = True
        self.save_state(state)

        logger.info(
            f"Outbound marketing email ready | provider={self.provider_name} | "
            f"campaigns={len(self.campaigns.all())} | {self.batch_size} every "
            f"{self.send_interval_hours * 60:.0f}m | cap {self.daily_cap or '∞'}/day"
        )

    # --- helpers ---

    def _sent_today(self) -> int:
        midnight = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        return self.campaigns.sent_since(midnight.isoformat(timespec="seconds"))

    def _remaining_today(self) -> int:
        if self.daily_cap == 0:
            return 10 ** 9
        return max(0, self.daily_cap - self._sent_today())

    def _sender_of(self, campaign: dict) -> tuple:
        return (campaign.get("from_email") or self.from_email,
                campaign.get("from_name") or self.from_name,
                campaign.get("reply_to") or self.reply_to)

    def _campaign_problems(self, campaign: dict) -> list:
        problems = []
        from_email, _, _ = self._sender_of(campaign)
        if not from_email:
            problems.append("no sender address (set FROM_EMAIL, or the campaign's own from)")
        if not (campaign.get("subject") or "").strip():
            problems.append("no subject")
        if not (campaign.get("body") or "").strip():
            problems.append("no body")
        if not self.campaigns.count_recipients(campaign["id"]):
            problems.append("no recipients")
        problems.extend((self.state.get("preflight") or {}).get("problems", []))
        return problems

    def _render(self, campaign: dict, row: dict) -> dict:
        return {
            "to": row["email"],
            "subject": sources_lib.render(campaign["subject"], row),
            "body": sources_lib.render(campaign["body"], row),
            "missing": sources_lib.missing_placeholders(campaign["body"], row)
            + sources_lib.missing_placeholders(campaign["subject"], row),
        }

    def _message(self, campaign: dict, rendered: dict) -> dict:
        from_email, from_name, reply_to = self._sender_of(campaign)
        return {
            "to": rendered["to"],
            "subject": rendered["subject"],
            "body": rendered["body"],
            "from_email": from_email,
            "from_name": from_name,
            "reply_to": reply_to,
            "is_html": bool(campaign.get("is_html")),
            "unsubscribe_mailto": self.unsubscribe_mailto or from_email,
            "unsubscribe_url": self.unsubscribe_url,
        }

    async def _refresh_preflight(self, state: dict, force: bool = False) -> dict:
        if force:
            state["last_preflight"] = ""
        if not util.due(state, "last_preflight", self.preflight_hours):
            return state.get("preflight") or {}

        report = await asyncio.to_thread(self.provider.preflight, self.from_email)
        state["preflight"] = report
        util.mark(state, "last_preflight")

        if report.get("ok") and self.provider_name == "ses" and not _flag("SKIP_SES_SUPPRESSION", "0"):
            try:
                addresses = await asyncio.to_thread(self.provider.suppressed)
            except Exception as e:
                logger.warning(f"could not read the SES suppression list: {e}")
            else:
                added = self.campaigns.suppress(addresses, reason="ses-bounce")
                state["provider_suppressed"] = len(addresses)
                logger.info(f"SES suppression list: {len(addresses)} address(es), {added} new")
        return report

    # --- observe / think / act ---

    async def observe(self) -> dict:
        state = self.state
        if state.get("import_request"):
            return {"phase": "import"}
        if state.get("test_request"):
            return {"phase": "test"}

        # Sending outranks the boot-time load: a scheduled wake runs exactly one
        # tick, so if load went first an armed campaign would never send — every
        # wake would burn its tick re-recording the campaign list.
        for campaign in self.campaigns.sending():
            if self.campaigns.queue_size(campaign["id"]) == 0:
                return {"phase": "finish", "campaign": campaign["name"]}
            if self._remaining_today() <= 0:
                continue
            key = f"last_send:{campaign['id']}"
            if util.due(state, key, self.send_interval_hours):
                return {"phase": "send", "campaign": campaign["name"]}

        if state.pop("load_requested", False):
            return {"phase": "load"}
        return {"phase": "idle"}

    async def think(self, obs: dict) -> Optional[dict]:
        return None if obs["phase"] == "idle" else obs

    async def act(self, decision: dict) -> dict:
        state = self.state
        phase = decision["phase"]
        if phase == "load":
            return await self._act_load(state)
        if phase == "import":
            return await self._act_import(state)
        if phase == "test":
            return await self._act_test(state)
        if phase == "finish":
            return self._act_finish(state, decision["campaign"])
        return await self._act_send(state, decision["campaign"])

    async def _act_load(self, state: dict) -> dict:
        report = await self._refresh_preflight(state, force=True)
        self.save_state(state)
        rows = self.campaigns.all()

        if not report.get("ok"):
            problems = report.get("problems", [])
            self.record(
                "blocked",
                payload={"problems": problems, "provider": self.provider_name,
                         "body_md": f"**{self.provider_name} is not ready to send.**\n"
                                    + "\n".join(f"- {p}" for p in problems)},
                title=f"{self.provider_name} not ready ({len(problems)} problem(s))",
                tags=["blocked"],
                dedupe_key=f"blocked:{self.provider_name}:{'|'.join(problems)}",
            )

        lines = [f"**{len(rows)} campaign(s)** · provider **{self.provider_name}**"]
        for campaign in rows:
            stats = self.campaigns.stats(campaign["id"])
            lines.append(
                f"- `{campaign['name']}` — {campaign['status']} · {stats['queued']} queued · "
                f"{stats['sent']} sent"
            )
        if not rows:
            lines.append("\nNo campaigns yet. Create one from the interface on the Overview "
                         "tab, or say `new <name>` in chat.")

        self.record(
            "ready",
            payload={"campaigns": rows, "provider": self.provider_name,
                     "preflight": report, "body_md": "\n".join(lines)},
            title=f"{len(rows)} campaign(s), provider {self.provider_name}",
            tags=["ready"],
            dedupe_key=f"ready:{len(rows)}:{report.get('ok')}",
        )
        return {"result": "loaded", "campaigns": len(rows)}

    async def _act_import(self, state: dict) -> dict:
        request = state.pop("import_request")
        self.save_state(state)
        name = request["campaign"]
        campaign = self.campaigns.get(name)
        if not campaign:
            return {"result": "unknown_campaign"}

        kind = (request.get("kind") or self.source_kind or "file").lower()
        if kind == "postgres":
            rows, problems = await asyncio.to_thread(
                sources_lib.from_postgres, self.source_dsn, self.source_query
            )
        elif kind == "paste":
            rows, problems = sources_lib.from_text(request.get("text", "")), []
        else:
            path = Path(request.get("file") or self.source_file or "list.csv")
            if not path.is_absolute():
                path = AGENT_DIR / path
            rows, problems = sources_lib.from_file(path)

        clean, rejected = sources_lib.normalise(rows)
        added = self.campaigns.add_recipients(campaign["id"], clean, source=kind)
        self.campaigns.add_rejected(campaign["id"], rejected)

        body_md = (
            f"**{name}** — imported from {kind}\n"
            f"- Added: **{added}** new recipient(s)\n"
            f"- Already present: {len(clean) - added}\n"
            f"- Rejected: {len(rejected)}\n"
            f"- Audience now: {self.campaigns.count_recipients(campaign['id'])}"
        )
        if problems:
            body_md += "\n- Problems: " + "; ".join(problems)

        self.record(
            "ready",
            payload={"campaign": name, "source": kind, "added": added,
                     "rejected": rejected[:50], "problems": problems, "body_md": body_md},
            title=f"{name}: +{added} recipient(s) from {kind}",
            tags=["import"],
        )
        logger.info(f"import into {name} from {kind}: +{added}, {len(rejected)} rejected")
        return {"result": "imported", "added": added, "rejected": len(rejected)}

    async def _act_send(self, state: dict, name: str) -> dict:
        campaign = self.campaigns.get(name)
        if not campaign:
            return {"result": "unknown_campaign"}

        problems = self._campaign_problems(campaign)
        if problems:
            self.campaigns.set_status(name, "paused")
            self.record(
                "blocked",
                payload={"campaign": name, "problems": problems,
                         "body_md": f"**{name} paused before sending.**\n"
                                    + "\n".join(f"- {p}" for p in problems)},
                title=f"{name} paused — {len(problems)} problem(s)",
                tags=["blocked"],
                dedupe_key=f"blocked:{name}:{'|'.join(problems)}",
            )
            return {"result": "blocked", "problems": problems}

        limit = min(campaign.get("batch_size") or self.batch_size, self._remaining_today())
        batch = self.campaigns.queue(campaign["id"], limit)
        sent, failed, rows, aborted = 0, 0, [], ""

        for row in batch:
            rendered = self._render(campaign, row)
            email = rendered["to"]

            if self.dry_run:
                rows.append({"email": email, "status": "dry-run", "subject": rendered["subject"]})
                continue

            if not self.campaigns.claim(campaign["id"], email, self.provider_name):
                continue
            try:
                message_id = await asyncio.to_thread(self.provider.send, self._message(campaign, rendered))
                self.campaigns.mark_sent(campaign["id"], email, message_id)
                rows.append({"email": email, "status": "sent", "id": message_id})
                sent += 1
                state["retry_streak"] = 0
            except Exception as e:
                detail = str(e)
                recipient_fault = getattr(e, "recipient_fault", False)
                logger.error(f"send to {email} failed: {detail}")
                if recipient_fault or state.get("retry_streak", 0) >= 2:
                    self.campaigns.mark_failed(campaign["id"], email, detail)
                    rows.append({"email": email, "status": "failed", "error": detail[:300]})
                    failed += 1
                else:
                    self.campaigns.release(campaign["id"], email)
                    state["retry_streak"] = int(state.get("retry_streak", 0)) + 1
                    aborted = detail
                    break
            await asyncio.sleep(1 / self.rate_per_second)

        util.mark(state, f"last_send:{campaign['id']}")
        self.save_state(state)
        stats = self.campaigns.stats(campaign["id"])

        if self.dry_run:
            self.record(
                "batch",
                payload={"campaign": name, "dry_run": True, "rows": rows,
                         "body_md": f"**Dry run** — would have sent {len(rows)} email(s) for "
                                    f"**{name}**. Set `DRY_RUN=0` to send for real."},
                title=f"Dry run: {len(rows)} email(s) for {name}",
                tags=["batch", "dry-run"],
                dedupe_key=f"dryrun:{name}:{stats['queued']}",
            )
            return {"result": "dry_run", "batch": len(rows)}

        if aborted:
            self.record(
                "blocked",
                payload={"campaign": name, "error": aborted, "sent": sent,
                         "attempt": state.get("retry_streak", 1),
                         "body_md": (f"**{name}: batch stopped — {self.provider_name} rejected the "
                                     f"request.**\n\n```\n{aborted[:600]}\n```\n"
                                     f"Nobody was burned: the queue is untouched and it retries in "
                                     f"{self.send_interval_hours * 60:.0f}m. Attempt "
                                     f"{state.get('retry_streak', 1)} of 3.")},
                title=f"{name}: batch stopped by {self.provider_name}",
                score=1.0,
                tags=["blocked", "failure"],
                dedupe_key=f"aborted:{name}:{aborted[:120]}",
            )
            return {"result": "aborted", "sent": sent, "error": aborted}

        self.record(
            "batch",
            payload={"campaign": name, "sent": sent, "failed": failed, **stats, "rows": rows,
                     "body_md": (f"**{name}: sent {sent}**"
                                 f"{f', **{failed} failed**' if failed else ''}.\n"
                                 f"- Remaining: {stats['queued']}\n"
                                 f"- Sent today: {self._sent_today()}/{self.daily_cap or '∞'}")},
            title=f"{name}: sent {sent}" + (f", {failed} failed" if failed else ""),
            score=1.0 if failed else None,
            tags=["batch"] + (["failure"] if failed else []),
        )
        logger.info(f"{name}: {sent} sent, {failed} failed, {stats['queued']} remaining")
        return {"result": "sent", "sent": sent, "failed": failed, "remaining": stats["queued"]}

    async def _act_test(self, state: dict) -> dict:
        request = state.pop("test_request")
        self.save_state(state)
        name, to_address = request["campaign"], request["to"]
        campaign = self.campaigns.get(name)
        if not campaign:
            return {"result": "unknown_campaign"}

        sample = (self.campaigns.queue(campaign["id"], 1) or [{}])[0]
        sample = dict(sample)
        sample["email"] = to_address
        rendered = self._render(campaign, sample)

        try:
            message_id = await asyncio.to_thread(self.provider.send, self._message(campaign, rendered))
            status, detail = "sent", message_id
        except Exception as e:
            status, detail = "failed", str(e)
            logger.error(f"test send to {to_address} failed: {detail}")

        self.record(
            "test",
            payload={"campaign": name, "to": to_address, "status": status, "detail": detail,
                     "subject": rendered["subject"],
                     "body_md": (f"**Test of {name} to {to_address}: {status}**\n\n"
                                 f"Subject: {rendered['subject']}\n\n"
                                 f"```\n{rendered['body'][:1500]}\n```"
                                 + (f"\n\nError: `{detail}`" if status == "failed" else ""))},
            title=f"Test of {name} to {to_address}: {status}",
            tags=["test"] + (["failure"] if status == "failed" else []),
        )
        return {"result": status, "to": to_address}

    def _act_finish(self, state: dict, name: str) -> dict:
        campaign = self.campaigns.get(name)
        if not campaign:
            return {"result": "unknown_campaign"}
        self.campaigns.set_status(name, "done")
        stats = self.campaigns.stats(campaign["id"])
        self.record(
            "done",
            payload={"campaign": name, **stats,
                     "body_md": (f"**{name} complete.**\n"
                                 f"- Sent: {stats['sent']}\n"
                                 f"- Failed: {stats['failed']}\n"
                                 f"- Audience: {stats['recipients']}\n\n"
                                 f"Add more recipients and arm it again to continue.")},
            title=f"{name} complete — {stats['sent']} sent",
            tags=["done"],
            dedupe_key=f"done:{name}:{stats['sent']}",
        )
        logger.info(f"{name} complete: {stats['sent']} sent")
        return {"result": "done", **stats}

    # --- chat: also the interface's only way to act ---

    async def on_command(self, command: dict):
        text = (command.get("text") or "").strip()
        head, _, rest = text.partition(" ")
        verb = head.strip().lower()
        rest = rest.strip()
        state = self.state

        if verb in ("status", ""):
            return {"text": self._status_text(), "stats": self._stats()}

        if verb == "preflight":
            report = await self._refresh_preflight(state, force=True)
            self.save_state(state)
            return {"text": self._preflight_text(report), "preflight": report}

        if verb == "new":
            name = rest.split("\n")[0].strip()
            if not name:
                return "Give the campaign a name, e.g. `new august-outreach`."
            if self.campaigns.get(name):
                return f"`{name}` already exists."
            self.campaigns.create(name)
            return f"Created draft campaign **{name}**. Set a subject and body, add recipients, then arm it."

        if verb == "set":
            return self._cmd_set(rest)

        if verb == "add":
            name, _, pasted = rest.partition("\n")
            campaign = self.campaigns.get(name.strip())
            if not campaign:
                return f"No campaign called `{name.strip()}`."
            rows, rejected = sources_lib.normalise(sources_lib.from_text(pasted))
            added = self.campaigns.add_recipients(campaign["id"], rows, source="paste")
            self.campaigns.add_rejected(campaign["id"], rejected)
            return (f"**{name.strip()}**: added {added} recipient(s)"
                    f"{f', rejected {len(rejected)}' if rejected else ''}. "
                    f"Audience is now {self.campaigns.count_recipients(campaign['id'])}.")

        if verb == "import":
            parts = rest.split()
            if not parts:
                return "Say which campaign, e.g. `import august-outreach postgres`."
            name = parts[0]
            if not self.campaigns.get(name):
                return f"No campaign called `{name}`."
            state["import_request"] = {"campaign": name,
                                       "kind": parts[1] if len(parts) > 1 else self.source_kind}
            self.save_state(state)
            return f"Importing recipients into **{name}** — result lands in Records shortly."

        if verb in ("arm", "start", "send"):
            return self._cmd_arm(rest)

        if verb in ("pause", "stop"):
            campaign = self.campaigns.get(rest)
            if not campaign:
                return f"No campaign called `{rest}`."
            self.campaigns.set_status(rest, "paused")
            return f"**{rest}** paused with {self.campaigns.queue_size(campaign['id'])} still queued."

        if verb == "preview":
            return self._cmd_preview(rest)

        if verb == "test":
            parts = rest.split()
            if len(parts) < 2 or "@" not in parts[1]:
                return "Say `test <campaign> you@example.com`."
            if not self.campaigns.get(parts[0]):
                return f"No campaign called `{parts[0]}`."
            state["test_request"] = {"campaign": parts[0], "to": parts[1].lower()}
            self.save_state(state)
            return f"Sending one test of **{parts[0]}** to {parts[1]} — result lands in Records shortly."

        if verb in ("suppress", "unsubscribe"):
            targets = [t for t in rest.replace(",", " ").split() if "@" in t]
            if not targets:
                return "Give me one or more addresses, e.g. `suppress a@b.com`."
            added = self.campaigns.suppress(targets, reason="manual")
            return (f"Suppressed {added} address(es). {self.campaigns.suppressed_count()} total — "
                    f"they are skipped by every campaign.")

        if verb == "delete":
            if not self.campaigns.delete(rest):
                return f"No campaign called `{rest}`."
            return f"Deleted **{rest}** and everything belonging to it."

        return await super().on_command(command)

    def _cmd_set(self, rest: str):
        name, _, tail = rest.partition(" ")
        field, _, value = tail.partition(" ")
        field = field.strip().lower()
        campaign = self.campaigns.get(name.strip())
        if not campaign:
            return f"No campaign called `{name.strip()}`."

        aliases = {"from": "from_email", "sender": "from_email", "name": "from_name",
                   "reply": "reply_to", "subject": "subject", "body": "body",
                   "html": "is_html", "cap": "daily_cap", "batch": "batch_size"}
        column = aliases.get(field)
        if not column:
            return f"Can't set `{field}`. Try: subject, body, from, name, reply, html, cap, batch."

        value = value.strip() if column not in ("body",) else tail.partition(" ")[2]
        if column in ("daily_cap", "batch_size"):
            if not value.strip().isdigit():
                return f"`{field}` needs a number."
            value = int(value.strip())
        if column == "is_html":
            value = 1 if value.strip().lower() in ("1", "true", "yes", "on") else 0

        self.campaigns.update(name.strip(), **{column: value})
        shown = str(value)
        return f"**{name.strip()}** {field} set" + (f" to {shown}" if len(shown) < 60 else ".")

    def _cmd_arm(self, name: str):
        campaign = self.campaigns.get(name)
        if not campaign:
            return f"No campaign called `{name}`. Say `new {name}` first." if name else \
                   "Say which campaign to arm, e.g. `arm august-outreach`."
        problems = self._campaign_problems(campaign)
        if problems:
            return f"**{name}** can't send yet:\n" + "\n".join(f"- {p}" for p in problems)
        self.campaigns.set_status(name, "sending")
        queued = self.campaigns.queue_size(campaign["id"])
        pace = campaign.get("batch_size") or self.batch_size
        return (f"**{name}** is armed: {queued} queued, {pace} every "
                f"{self.send_interval_hours * 60:.0f}m, up to {self.daily_cap or 'unlimited'}/day"
                f"{' (DRY_RUN is on)' if self.dry_run else ''}.")

    def _cmd_preview(self, name: str):
        campaign = self.campaigns.get(name)
        if not campaign:
            return f"No campaign called `{name}`."
        queue = self.campaigns.queue(campaign["id"], 1)
        if not queue:
            return f"**{name}** has nobody queued."
        rendered = self._render(campaign, queue[0])
        missing = (f"\n\nMissing placeholder values: {', '.join(rendered['missing'])}"
                   if rendered["missing"] else "")
        return {
            "text": (f"**{name} — next up: {rendered['to']}**\n\nSubject: {rendered['subject']}\n\n"
                     f"```\n{rendered['body'][:1500]}\n```{missing}"),
            "preview": rendered,
        }

    # --- state for the interface ---

    def _stats(self) -> dict:
        rows = []
        for campaign in self.campaigns.all():
            rows.append({**campaign, **self.campaigns.stats(campaign["id"])})
        return {
            "provider": self.provider_name,
            "from_email": self.from_email,
            "dry_run": self.dry_run,
            "batch_size": self.batch_size,
            "interval_minutes": round(self.send_interval_hours * 60),
            "daily_cap": self.daily_cap,
            "rate_per_second": self.rate_per_second,
            "sent_today": self._sent_today(),
            "suppressed": self.campaigns.suppressed_count(),
            "source": self.source_kind or "file",
            "source_configured": bool(self.source_dsn and self.source_query) if self.source_kind == "postgres" else True,
            "preflight": self.state.get("preflight") or {},
            "campaigns": rows,
        }

    def _status_text(self) -> str:
        rows = self.campaigns.all()
        lines = [
            f"**Outbound marketing email** · provider **{self.provider_name}**"
            f"{' · DRY_RUN' if self.dry_run else ''}",
            f"- Sent today: {self._sent_today()}/{self.daily_cap or '∞'} · pace {self.batch_size} "
            f"every {self.send_interval_hours * 60:.0f}m at {self.rate_per_second}/sec",
            f"- Suppressed: {self.campaigns.suppressed_count()}",
        ]
        if not rows:
            lines.append("- No campaigns yet.")
        for campaign in rows:
            stats = self.campaigns.stats(campaign["id"])
            lines.append(
                f"- `{campaign['name']}` — **{campaign['status']}** · {stats['queued']} queued · "
                f"{stats['sent']} sent · {stats['failed']} failed"
            )
        problems = (self.state.get("preflight") or {}).get("problems", [])
        if problems:
            lines.append("- **Problems:** " + "; ".join(problems))
        return "\n".join(lines)

    def _preflight_text(self, report: dict) -> str:
        if not report:
            return "Nothing to check yet — set a provider and sender first."
        lines = [f"**{report.get('provider', self.provider_name)} preflight**",
                 f"- Credentials: {report.get('credentials') or 'none found'}"]
        if report.get("identity"):
            lines.append(f"- Verified sender: {report['identity']}"
                         + (f", DKIM {report['dkim']}" if report.get("dkim") else ""))
        if report.get("domains"):
            lines.append("- Domains: " + ", ".join(
                f"{d['name']} ({d['status']})" for d in report["domains"]) or "none")
        if "production_access" in report:
            lines.append(f"- Production access: {'yes' if report['production_access'] else 'NO — sandbox'}")
        quota = report.get("quota") or {}
        if quota:
            lines.append(f"- Quota: {quota.get('sent_24_hour', '?')} of {quota.get('max_24_hour', '?')} "
                         f"in 24h at up to {quota.get('max_per_second', '?')}/sec")
        for note in report.get("notes", []):
            lines.append(f"- Note: {note}")
        for problem in report.get("problems", []):
            lines.append(f"- **Problem:** {problem}")
        if report.get("ok"):
            lines.append("\nReady to send.")
        return "\n".join(lines)


if __name__ == "__main__":
    agent = OutboundMarketingEmailAgent()
    asyncio.run(agent.run())
