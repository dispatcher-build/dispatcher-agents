"""
Where recipients come from, and what makes one sendable.

Four sources, all landing in the same shape (a dict with a lowercase 'email'
plus whatever other columns came with it, available as {{placeholders}}):

  file      a CSV/JSONL/JSON/text file in the agent directory
  paste     addresses pasted into the worker's interface
  postgres  any query against any database you configure
  store     rows already in the worker's own SQLite store

Validation is deliberately strict: a bounce costs sending reputation, so a bad
address is cheaper to drop here than to send and regret.
"""

import csv
import json
import re
from pathlib import Path

EMAIL_COLUMNS = ("email", "email_address", "e-mail", "mail", "address", "to")

EMAIL_RE = re.compile(r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$")
NAME_ADDR_RE = re.compile(r"^\s*(?P<name>[^<>]*?)\s*<(?P<email>[^<>@\s]+@[^<>@\s]+)>\s*$")
PLACEHOLDER_RE = re.compile(r"\{\{\s*([\w_]+)\s*\}\}")
SPLIT_RE = re.compile(r"[\s,;]+")

DISPOSABLE_DOMAINS = {
    "10minutemail.com", "mailinator.com", "dispostable.com", "guerrillamail.com",
    "spamgourmet.com", "trashmail.com", "yopmail.com", "temp-mail.org",
    "throwaway.email", "maildrop.cc", "getnada.com", "tempail.com",
    "sharklasers.com", "grr.la", "guerrillamailblock.com", "pokemail.net",
    "spam4.me", "bccto.me", "chacuo.net", "discard.email", "fakemail.net",
    "hidemail.de", "mytrashmail.com", "no-spam.ws", "nowmymail.com",
    "sneakemail.com", "spambog.com", "tempinbox.com", "tempymail.com",
    "trbvm.com", "trbvn.com", "byom.de", "jnxjn.com", "drdrb.net",
}

TYPO_DOMAINS = {
    "gmial.com", "gmai.com", "gmale.com", "gmali.com", "yahooo.com", "yahho.com",
    "hotmial.com", "hotmall.com", "outlok.com", "outlok.co",
}

UNDELIVERABLE_LOCAL_RE = re.compile(
    r"^(test\d*|fake\d*|dummy\d*|noreply|no-reply|example|sample|temp\d*|null|void|nobody|placeholder)$"
)


# --- validation and rendering ------------------------------------------------

def validate(email: str) -> tuple:
    """(ok, reason). Format, disposable domains, provider typos, role mailboxes."""
    if not email or not EMAIL_RE.match(email):
        return False, "invalid address format"
    local, _, domain = email.partition("@")
    if domain in DISPOSABLE_DOMAINS:
        return False, "disposable domain"
    if domain in TYPO_DOMAINS:
        return False, "misspelled provider domain"
    if UNDELIVERABLE_LOCAL_RE.match(local):
        return False, "placeholder mailbox"
    return True, ""


def render(template: str, row: dict) -> str:
    def replace(match):
        value = row.get(match.group(1))
        return "" if value is None else str(value)

    return PLACEHOLDER_RE.sub(replace, template)


def placeholders(template: str) -> list:
    return sorted(set(PLACEHOLDER_RE.findall(template or "")))


def missing_placeholders(template: str, row: dict) -> list:
    return sorted({k for k in PLACEHOLDER_RE.findall(template or "") if not str(row.get(k) or "").strip()})


# --- normalisation -----------------------------------------------------------

def _email_of(row: dict) -> str:
    for key in row:
        if str(key).lower().replace(" ", "_") in EMAIL_COLUMNS:
            value = str(row[key] or "").strip()
            if value:
                return value
    # Fall back to any column that looks like an address (postgres queries often
    # name the column something else entirely).
    for value in row.values():
        text = str(value or "").strip()
        if EMAIL_RE.match(text):
            return text
    return ""


def normalise(rows) -> tuple:
    """Return (clean_rows, rejected) with duplicates and bad addresses removed."""
    clean, rejected, seen = [], [], set()
    for row in rows:
        if not isinstance(row, dict):
            row = {"email": str(row)}
        email = _email_of(row).lower()
        if not email:
            rejected.append({"email": "", "reason": "no email column"})
            continue
        if email in seen:
            continue
        seen.add(email)
        ok, reason = validate(email)
        row = {str(k): v for k, v in row.items()}
        row["email"] = email
        (clean if ok else rejected).append(row if ok else {"email": email, "reason": reason})
    return clean, rejected


# --- the four sources --------------------------------------------------------

def from_text(text: str) -> list:
    """Addresses pasted into the interface: one per line, CSV, or 'Name <a@b.c>'."""
    rows = []
    lines = [l.strip() for l in (text or "").splitlines() if l.strip()]
    if not lines:
        return rows

    header = lines[0]
    if "," in header and "@" not in header:
        reader = csv.DictReader(lines)
        return [dict(r) for r in reader]

    for line in lines:
        if line.startswith("#"):
            continue
        match = NAME_ADDR_RE.match(line)
        if match:
            rows.append({"email": match.group("email"), "name": match.group("name")})
            continue
        for token in SPLIT_RE.split(line):
            if token:
                rows.append({"email": token})
    return rows


def from_file(path: Path) -> tuple:
    """CSV, JSONL, JSON array, or one address per line."""
    if not path.exists():
        return [], [f"file not found: {path}"]

    problems = []
    text = path.read_text(encoding="utf-8", errors="replace")
    suffix = path.suffix.lower()

    if suffix == ".csv":
        return [dict(r) for r in csv.DictReader(text.splitlines())], problems
    if suffix == ".jsonl":
        rows = []
        for lineno, line in enumerate(text.splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as e:
                problems.append(f"line {lineno} is not valid JSON: {e}")
        return rows, problems
    if suffix == ".json":
        try:
            parsed = json.loads(text or "[]")
        except json.JSONDecodeError as e:
            return [], [f"file is not valid JSON: {e}"]
        return (parsed if isinstance(parsed, list) else parsed.get("recipients", [])), problems

    return from_text(text), problems


def from_postgres(dsn: str, query: str, limit: int = 50000) -> tuple:
    """Any query against any database the user configures.

    This is the atlas-email path: the addresses already live in a table
    (researcher_emails, github_profiles, whatever) and the campaign should pull
    from it rather than from a file someone exported by hand.
    """
    if not dsn:
        return [], ["no database configured (set SOURCE_DSN)"]
    if not query:
        return [], ["no query configured (set SOURCE_QUERY)"]
    if not _is_read_only(query):
        return [], ["SOURCE_QUERY must be a single read-only SELECT/WITH statement"]

    try:
        import psycopg2
        from psycopg2.extras import RealDictCursor
    except ImportError:
        return [], ["psycopg2 is not installed in this image"]

    try:
        conn = psycopg2.connect(dsn, connect_timeout=15, cursor_factory=RealDictCursor)
    except Exception as e:
        return [], [f"could not connect: {e}"]

    try:
        with conn, conn.cursor() as cur:
            cur.execute(f"SELECT * FROM ({query.rstrip(';')}) AS source LIMIT {int(limit)}")
            return [dict(r) for r in cur.fetchall()], []
    except Exception as e:
        return [], [f"query failed: {e}"]
    finally:
        conn.close()


def _is_read_only(sql: str) -> bool:
    stripped = re.sub(r"--[^\n]*", " ", sql or "")
    stripped = re.sub(r"/\*.*?\*/", " ", stripped, flags=re.S).strip()
    if ";" in stripped.rstrip(";"):
        return False
    return bool(re.match(r"^(select|with)\b", stripped, re.I))
