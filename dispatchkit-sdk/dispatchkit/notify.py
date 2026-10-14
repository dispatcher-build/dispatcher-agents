"""
DispatchKit Notify: push records out of the canvas.

Records land in data/records.jsonl and render in the Records tab. That is a
pull surface — fine for digests, useless for alerts. 

This module gives every agent a push surface with zero code: 

configure a destination and matching
records are delivered as they are written.

Transports (both optional, both off unless configured):
- NOTIFY_WEBHOOK_URL  -> HTTP POST. Slack and Discord webhook URLs are
                         detected and given their native payload shape;
                         anything else receives the full record envelope.
- NOTIFY_EMAIL_TO     -> SMTP, comma-separated recipients.

Filtering:
- NOTIFY_ON        comma-separated record kinds, or "*" for all. Empty
                   disables delivery. `reply` is never delivered (chat
                   replies are already a pull surface).
- NOTIFY_MIN_SCORE optional float; records scoring below it are skipped.

Guardrails, mirroring llm.py:
- NOTIFY_DISABLED=1     -> never touch the network (offline testing)
- NOTIFY_MAX_PER_DAY    -> daily cap persisted in data_dir/notify_usage.json
                           (default 200, 0 = unlimited)
- NOTIFY_TIMEOUT_SECONDS / NOTIFY_MAX_BODY_CHARS

Delivery runs on a background daemon thread so a slow or wedged endpoint can
never block the tick loop. Every attempt is appended to notify_log.jsonl.
Stdlib only — this must work in a bare `python3 main.py` run.
"""

import json
import logging
import os
import queue
import smtplib
import threading
import time
import urllib.error
import urllib.request

from . import netsafe
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

USAGE_FILE = "notify_usage.json"
DELIVERY_LOG_FILE = "notify_log.jsonl"

NEVER_NOTIFY_KINDS = {"reply"}
QUEUE_MAX = 256
MAX_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = (1.0, 4.0)

_queue: Optional[queue.Queue] = None
_worker: Optional[threading.Thread] = None
_worker_lock = threading.Lock()
_inflight = threading.Event()


class NotifyBudgetExceeded(RuntimeError):
    pass


def enabled() -> bool:
    return os.environ.get("NOTIFY_DISABLED", "0") != "1"


def _data_dir(data_dir: Optional[Path]) -> Path:
    return Path(data_dir or os.environ.get("AGENT_DATA_DIR", "./data"))


def _webhook_url() -> str:
    return os.environ.get("NOTIFY_WEBHOOK_URL", "").strip()


def _email_recipients() -> list:
    raw = os.environ.get("NOTIFY_EMAIL_TO", "")
    return [addr.strip() for addr in raw.split(",") if addr.strip()]


def configured() -> bool:
    """True if any transport has a destination set."""
    return bool(_webhook_url() or (_email_recipients() and os.environ.get("SMTP_HOST", "").strip()))


def should_notify(record: dict) -> bool:
    """Pure filter: does this record match the configured kinds and score floor?"""
    kind = record.get("kind") or ""
    if kind in NEVER_NOTIFY_KINDS:
        return False

    raw_kinds = os.environ.get("NOTIFY_ON", "").strip()
    if not raw_kinds:
        return False
    if raw_kinds != "*":
        wanted = {k.strip() for k in raw_kinds.split(",") if k.strip()}
        if kind not in wanted:
            return False

    floor = os.environ.get("NOTIFY_MIN_SCORE", "").strip()
    if floor:
        try:
            if float(record.get("score") or 0) < float(floor):
                return False
        except (TypeError, ValueError):
            pass

    return True


def _read_usage(data_dir: Path) -> dict:
    usage_path = data_dir / USAGE_FILE
    today = datetime.utcnow().strftime("%Y-%m-%d")
    usage = {"date": today, "sent": 0}
    if usage_path.exists():
        try:
            usage = json.loads(usage_path.read_text())
        except json.JSONDecodeError:
            pass
    if usage.get("date") != today:
        usage = {"date": today, "sent": 0}
    return usage


def _check_budget(data_dir: Path) -> None:
    max_per_day = int(os.environ.get("NOTIFY_MAX_PER_DAY", "200"))
    if max_per_day <= 0:
        return
    usage = _read_usage(data_dir)
    if usage["sent"] >= max_per_day:
        raise NotifyBudgetExceeded(f"notify daily cap reached ({usage['sent']}/{max_per_day})")


