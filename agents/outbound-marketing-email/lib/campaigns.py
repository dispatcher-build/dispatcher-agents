"""
Campaigns, recipients and the send ledger, in the worker's own SQLite store.

The store is the reason this is a campaign worker rather than a one-shot
blaster: several campaigns can exist at once, each with its own audience,
subject, body and pace, and only the ones you arm actually send. The platform
opens the same file read-only, so the interface reads these tables directly
with dispatcher.query() instead of asking the agent for a summary.

Tables
  campaigns   one row per campaign, with its status and template
  recipients  the audience, tagged by campaign, with the source it came from
  sends       append-only ledger; a row exists before the send is attempted
  suppressed  addresses that must never be contacted again

The duplicate guard is the UNIQUE constraint on sends(campaign_id, email): the
row is inserted before the provider is called, so a crash mid-batch cannot
produce a second email to the same person.
"""

import json
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS campaigns (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL UNIQUE,
    status       TEXT NOT NULL DEFAULT 'draft',
    subject      TEXT NOT NULL DEFAULT '',
    body         TEXT NOT NULL DEFAULT '',
    is_html      INTEGER NOT NULL DEFAULT 0,
    from_email   TEXT NOT NULL DEFAULT '',
    from_name    TEXT NOT NULL DEFAULT '',
    reply_to     TEXT NOT NULL DEFAULT '',
    daily_cap    INTEGER,
    batch_size   INTEGER,
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    armed_at     TEXT,
    finished_at  TEXT
);

CREATE TABLE IF NOT EXISTS recipients (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id INTEGER NOT NULL,
    email       TEXT NOT NULL,
    fields      TEXT NOT NULL DEFAULT '{}',
    source      TEXT NOT NULL DEFAULT 'paste',
    added_at    TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (campaign_id, email)
);
CREATE INDEX IF NOT EXISTS recipients_campaign ON recipients (campaign_id);

CREATE TABLE IF NOT EXISTS sends (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id INTEGER NOT NULL,
    email       TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'claimed',
    detail      TEXT NOT NULL DEFAULT '',
    provider    TEXT NOT NULL DEFAULT '',
    claimed_at  TEXT NOT NULL DEFAULT (datetime('now')),
    sent_at     TEXT,
    UNIQUE (campaign_id, email)
);
CREATE INDEX IF NOT EXISTS sends_campaign ON sends (campaign_id, status);

