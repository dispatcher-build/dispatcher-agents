"""
DispatchKit Records

Generic structured output for agents. Each agent appends records to an
append-only JSONL file in its own data dir — no central schema, no external
services. The platform only knows the envelope; everything agent-specific
lives in `payload`.

Envelope:
    {"id", "ts", "agent_id", "kind", "title", "score", "tags", "dedupe_key", "alert", "payload"}

`alert` is None or one of ALERT_LEVELS. Alert records are also indexed in
`alerts.jsonl` so the platform can count unseen alerts without scanning the
full records file.
"""

import json
import logging
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

RECORDS_FILE = "records.jsonl"
RECORDS_MAX_BYTES = 10 * 1024 * 1024
ALERTS_FILE = "alerts.jsonl"
ALERTS_MAX_BYTES = 1 * 1024 * 1024
ALERT_LEVELS = ("info", "error")


def _records_path(data_dir: Path) -> Path:
    return Path(data_dir) / RECORDS_FILE


def _append_line(path: Path, obj: dict, max_bytes: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size > max_bytes:
        rotated = path.with_name(f"{path.stem}.1{path.suffix}")
        path.replace(rotated)
        logger.info(f"Rotated {path.name} to {rotated}")
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False, default=str) + "\n")
        f.flush()


def append_record(
    data_dir: Path,
    agent_id: str,
    kind: str,
    payload: dict,
    *,
    title: Optional[str] = None,
    score: Optional[float] = None,
    tags: Optional[list] = None,
    dedupe_key: Optional[str] = None,
    alert: Optional[str] = None,
) -> dict:
    """Append one record to the agent's records.jsonl. Returns the full record."""
    if alert is not None and alert not in ALERT_LEVELS:
        raise ValueError(f"alert must be one of {ALERT_LEVELS}, got {alert!r}")

    record = {
        "id": str(uuid.uuid4()),
        "ts": datetime.utcnow().isoformat() + "Z",
        "agent_id": agent_id,
        "kind": kind,
        "title": title,
        "score": score,
        "tags": tags or [],
        "dedupe_key": dedupe_key,
        "alert": alert,
        "payload": payload,
    }

    _append_line(_records_path(data_dir), record, RECORDS_MAX_BYTES)

    if alert is not None:
        index = {k: record[k] for k in ("id", "ts", "kind", "title", "alert")}
        _append_line(Path(data_dir) / ALERTS_FILE, index, ALERTS_MAX_BYTES)

    return record


def read_records(
    data_dir: Path,
    kind: Optional[str] = None,
    limit: Optional[int] = None,
) -> list:
    """Read records newest-first, skipping corrupt/partial lines."""
    path = _records_path(data_dir)
    if not path.exists():
        return []

    records = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if kind is not None and rec.get("kind") != kind:
                continue
            records.append(rec)

    records.reverse()
    if limit is not None:
        records = records[:limit]
    return records