def _record_sent(data_dir: Path) -> None:
    """Count against the daily cap only when a transport actually delivered —
    a dead webhook or full queue must not eat the day's budget.
    """
    usage = _read_usage(data_dir)
    usage["sent"] += 1
    usage_path = data_dir / USAGE_FILE
    usage_path.parent.mkdir(parents=True, exist_ok=True)
    usage_path.write_text(json.dumps(usage))


def _summarize(record: dict) -> tuple:
    """Render a record as (subject, body). Payload keys ending in _md win."""
    max_chars = int(os.environ.get("NOTIFY_MAX_BODY_CHARS", "1500"))
    kind = record.get("kind") or "record"
    subject = record.get("title") or kind

    payload = record.get("payload") or {}
    md_keys = [k for k in payload if k.endswith("_md")]
    lines = []
    if md_keys:
        lines = [str(payload[k]) for k in md_keys]
    else:
        for key, value in payload.items():
            if isinstance(value, (dict, list)):
                continue
            lines.append(f"{key}: {value}")

    body = "\n".join(lines).strip()
    if len(body) > max_chars:
        body = body[: max_chars - 1].rstrip() + "…"
    return subject, body


def _webhook_payload(url: str, record: dict, agent_name: str) -> dict:
    subject, body = _summarize(record)
    kind = record.get("kind") or "record"
    headline = f"[{agent_name}] {kind}: {subject}"

    if "hooks.slack.com" in url:
        text = f"*{headline}*\n{body}" if body else f"*{headline}*"
        return {
            "text": headline,
            "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": text[:2900]}}],
        }

    if "discord.com/api/webhooks" in url or "discordapp.com/api/webhooks" in url:
        content = f"**{headline}**\n{body}" if body else f"**{headline}**"
        return {"content": content[:1900]}

    return {"text": headline, "agent": agent_name, "record": record}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "webhook redirects are not followed", headers, fp)


_opener = urllib.request.build_opener(_NoRedirect)


def _post_webhook(url: str, payload: dict, timeout: float) -> int:
    netsafe.assert_public_url(url)
    data = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", "User-Agent": "dispatchkit-notify/1"},
        method="POST",
    )
    with _opener.open(request, timeout=timeout) as response:
        return response.status


def _send_email(recipients: list, record: dict, agent_name: str, timeout: float) -> None:
    host = os.environ.get("SMTP_HOST", "").strip()
    if not host:
        raise RuntimeError("NOTIFY_EMAIL_TO is set but SMTP_HOST is not")

    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ.get("SMTP_USER", "").strip()
    password = os.environ.get("SMTP_PASSWORD", "")
    sender = os.environ.get("SMTP_FROM", "").strip() or user
    if not sender:
        raise RuntimeError("SMTP_FROM (or SMTP_USER) is required to send email")

    subject, body = _summarize(record)
    kind = record.get("kind") or "record"

    message = EmailMessage()
    message["Subject"] = f"[{agent_name}] {kind}: {subject}"
    message["From"] = sender
    message["To"] = ", ".join(recipients)
    message.set_content(
        f"{subject}\n\n{body}\n\n--\n{agent_name} · {kind} · {record.get('ts', '')}\n"
    )

    if port == 465:
        smtp = smtplib.SMTP_SSL(host, port, timeout=timeout)
    else:
        smtp = smtplib.SMTP(host, port, timeout=timeout)
    try:
        if port != 465 and os.environ.get("SMTP_STARTTLS", "1") != "0":
            smtp.starttls()
        if user:
            smtp.login(user, password)
        smtp.send_message(message)
    finally:
        try:
            smtp.quit()
        except Exception:
            pass


def _log_delivery(data_dir: Path, entry: dict) -> None:
    try:
        entry = {"ts": datetime.utcnow().isoformat() + "Z", **entry}
        path = data_dir / DELIVERY_LOG_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, default=str) + "\n")
    except Exception:
        logger.warning("failed to log notify delivery", exc_info=True)


