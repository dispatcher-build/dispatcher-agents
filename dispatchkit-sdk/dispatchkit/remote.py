"""
Remote reporting for workers that run outside the platform.

A worker scheduled as `local` runs on the user's own machine, where the
platform cannot read its data dir. With DISPATCHKIT_REMOTE_URL and
DISPATCHKIT_TOKEN set (`dispatcher run --remote` sets them), the agent
mirrors every record to the platform, pulls chat commands from it and keeps
a heartbeat, so the worker behaves like a hosted one on the canvas. Every
file is still written locally.

Records go through one ordered background thread so the agent loop never
waits on the network; a failed post is retried with backoff. The heartbeat
is a second thread that posts the current state every HEARTBEAT_SECONDS and
immediately on every state change, never queued, so a stale report is never
delivered late as a fresh one.
"""

import json
import logging
import os
import queue
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

SEEN_FILE = "remote_seen.json"
SEEN_MAX = 500
QUEUE_MAX = 2000
TIMEOUT_SECONDS = 20
HEARTBEAT_SECONDS = 45


def _detail(error: urllib.error.HTTPError) -> str:
    try:
        body = json.loads(error.read().decode())
        return str(body.get("detail") or body.get("error") or body)
    except Exception:
        return str(error.reason)


class Remote:
    def __init__(self, url: str, token: str, agent_id: str, data_dir):
        self.url = url.rstrip("/")
        self.token = token
        self.agent_id = agent_id
        self.data_dir = Path(data_dir)
        self.host = socket.gethostname()
        self.failures = 0
        self._state = "idle"
        self._missing: list = []
        self._hint = ""
        self._seen = self._load_seen()
        self._queue: "queue.Queue" = queue.Queue(maxsize=QUEUE_MAX)
        self._wake = threading.Event()
        self._stopping = threading.Event()
        self._pump_thread: Optional[threading.Thread] = None
        self._beat_thread: Optional[threading.Thread] = None

    @classmethod
    def from_env(cls, agent_id: str, data_dir) -> Optional["Remote"]:
        url = os.environ.get("DISPATCHKIT_REMOTE_URL", "").strip()
        token = os.environ.get("DISPATCHKIT_TOKEN", "").strip()
        if not url or not token:
            return None
        return cls(url, token, agent_id, data_dir)

    # --- lifecycle ---

    def start(self) -> None:
        if self._pump_thread is not None:
            return
        self._pump_thread = threading.Thread(target=self._pump, name="dispatchkit-remote-records", daemon=True)
        self._beat_thread = threading.Thread(target=self._beat, name="dispatchkit-remote-heartbeat", daemon=True)
        self._pump_thread.start()
        self._beat_thread.start()
        logger.info("remote reporting to %s as %s", self.url, self.agent_id)

    def stop(self, timeout: float = 10.0) -> None:
        self._stopping.set()
        self._wake.set()
        deadline = time.time() + timeout
        while not self._queue.empty() and time.time() < deadline:
            time.sleep(0.2)
        try:
            self._post_heartbeat("stopped")
        except Exception as e:
            logger.warning("remote: final heartbeat failed: %s", e)

    # --- outgoing ---

    def record(self, record: dict) -> None:
        body = {k: record.get(k) for k in ("kind", "payload", "title", "score", "tags", "dedupe_key", "alert")}
        try:
            self._queue.put_nowait(("/records", body))
        except queue.Full:
            logger.error("remote queue full, dropping record %s", record.get("kind"))

    def set_state(self, state: str, missing=None, hint: str = "") -> None:
        self._state = state
        self._missing = list(missing or [])
        self._hint = hint
        self._wake.set()

    # --- incoming ---

    def pending_commands(self) -> list:
        data = self._request("GET", "/chat", params={"limit": 50})
        commands = []
        for message in data.get("messages", []):
            if message.get("role") != "user" or message.get("status") != "pending":
                continue
            cid = message.get("id") or ""
            if not cid or cid in self._seen:
                continue
            commands.append({
                "id": cid,
                "ts": message.get("ts", ""),
                "type": "command",
                "text": message.get("text", ""),
                "source": "remote",
                "meta": {},
            })
        return commands

    def mark_seen(self, command_id: str) -> None:
        self._seen.append(command_id)
        del self._seen[:-SEEN_MAX]
        try:
            (self.data_dir / SEEN_FILE).write_text(json.dumps(self._seen))
        except OSError as e:
            logger.warning("remote: could not persist seen ids: %s", e)

    # --- internals ---

    def _load_seen(self) -> list:
        path = self.data_dir / SEEN_FILE
        try:
            seen = json.loads(path.read_text())
            return [str(s) for s in seen] if isinstance(seen, list) else []
        except (OSError, ValueError):
            return []

    def _request(self, method: str, path: str, body=None, params=None):
        url = f"{self.url}/api/v2/agents/{self.agent_id}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = json.dumps(body, default=str).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "User-Agent": "dispatchkit-remote",
        })
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as res:
            return json.loads(res.read().decode() or "{}")

    def _post_heartbeat(self, state: str) -> None:
        self._request("POST", "/heartbeat", {
            "state": state,
            "missing": self._missing if state == "needs_setup" else [],
            "hint": self._hint if state == "needs_setup" else "",
            "host": self.host,
        })

    def _beat(self) -> None:
        while not self._stopping.is_set():
            try:
                self._post_heartbeat(self._state)
                self.failures = 0
            except urllib.error.HTTPError as e:
                logger.warning("remote heartbeat rejected (%s): %s", e.code, _detail(e))
            except Exception as e:
                self.failures += 1
                logger.warning("remote heartbeat failed: %s", e)
            self._wake.wait(HEARTBEAT_SECONDS)
            self._wake.clear()

    def _pump(self) -> None:
        while True:
            path, body = self._queue.get()
            delay = 2.0
            while True:
                try:
                    self._request("POST", path, body)
                    self.failures = 0
                    break
                except urllib.error.HTTPError as e:
                    if 400 <= e.code < 500 and e.code not in (408, 429):
                        logger.warning("remote %s rejected (%s): %s", path, e.code, _detail(e))
                        break
                    self.failures += 1
                except Exception as e:
                    self.failures += 1
                    logger.warning("remote %s failed (%s), retrying in %.0fs", path, e, delay)
                if self._stopping.is_set() and delay > 8:
                    break
                time.sleep(delay)
                delay = min(delay * 2, 120)
            self._queue.task_done()
