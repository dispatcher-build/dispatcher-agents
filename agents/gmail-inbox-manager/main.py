"""
Gmail Inbox Manager — DispatchKit Agent

Keeps a Gmail inbox clean and surfaces what matters:

- labels: on first contact, Dispatcher/<Category> labels are created once and
  cached
- triage: every TRIAGE_EVERY_MINUTES (or on demand via chat), new inbox mail is
  classified into Action needed / Waiting on them / FYI / Newsletter /
  Notification / Receipt / Promotion in one LLM call (heuristics when the LLM
  is off or over budget), then labelled, starred and archived according to the
  autonomy level: observe reports only, organise labels and stars, autopilot
  also sweeps ARCHIVE_CATEGORIES out of the inbox

Records: kind="triage" per batch (markdown summary, counts, items),
kind="flag" for urgent or VIP action items (alert level info), kind="undo".
Every batch is reversible with `undo <batch>`. The worker never deletes,
never sends, and never changes read state. Needs the user's own Google OAuth
client (GMAIL_CLIENT_ID / GMAIL_CLIENT_SECRET / GMAIL_REFRESH_TOKEN, minted
with lib/gmail_auth.py); without them it boots, explains itself, and idles.
"""

import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent / "lib"))

from dispatchkit import Agent, llm, phase, util

import triage
from gmail import GmailAuthError, GmailClient, GmailError

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