CREATE TABLE IF NOT EXISTS suppressed (
    email      TEXT PRIMARY KEY,
    reason     TEXT NOT NULL DEFAULT 'manual',
    added_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS rejected (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id INTEGER,
    email       TEXT NOT NULL,
    reason      TEXT NOT NULL,
    added_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

STATUSES = ("draft", "sending", "paused", "done")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Campaigns:
    def __init__(self, store):
        self.store = store
        for statement in SCHEMA.strip().split(";\n"):
            if statement.strip():
                self.store.execute(statement)

    # --- campaigns ---

    def create(self, name: str, **fields) -> dict:
        columns = ("subject", "body", "is_html", "from_email", "from_name",
                   "reply_to", "daily_cap", "batch_size")
        values = {k: fields.get(k) for k in columns if fields.get(k) is not None}
        keys = ", ".join(["name"] + list(values))
        marks = ", ".join(["?"] * (1 + len(values)))
        self.store.execute(
            f"INSERT OR IGNORE INTO campaigns ({keys}) VALUES ({marks})",
            tuple([name] + list(values.values())),
        )
        return self.get(name)

    def get(self, name: str):
        rows = self.store.query("SELECT * FROM campaigns WHERE name = ?", (name,))
        return dict(rows[0]) if rows else None

    def by_id(self, campaign_id: int):
        rows = self.store.query("SELECT * FROM campaigns WHERE id = ?", (campaign_id,))
        return dict(rows[0]) if rows else None

    def all(self) -> list:
        return [dict(r) for r in self.store.query(
            "SELECT * FROM campaigns ORDER BY (status = 'sending') DESC, created_at DESC"
        )]

    def update(self, name: str, **fields) -> None:
        allowed = {"status", "subject", "body", "is_html", "from_email", "from_name",
                   "reply_to", "daily_cap", "batch_size", "armed_at", "finished_at"}
        sets = {k: v for k, v in fields.items() if k in allowed}
        if not sets:
            return
        assignments = ", ".join(f"{k} = ?" for k in sets)
        self.store.execute(
            f"UPDATE campaigns SET {assignments} WHERE name = ?",
            tuple(list(sets.values()) + [name]),
        )

    def set_status(self, name: str, status: str) -> None:
        if status not in STATUSES:
            raise ValueError(f"unknown status '{status}'")
        stamps = {}
        if status == "sending":
            stamps["armed_at"] = _now()
        if status == "done":
            stamps["finished_at"] = _now()
        self.update(name, status=status, **stamps)

    def sending(self) -> list:
        """The campaigns that are actually allowed to send, oldest armed first."""
        return [dict(r) for r in self.store.query(
            "SELECT * FROM campaigns WHERE status = 'sending' ORDER BY armed_at"
        )]

    def delete(self, name: str) -> bool:
        campaign = self.get(name)
        if not campaign:
            return False
        for table in ("recipients", "sends"):
            self.store.execute(f"DELETE FROM {table} WHERE campaign_id = ?", (campaign["id"],))
        self.store.execute("DELETE FROM rejected WHERE campaign_id = ?", (campaign["id"],))
        self.store.execute("DELETE FROM campaigns WHERE id = ?", (campaign["id"],))
        return True

    # --- recipients ---

    def add_recipients(self, campaign_id: int, rows: list, source: str = "paste") -> int:
        if not rows:
            return 0
        payload = [
            (campaign_id, row["email"],
             json.dumps({k: v for k, v in row.items() if k != "email"}, default=str),
             source)
            for row in rows
        ]
        before = self.count_recipients(campaign_id)
        self.store.executemany(
            "INSERT OR IGNORE INTO recipients (campaign_id, email, fields, source) "
            "VALUES (?, ?, ?, ?)",
            payload,
        )
        return self.count_recipients(campaign_id) - before

    def add_rejected(self, campaign_id, rows: list) -> None:
        if not rows:
            return
        self.store.executemany(
            "INSERT INTO rejected (campaign_id, email, reason) VALUES (?, ?, ?)",
            [(campaign_id, r.get("email", ""), r.get("reason", "")) for r in rows],
        )

    def count_recipients(self, campaign_id: int) -> int:
        rows = self.store.query(
            "SELECT COUNT(*) AS n FROM recipients WHERE campaign_id = ?", (campaign_id,)
        )
        return rows[0]["n"] if rows else 0

    def queue(self, campaign_id: int, limit: int) -> list:
        """Recipients with no ledger row and no suppression, oldest first."""
        rows = self.store.query(
            """
            SELECT r.email, r.fields FROM recipients r
            LEFT JOIN sends s ON s.campaign_id = r.campaign_id AND s.email = r.email
            WHERE r.campaign_id = ?
              AND s.id IS NULL
              AND r.email NOT IN (SELECT email FROM suppressed)
            ORDER BY r.id
            LIMIT ?
            """,
            (campaign_id, limit),
        )
        out = []
        for row in rows:
            try:
                fields = json.loads(row["fields"] or "{}")
            except json.JSONDecodeError:
                fields = {}
            fields["email"] = row["email"]
            out.append(fields)
        return out

    def queue_size(self, campaign_id: int) -> int:
        rows = self.store.query(
            """
            SELECT COUNT(*) AS n FROM recipients r
            LEFT JOIN sends s ON s.campaign_id = r.campaign_id AND s.email = r.email
            WHERE r.campaign_id = ? AND s.id IS NULL
              AND r.email NOT IN (SELECT email FROM suppressed)
            """,
            (campaign_id,),
        )
        return rows[0]["n"] if rows else 0

    # --- ledger ---

    def claim(self, campaign_id: int, email: str, provider: str) -> bool:
        """Reserve an address before sending. False means someone already has it."""
        cur = self.store.execute(
            "INSERT OR IGNORE INTO sends (campaign_id, email, status, provider) "
            "VALUES (?, ?, 'claimed', ?)",
            (campaign_id, email, provider),
        )
        return cur.rowcount > 0

    def mark_sent(self, campaign_id: int, email: str, detail: str) -> None:
        self.store.execute(
            "UPDATE sends SET status = 'sent', detail = ?, sent_at = ? "
            "WHERE campaign_id = ? AND email = ?",
            (detail[:500], _now(), campaign_id, email),
        )

    def mark_failed(self, campaign_id: int, email: str, detail: str) -> None:
        self.store.execute(
            "UPDATE sends SET status = 'failed', detail = ? WHERE campaign_id = ? AND email = ?",
            (detail[:500], campaign_id, email),
        )

    def release(self, campaign_id: int, email: str) -> None:
        """Undo a claim so the address is retried once the cause is fixed."""
        self.store.execute(
            "DELETE FROM sends WHERE campaign_id = ? AND email = ? AND status = 'claimed'",
            (campaign_id, email),
        )

    def stats(self, campaign_id: int) -> dict:
        rows = self.store.query(
            "SELECT status, COUNT(*) AS n FROM sends WHERE campaign_id = ? GROUP BY status",
            (campaign_id,),
        )
        counts = {r["status"]: r["n"] for r in rows}
        return {
            "sent": counts.get("sent", 0),
            "failed": counts.get("failed", 0),
            "claimed": counts.get("claimed", 0),
            "recipients": self.count_recipients(campaign_id),
            "queued": self.queue_size(campaign_id),
        }

    def sent_since(self, since_iso: str) -> int:
        rows = self.store.query(
            "SELECT COUNT(*) AS n FROM sends WHERE status = 'sent' AND sent_at >= ?",
            (since_iso,),
        )
        return rows[0]["n"] if rows else 0

    # --- suppression ---

    def suppress(self, emails, reason: str = "manual") -> int:
        rows = [(e.strip().lower(), reason) for e in emails if str(e).strip()]
        if not rows:
            return 0
        before = self.suppressed_count()
        self.store.executemany(
            "INSERT OR IGNORE INTO suppressed (email, reason) VALUES (?, ?)", rows
        )
        return self.suppressed_count() - before

    def is_suppressed(self, email: str) -> bool:
        rows = self.store.query("SELECT 1 FROM suppressed WHERE email = ?", (email.lower(),))
        return bool(rows)

    def suppressed_count(self) -> int:
        rows = self.store.query("SELECT COUNT(*) AS n FROM suppressed")
        return rows[0]["n"] if rows else 0