def _deliver(record: dict, agent_name: str, data_dir: Path) -> None:
    timeout = float(os.environ.get("NOTIFY_TIMEOUT_SECONDS", "10"))
    url = _webhook_url()
    recipients = _email_recipients()

    delivered = False
    if url:
        payload = _webhook_payload(url, record, agent_name)
        delivered |= _attempt("webhook", data_dir, record, lambda: _post_webhook(url, payload, timeout))

    if recipients:
        delivered |= _attempt(
            "email", data_dir, record,
            lambda: _send_email(recipients, record, agent_name, timeout),
        )

    # One notification = one unit of budget, and only when something actually
    # went out — matching the per-record semantics the queue check enforces.
    if delivered:
        _record_sent(data_dir)


def _attempt(transport: str, data_dir: Path, record: dict, send) -> bool:
    """Run one transport with retries. Never raises — a dead endpoint is not an agent failure."""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            send()
            _log_delivery(data_dir, {
                "transport": transport, "record_id": record.get("id"),
                "kind": record.get("kind"), "ok": True, "attempt": attempt,
            })
            return True
        except urllib.error.HTTPError as e:
            retryable = e.code == 429 or e.code >= 500
            error = f"HTTP {e.code}"
        except Exception as e:
            retryable = True
            error = str(e)[:300]

        last = attempt == MAX_ATTEMPTS or not retryable
        _log_delivery(data_dir, {
            "transport": transport, "record_id": record.get("id"),
            "kind": record.get("kind"), "ok": False, "attempt": attempt, "error": error,
        })
        if last:
            logger.warning(f"notify {transport} failed after {attempt} attempt(s): {error}")
            return False
        time.sleep(RETRY_BACKOFF_SECONDS[min(attempt - 1, len(RETRY_BACKOFF_SECONDS) - 1)])
    return False


def _run() -> None:
    while True:
        item = _queue.get()
        try:
            if item is None:
                return
            record, agent_name, data_dir = item
            _inflight.set()
            _deliver(record, agent_name, data_dir)
        except Exception:
            logger.warning("notify worker error", exc_info=True)
        finally:
            _inflight.clear()
            _queue.task_done()


def _ensure_worker() -> None:
    global _queue, _worker
    with _worker_lock:
        if _queue is None:
            _queue = queue.Queue(maxsize=QUEUE_MAX)
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_run, name="dispatchkit-notify", daemon=True)
            _worker.start()


def dispatch(record: dict, agent_name: str, data_dir: Optional[Path] = None) -> bool:
    """
    Queue a record for delivery if it matches the filter and a transport is set.
    Returns True if queued. Cheap and safe to call on every record.
    """
    if not enabled() or not configured() or not should_notify(record):
        return False

    directory = _data_dir(data_dir)
    try:
        _check_budget(directory)
    except NotifyBudgetExceeded as e:
        logger.warning(str(e))
        _log_delivery(directory, {
            "transport": "-", "record_id": record.get("id"),
            "kind": record.get("kind"), "ok": False, "error": str(e),
        })
        return False

    _ensure_worker()
    try:
        _queue.put_nowait((record, agent_name, directory))
        return True
    except queue.Full:
        logger.warning("notify queue full, dropping record %s", record.get("id"))
        return False


def flush(timeout: float = 10.0) -> bool:
    """Wait for queued notifications to drain. Returns True if the queue emptied."""
    if _queue is None:
        return True
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _queue.empty() and not _inflight.is_set():
            return True
        time.sleep(0.1)
    return _queue.empty() and not _inflight.is_set()


def send_test(agent_name: str = "dispatchkit", data_dir: Optional[Path] = None) -> dict:
    """
    Deliver a synthetic record through every configured transport, bypassing
    the NOTIFY_ON filter. Returns a result dict for the caller to surface.
    """
    if not enabled():
        return {"ok": False, "reason": "NOTIFY_DISABLED=1"}
    if not configured():
        return {"ok": False, "reason": "no transport configured (set NOTIFY_WEBHOOK_URL or NOTIFY_EMAIL_TO + SMTP_HOST)"}

    directory = _data_dir(data_dir)
    record = {
        "id": "notify-test",
        "ts": datetime.utcnow().isoformat() + "Z",
        "kind": "test",
        "title": "Notification test",
        "payload": {"message_md": f"If you can read this, {agent_name} can reach you."},
    }
    _deliver(record, agent_name, directory)
    return {
        "ok": True,
        "webhook": bool(_webhook_url()),
        "email": _email_recipients(),
        "detail": f"see data/{DELIVERY_LOG_FILE} for the per-transport result",
    }