CREDENTIAL_KEYS = ("GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET", "GMAIL_REFRESH_TOKEN")
SETUP_HINT = (
    "Create a Desktop OAuth client in console.cloud.google.com with the Gmail API enabled, "
    "run `python lib/gmail_auth.py --client-id ... --client-secret ...` on your machine, "
    "and paste the printed values here."
)
SCHEMA = [
    "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL)",
    """CREATE TABLE IF NOT EXISTS triage (
        message_id TEXT PRIMARY KEY, thread_id TEXT, "from" TEXT, subject TEXT, snippet TEXT,
        category TEXT, urgent INTEGER DEFAULT 0, vip INTEGER DEFAULT 0, reason TEXT,
        actions_json TEXT NOT NULL, status TEXT NOT NULL, batch_id TEXT NOT NULL,
        ts TEXT NOT NULL, undone_ts TEXT)""",
    "CREATE INDEX IF NOT EXISTS triage_batch ON triage(batch_id)",
    """CREATE TABLE IF NOT EXISTS batches (
        batch_id TEXT PRIMARY KEY, ts TEXT, autonomy TEXT, total INTEGER, archived INTEGER,
        starred INTEGER, flagged INTEGER, classifier TEXT, undone_ts TEXT)""",
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _split_list(raw: str) -> list:
    return [x.strip() for x in (raw or "").split(",") if x.strip()]


class GmailInboxManager(Agent):
    def __init__(self):
        super().__init__()
        self.gmail: Optional[GmailClient] = None
        self.configured = False
        self.triage_minutes = 15.0
        self.max_per_run = 50
        self.label_prefix = "Dispatcher"
        self.dry_run = False
        self.env_defaults: dict = {}

    async def setup(self):
        self.triage_minutes = float(os.environ.get("TRIAGE_EVERY_MINUTES", "15"))
        self.max_per_run = int(os.environ.get("MAX_MESSAGES_PER_RUN", "50"))
        self.label_prefix = os.environ.get("LABEL_PREFIX", "Dispatcher").strip() or "Dispatcher"
        self.dry_run = os.environ.get("DRY_RUN", "0") == "1"
        self.env_defaults = {
            "autonomy": os.environ.get("AUTONOMY", "organise").strip().lower().replace("organize", "organise"),
            "vip_senders": _split_list(os.environ.get("VIP_SENDERS", "")),
            "archive_categories": _split_list(os.environ.get("ARCHIVE_CATEGORIES", "Newsletter,Notification,Promotion")),
            "rules": [],
        }
        for statement in SCHEMA:
            self.store.execute(statement)

        creds = {k: os.environ.get(k, "").strip() for k in CREDENTIAL_KEYS}
        missing = [k for k, v in creds.items() if not v]
        if missing:
            self.configured = False
            self.needs_setup(missing, SETUP_HINT)
        else:
            self.configured = True
            self.clear_needs_setup()
            self.gmail = GmailClient(creds["GMAIL_CLIENT_ID"], creds["GMAIL_CLIENT_SECRET"], creds["GMAIL_REFRESH_TOKEN"])
        settings = self._settings()
        logger.info(
            f"Inbox manager ready | configured={self.configured} | autonomy={settings['autonomy']} | "
            f"every {self.triage_minutes}m | up to {self.max_per_run}/run | dry_run={self.dry_run}"
        )

    # --- settings ---

    def _settings(self) -> dict:
        effective = {k: (list(v) if isinstance(v, list) else v) for k, v in self.env_defaults.items()}
        for row in self.store.query("SELECT key, value FROM settings"):
            key, raw = row["key"], row["value"]
            if key in ("vip_senders", "archive_categories", "rules"):
                try:
                    effective[key] = json.loads(raw)
                except json.JSONDecodeError:
                    continue
            else:
                effective[key] = raw
        if effective["autonomy"] not in triage.AUTONOMY_LEVELS:
            effective["autonomy"] = "organise"
        return effective

    def _set_setting(self, key: str, value):
        raw = json.dumps(value) if isinstance(value, list) else str(value)
        self.store.execute(
            "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (key, raw, _now()),
        )

    def _reset_setting(self, key: str):
        self.store.execute("DELETE FROM settings WHERE key = ?", (key,))

    # --- gmail helpers ---

    def _labels(self) -> dict:
        cached = self.store.get("labels") or {}
        if all(cached.get(c) for c in triage.CATEGORIES):
            return cached
        ids = self.gmail.ensure_labels(self.label_prefix, triage.CATEGORIES)
        self.store.set("labels", ids)
        return ids

    def _me(self) -> str:
        me = self.store.get("me")
        if not me:
            me = self.gmail.profile().get("emailAddress", "")
            self.store.set("me", me)
        return me

    def _query(self, state: dict) -> str:
        last = state.get("last_triage")
        if not last:
            return "in:inbox newer_than:1d"
        try:
            since = datetime.fromisoformat(str(last).rstrip("Z")).replace(tzinfo=timezone.utc) - timedelta(hours=1)
        except ValueError:
            return "in:inbox newer_than:1d"
        return f"in:inbox after:{int(since.timestamp())}"

    async def _handle_auth_error(self, error: Exception):
        logger.error(f"Gmail rejected the refresh token: {error}")
        self.needs_setup(["GMAIL_REFRESH_TOKEN"], "Google rejected the refresh token. " + SETUP_HINT)
        self.configured = False
        if not self.has_recent_record("auth", within_hours=24):
            self.alert(
                "Gmail token rejected — re-run lib/gmail_auth.py and paste a new refresh token",
                {"error": str(error)[:300]},
                level="error",
                dedupe_key="auth",
            )

    # --- triage ---

    @phase("triage", when=lambda a: a.configured and bool(
        a.state.get("triage_requested") or util.due(a.state, "last_triage", a.triage_minutes / 60)))
    async def triage(self) -> dict:
        state = self.state
        settings = self._settings()
        autonomy = settings["autonomy"]
        vips = settings["vip_senders"]
        rules = settings["rules"]
        archive = set(settings["archive_categories"])

        try:
            labels = await asyncio.to_thread(self._labels)
            me = await asyncio.to_thread(self._me)
            ids = await asyncio.to_thread(self.gmail.list_message_ids, self._query(state), self.max_per_run * 3)
        except GmailAuthError as e:
            await self._handle_auth_error(e)
            return {"result": "auth_error"}
        except GmailError as e:
            logger.warning(f"Gmail unavailable: {e}")
            return {"result": "gmail_error"}

        fresh_ids = [i for i in ids if not self.store.has_seen(f"msg:{i}")][: self.max_per_run]
        if not fresh_ids:
            state["triage_requested"] = False
            state["last_result"] = "nothing_new"
            return {"result": "nothing_new"}

        messages = []
        for message_id in fresh_ids:
            try:
                messages.append(await asyncio.to_thread(self.gmail.get_message, message_id))
            except GmailAuthError as e:
                await self._handle_auth_error(e)
                return {"result": "auth_error"}
            except GmailError as e:
                logger.warning(f"Skipping {message_id}: {e}")
        if not messages:
            return {"result": "fetch_failed"}

        stub = triage.stub_classify(messages, vips, rules, me)
        classifier = "heuristic"
        if llm.enabled():
            system, prompt = triage.build_prompt(messages, vips, rules, me)
            try:
                data = await asyncio.to_thread(
                    llm.ask_json, prompt, system=system, max_tokens=3000,
                    data_dir=self.config.data_dir, stub=stub,
                )
                if data and data.get("results"):
                    classifier = "llm"
                else:
                    data = stub
            except llm.LLMBudgetExceeded:
                logger.warning("LLM budget exhausted; using heuristics")
                data = stub
        else:
            data = stub
        parsed = triage.parse_response(data, len(messages))
        results = parsed["results"]
        for r in results:
            msg = messages[r["i"]]
            forced = triage.apply_rules(msg, rules)
            if forced:
                r["category"] = forced
                r["reason"] = "matched a rule"
            from_me = "from-me" in triage.hints_for(msg, me) or "sent" in triage.hints_for(msg, me)
            r["vip"] = triage.is_vip(msg, vips) and not from_me
            if r["vip"] and r["category"] == "FYI":
                r["category"] = "Action needed"

        batch_id = "b-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        applied = {"archived": 0, "starred": 0, "failed": 0}
        flagged = 0
        for r in results:
            msg = messages[r["i"]]
            actions = triage.plan_actions(r["category"], r["urgent"], autonomy, archive, labels)
            status = await self._apply(batch_id, msg, r, actions)
            if status == "applied":
                applied["archived"] += int("INBOX" in actions["remove"])
                applied["starred"] += int("STARRED" in actions["add"])
            elif status == "failed":
                applied["failed"] += 1
            self.store.add_seen(f"msg:{msg['id']}")
            if r["category"] == "Action needed" and (r["urgent"] or r["vip"]):
                flagged += self._flag(msg, r, batch_id)

        counts = {}
        for r in results:
            counts[r["category"]] = counts.get(r["category"], 0) + 1
        summary = parsed["summary_md"].strip() or triage.summary_md(results, messages, autonomy, applied)
        self.store.execute(
            "INSERT OR REPLACE INTO batches (batch_id, ts, autonomy, total, archived, starred, flagged, classifier) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (batch_id, _now(), autonomy, len(messages), applied["archived"], applied["starred"], flagged, classifier),
        )
        action_count = counts.get("Action needed", 0)
        record = self.record(
            "triage",
            payload={
                "batch_id": batch_id,
                "autonomy": autonomy,
                "counts": counts,
                "archived": applied["archived"],
                "starred": applied["starred"],
                "flagged": flagged,
                "failed": applied["failed"],
                "classifier": classifier,
                "dry_run": self.dry_run,
                "summary_md": summary,
                "items": [
                    {
                        "id": messages[r["i"]]["id"],
                        "from": messages[r["i"]]["from"],
                        "subject": messages[r["i"]]["subject"],
                        "category": r["category"],
                        "urgent": r["urgent"],
                        "vip": r["vip"],
                    }
                    for r in results
                ],
            },
            title=f"Triage: {len(messages)} message(s), {action_count} need action",
            tags=["triage", autonomy],
            dedupe_key=batch_id,
        )
        await self.emit("inbox.triage", {"record_id": record["id"], "batch_id": batch_id, "total": len(messages)})

        state["triage_requested"] = False
        state["last_batch_id"] = batch_id
        state["last_result"] = f"{len(messages)} triaged"
        totals = state.setdefault("totals", {"messages": 0, "archived": 0, "starred": 0, "flagged": 0, "batches": 0})
        totals["messages"] += len(messages)
        totals["archived"] += applied["archived"]
        totals["starred"] += applied["starred"]
        totals["flagged"] += flagged
        totals["batches"] += 1
        logger.info(f"Triage {batch_id}: {len(messages)} messages, {counts}, archived {applied['archived']}, flagged {flagged}")
        return {"result": "triaged", "batch_id": batch_id, "total": len(messages)}

    async def _apply(self, batch_id: str, msg: dict, result: dict, actions: dict) -> str:
        if not actions["add"] and not actions["remove"]:
            status = "observed"
        elif self.dry_run:
            status = "dry_run"
        else:
            try:
                await asyncio.to_thread(self.gmail.modify, msg["id"], actions["add"], actions["remove"])
                status = "applied"
            except GmailAuthError as e:
                await self._handle_auth_error(e)
                status = "failed"
            except GmailError as e:
                logger.warning(f"modify failed for {msg['id']}: {e}")
                status = "failed"
        self.store.execute(
            'INSERT OR REPLACE INTO triage (message_id, thread_id, "from", subject, snippet, category, urgent, vip, '
            "reason, actions_json, status, batch_id, ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                msg["id"], msg["thread_id"], msg["from"], msg["subject"], msg["snippet"][:300],
                result["category"], int(result["urgent"]), int(result["vip"]), result.get("reason", ""),
                json.dumps(actions if status == "applied" else {"add": [], "remove": []}),
                status, batch_id, _now(),
            ),
        )
        return status

    def _flag(self, msg: dict, result: dict, batch_id: str) -> int:
        key = f"flag:{msg['id']}"
        if self.has_recent_record(key, within_hours=720):
            return 0
        why = "VIP" if result["vip"] else "urgent"
        score = 80.0 if (result["vip"] and result["urgent"]) else 60.0
        self.record(
            "flag",
            payload={
                "message_id": msg["id"],
                "thread_id": msg["thread_id"],
                "from": msg["from"],
                "subject": msg["subject"],
                "snippet": msg["snippet"][:300],
                "reason": result.get("reason", ""),
                "why": why,
                "url": f"https://mail.google.com/mail/u/0/#inbox/{msg['thread_id']}",
                "batch_id": batch_id,
            },
            title=f"Action needed: {msg['subject'][:80]} — {msg['from_email'] or msg['from']}",
            score=score,
            tags=["flag", why],
            dedupe_key=key,
            alert="info",
        )
        return 1

    # --- undo ---

    async def _undo(self, batch_id: str) -> str:
        rows = self.store.query("SELECT message_id, actions_json, status FROM triage WHERE batch_id = ?", (batch_id,))
        if not rows:
            return f"No batch {batch_id}."
        restored = failed = skipped = 0
        for row in rows:
            if row["status"] != "applied":
                skipped += 1
                continue
            actions = json.loads(row["actions_json"])
            try:
                await asyncio.to_thread(self.gmail.modify, row["message_id"], actions["remove"], actions["add"])
                self.store.execute("UPDATE triage SET status = 'undone', undone_ts = ? WHERE message_id = ?", (_now(), row["message_id"]))
                restored += 1
            except GmailAuthError as e:
                await self._handle_auth_error(e)
                return f"Undo stopped: Gmail rejected the token after {restored} message(s)."
            except GmailError as e:
                logger.warning(f"undo failed for {row['message_id']}: {e}")
                failed += 1
        self.store.execute("UPDATE batches SET undone_ts = ? WHERE batch_id = ?", (_now(), batch_id))
        self.record(
            "undo",
            payload={"batch_id": batch_id, "restored": restored, "failed": failed, "skipped": skipped},
            title=f"Undid batch {batch_id}: {restored} message(s) restored",
            tags=["undo"],
            dedupe_key=f"undo:{batch_id}",
        )
        return f"Undid {batch_id}: {restored} restored, {failed} failed, {skipped} had nothing to undo."

    # --- chat ---

    async def on_command(self, command: dict):
        text = command.get("text", "").strip()
        lower = text.lower()
        state = self.state
        settings = self._settings()

        if lower == "status":
            totals = state.get("totals", {})
            lines = [
                f"- configured: {'yes' if self.configured else 'no (see Config tab)'}",
                f"- autonomy: {settings['autonomy']}",
                f"- archive categories: {', '.join(settings['archive_categories']) or 'none'}",
                f"- VIP senders: {', '.join(settings['vip_senders']) or 'none'}",
                f"- rules: {len(settings['rules'])}",
                f"- triage every {self.triage_minutes:g} min, up to {self.max_per_run} messages",
                f"- last triage: {state.get('last_triage', 'never')} ({state.get('last_result', 'n/a')})",
                f"- totals: {totals.get('messages', 0)} messages in {totals.get('batches', 0)} batches, "
                f"{totals.get('archived', 0)} archived, {totals.get('starred', 0)} starred, {totals.get('flagged', 0)} flagged",
            ]
            return "\n".join(lines)

        if lower in ("triage now", "triage"):
            if not self.configured:
                return "Not configured yet. " + SETUP_HINT
            state["triage_requested"] = True
            self.save_state(state)
            return "Triage queued; it runs on the next tick."

        if lower == "settings":
            return {
                **settings,
                "label_prefix": self.label_prefix,
                "triage_every_minutes": self.triage_minutes,
                "max_messages_per_run": self.max_per_run,
                "last_triage": state.get("last_triage"),
                "last_batch_id": state.get("last_batch_id"),
                "configured": self.configured,
            }

        if lower.startswith("set autonomy "):
            mode = lower[len("set autonomy "):].strip().replace("organize", "organise")
            if mode == "default":
                self._reset_setting("autonomy")
                return f"Autonomy back to the Config tab value: {self._settings()['autonomy']}."
            if mode not in triage.AUTONOMY_LEVELS:
                return "Autonomy must be observe, organise or autopilot."
            self._set_setting("autonomy", mode)
            return f"Autonomy set to {mode}."

        if lower.startswith("set archive "):
            raw = text[len("set archive "):].strip()
            if raw.lower() == "default":
                self._reset_setting("archive_categories")
                return f"Archive categories back to: {', '.join(self._settings()['archive_categories']) or 'none'}."
            if raw.lower() == "none":
                self._set_setting("archive_categories", [])
                return "Autopilot will archive nothing."
            wanted = []
            for name in _split_list(raw):
                match = next((c for c in triage.CATEGORIES if c.lower() == name.lower()), None)
                if not match:
                    return f"Unknown category {name!r}. Choose from: {', '.join(triage.CATEGORIES)}."
                wanted.append(match)
            self._set_setting("archive_categories", wanted)
            return f"Autopilot will archive: {', '.join(wanted)}."

        if lower.startswith("vip "):
            rest = text[4:].strip()
            vips = list(settings["vip_senders"])
            if rest.lower() == "list":
                return "VIP senders: " + (", ".join(vips) or "none")
            if rest.lower().startswith("add "):
                entry = rest[4:].strip().lower()
                if not entry or " " in entry:
                    return "Give one email address or domain."
                if entry not in vips:
                    vips.append(entry)
                self._set_setting("vip_senders", vips)
                return f"VIP senders: {', '.join(vips)}"
            if rest.lower().startswith("remove "):
                entry = rest[7:].strip().lower()
                vips = [v for v in vips if v != entry]
                self._set_setting("vip_senders", vips)
                return f"VIP senders: {', '.join(vips) or 'none'}"
            return "Use `vip add <email or domain>`, `vip remove <x>` or `vip list`."

        if lower.startswith("rule "):
            rest = text[5:].strip()
            rules = list(settings["rules"])
            if rest.lower() == "list":
                if not rules:
                    return "No rules."
                return "\n".join(f"{i + 1}. {r['match']}:{r['pattern']} -> {r['category']}" for i, r in enumerate(rules))
            if rest.lower().startswith("remove "):
                try:
                    index = int(rest[7:].strip()) - 1
                    removed = rules.pop(index)
                except (ValueError, IndexError):
                    return "Give the rule number from `rule list`."
                self._set_setting("rules", rules)
                return f"Removed rule {removed['match']}:{removed['pattern']} -> {removed['category']}."
            if rest.lower().startswith("add "):
                spec = rest[4:]
                if "->" not in spec or ":" not in spec.split("->", 1)[0]:
                    return "Format: `rule add from:<text> -> <Category>` or `rule add subject:<text> -> <Category>`."
                left, category_raw = (s.strip() for s in spec.split("->", 1))
                match, pattern = (s.strip() for s in left.split(":", 1))
                category = next((c for c in triage.CATEGORIES if c.lower() == category_raw.lower()), None)
                if match.lower() not in ("from", "subject") or not pattern or not category:
                    return f"Match must be from or subject and category one of: {', '.join(triage.CATEGORIES)}."
                rules.append({"match": match.lower(), "pattern": pattern, "category": category})
                self._set_setting("rules", rules)
                return f"Rule {len(rules)} added: {match.lower()}:{pattern} -> {category}."
            return "Use `rule add ...`, `rule remove <n>` or `rule list`."

        if lower.startswith("undo"):
            target = text[4:].strip()
            if not target or target.lower() == "last":
                target = state.get("last_batch_id", "")
                if not target:
                    return "Nothing to undo yet."
            if not self.configured:
                return "Not configured; cannot reach Gmail to undo."
            return await self._undo(target)

        return await super().on_command(command)


if __name__ == "__main__":
    asyncio.run(GmailInboxManager().run())
