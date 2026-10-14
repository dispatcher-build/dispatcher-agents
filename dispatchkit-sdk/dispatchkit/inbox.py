"""
File-based command inbox, symmetric to records: the platform backend appends
command envelopes to data/inbox.jsonl (it is the only writer); the agent reads
new lines from a byte-offset cursor and replies via records (kind="reply").

Command envelope:
    {"id", "ts", "type": "command", "text", "source", "meta"}
"""

import json
import logging
from pathlib import Path
from typing import Tuple

logger = logging.getLogger(__name__)

INBOX_FILE = "inbox.jsonl"
CURSOR_FILE = ".inbox_cursor"
SEEN_IDS_MAX = 200


def _cursor_path(data_dir: Path) -> Path:
    return Path(data_dir) / CURSOR_FILE


def load_cursor(data_dir: Path) -> dict:
    path = _cursor_path(data_dir)
    if path.exists():
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            pass
    return {"offset": 0, "seen_ids": []}


def commit_cursor(data_dir: Path, offset: int, seen_ids: list) -> None:
    path = _cursor_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"offset": offset, "seen_ids": seen_ids[-SEEN_IDS_MAX:]}))


def read_new_commands(data_dir: Path) -> Tuple[list, int, list]:
    """
    Read unprocessed commands. Returns (commands, end_offset, seen_ids).
    Each command carries "_offset" (the byte offset just past its line) so
    callers can commit the cursor after handling each command individually —
    a crash mid-batch then resumes at the first unhandled command. Handles
    rotation (offset past EOF resets to 0; seen_ids dedupes re-reads) and
    stops before a partial trailing line.
    """
    inbox_path = Path(data_dir) / INBOX_FILE
    cursor = load_cursor(data_dir)
    offset = cursor["offset"]
    seen_ids = cursor["seen_ids"]

    if not inbox_path.exists():
        return [], offset, seen_ids

    size = inbox_path.stat().st_size
    if offset > size:
        offset = 0
    if offset == size:
        return [], offset, seen_ids

    commands = []
    with open(inbox_path, "r", encoding="utf-8") as f:
        f.seek(offset)
        while True:
            line_start = f.tell()
            line = f.readline()
            if not line:
                break
            if not line.endswith("\n"):
                offset = line_start
                break
            offset = f.tell()
            line = line.strip()
            if not line:
                continue
            try:
                cmd = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not cmd.get("id") or cmd["id"] in seen_ids:
                continue
            cmd["_offset"] = offset
            commands.append(cmd)

    return commands, offset, seen_ids
