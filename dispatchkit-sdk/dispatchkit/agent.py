"""
DispatchKit Agent Runtime

The core agent framework. Every agent inherits from Agent and declares its
work as @phase methods (the SDK runs the first due one each tick), or for
fully custom sensing overrides observe() / think() / act() directly.

The runtime handles:
- HTTP health API (health, status, metrics)
- Graceful lifecycle (start, stop)
- State persistence across restarts
- Metrics collection
"""

import asyncio
import json
import logging
import os
import signal
import time
from abc import ABC
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional, Union

from aiohttp import web

from dispatchkit.events import Event
from dispatchkit.records import append_record, read_records
from dispatchkit import http, links, llm, notify, util
from dispatchkit.inbox import read_new_commands, commit_cursor, load_cursor
from dispatchkit.store import Store

logger = logging.getLogger(__name__)


PHASE_ATTR = "_dispatchkit_phase"


def phase(
    name: Optional[str] = None,
    *,
    every_hours: Optional[Union[float, Callable[["Agent"], float]]] = None,
    every_minutes: Optional[float] = None,
    when: Optional[Callable[["Agent"], bool]] = None,
):
    """Declare an async method as a scheduled phase of the agent.

    Phases are checked in declaration order each tick; the first one that is
    due runs, and its `last_<name>` timestamp is stamped into self.state.
    `every_hours` / `every_minutes` gate on that timestamp (`every_hours` may
    be a callable of the agent, for intervals read from env in setup()),
    `when(agent)` gates on a condition, and a phase with neither runs every tick.
    """
    hours = every_hours
    if every_minutes is not None:
        hours = every_minutes / 60.0

    def decorate(fn):
        setattr(fn, PHASE_ATTR, {"name": name or fn.__name__, "every_hours": hours, "when": when})
        return fn

    return decorate


class AgentState(str, Enum):
    STARTING = "starting"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPING = "stopping"
    STOPPED = "stopped"
    ERROR = "error"


@dataclass
class AgentConfig:
    agent_id: str = ""
    name: str = "unnamed"
    goal: str = ""
    tick_interval: float = 10.0
    api_port: int = 8080
    data_dir: Path = field(default_factory=lambda: Path("./data"))
    state_file: str = "state.json"
    max_consecutive_errors: int = 5
    max_error_rounds: int = 5
    chat_enabled: bool = True
    inbox_poll_seconds: float = 2.0
    run_once: bool = False

    @classmethod
    def from_manifest(cls, manifest_path: str = None) -> "AgentConfig":
        if manifest_path is None:
            manifest_path = os.environ.get("AGENT_MANIFEST")
        if manifest_path is None and os.path.exists("agent.json"):
            manifest_path = "agent.json"
        if manifest_path is None and os.path.exists("/agent/agent.json"):
            manifest_path = "/agent/agent.json"
        run_once = os.environ.get("AGENT_RUN_MODE", "") == "once"
        if manifest_path is None:
            return cls(agent_id=os.environ.get("AGENT_ID", "unknown"), run_once=run_once)

        with open(manifest_path) as f:
            m = json.load(f)

        data_dir = Path(os.environ.get("AGENT_DATA_DIR", m.get("data_dir", "./data")))
        api_port = int(os.environ.get("AGENT_API_PORT", m.get("api_port", 8080)))

        return cls(
            agent_id=m.get("id", os.environ.get("AGENT_ID", "unknown")),
            name=m.get("name", "unnamed"),
            goal=m.get("goal", ""),
            tick_interval=m.get("tick_interval", 10.0),
            api_port=api_port,
            data_dir=data_dir,
            max_consecutive_errors=m.get("max_consecutive_errors", 5),
            max_error_rounds=m.get("max_error_rounds", 5),
            chat_enabled=m.get("chat_enabled", True),
            inbox_poll_seconds=float(os.environ.get("AGENT_INBOX_POLL_SECONDS", m.get("inbox_poll_seconds", 2.0))),
            run_once=run_once,
        )


class Agent(ABC):
    """Base class for all DispatchKit agents."""

    def __init__(self, config: Optional[AgentConfig] = None):
        self.config = config or AgentConfig.from_manifest()
        self.config.data_dir.mkdir(parents=True, exist_ok=True)
        self.lifecycle = AgentState.STARTING
        self._cycle = 0
        self._errors = 0
        self._error_rounds = 0
        self._last_tick: Optional[float] = None
        self._started_at: Optional[float] = None
        self._metrics: dict = {"ticks": 0, "errors": 0, "events_emitted": 0}
        self.state: dict = {}
        self._shutdown_event = asyncio.Event()
        self._work_lock = asyncio.Lock()
        self._app: Optional[web.Application] = None
        self._runner: Optional[web.AppRunner] = None
        self._store: Optional[Store] = None

    @property
    def store(self) -> "Store":
        """Lazily-opened per-agent SQLite store at data/store.db."""
        if self._store is None:
            self._store = Store(self.config.data_dir / "store.db")
        return self._store

    # --- Agent links (see dispatchkit/links.py) ---

    def publish(self, name: str, content) -> Path:
        """Publish a file under data/out/ for workers linked to this one."""
        return links.publish(self.config.data_dir, name, content)

    def inputs(self) -> dict:
        """{alias: [Path, ...]} of files linked sources have published to this worker."""
        return links.inputs(self.config.data_dir)

    def input_path(self, alias: str, name: str) -> Optional[Path]:
        """A specific file from a linked source, or None if it has not landed."""
        return links.input_path(self.config.data_dir, alias, name)

    # --- Lifecycle hooks (override in subclass) ---

    async def setup(self):
        """Called once before the main loop starts. Override for initialization."""
        pass

    async def teardown(self):
        """Called once after the main loop ends. Override for cleanup."""
        pass

    def phases(self) -> list:
        """[(name, method, spec)] for every @phase method, in declaration order."""
        found: dict = {}
        for klass in reversed(type(self).__mro__):
            for attr, value in vars(klass).items():
                spec = getattr(value, PHASE_ATTR, None)
                if spec is not None:
                    found[attr] = (spec["name"], getattr(self, attr), spec)
        return list(found.values())

    def due_phase(self) -> Optional[str]:
        """Name of the first declared phase that is due now, or None."""
        for name, _method, spec in self.phases():
            hours = spec["every_hours"]
            if callable(hours):
                hours = hours(self)
            if hours is not None and not util.due(self.state, f"last_{name}", hours):
                continue
            if spec["when"] is not None and not spec["when"](self):
                continue
            return name
        return None

    async def observe(self) -> Any:
        """Default: report which declared phase is due. Override for custom sensing."""
        return {"phase": self.due_phase() or "idle"}

    async def think(self, observation: Any) -> Any:
        """Default: act on any non-idle observation. Override for real decisions."""
        if isinstance(observation, dict) and observation.get("phase") in (None, "idle"):
            return None
        return observation

    async def act(self, decision: Any) -> Any:
        """Default: run the due phase and stamp its timestamp. Override for custom work."""
        name = decision.get("phase") if isinstance(decision, dict) else None
        for phase_name, method, _spec in self.phases():
            if phase_name == name:
                result = await method()
                util.mark(self.state, f"last_{name}")
                self.save_state()
                return result
        raise NotImplementedError(
            f"{type(self).__name__} has no @phase named {name!r}; declare one or override act()"
        )

    async def on_event(self, event: Event):
        """Handle an incoming event from another agent. Override to react."""
        logger.info(f"Received event: {event.type} from {event.source}")

    async def on_command(self, command: dict) -> Any:
        """
        Handle a user command from the inbox. Return a str or dict reply.
        Default: answer conversationally from goal + state + recent records.
        Override for structured commands; fall back with super().on_command(cmd).
        """
        context_parts = [f"Current state: {json.dumps(self.state, default=str)[:2000]}"]
        recent = self.load_records(limit=10)
        if recent:
            lines = [
                f"- {r.get('ts', '')} [{r.get('kind', '')}] {r.get('title') or ''} {str(r.get('payload', ''))[:300]}"
                for r in recent
            ]
            context_parts.append("Recent output records:\n" + "\n".join(lines))

        system = (
            f"You are the agent \"{self.config.name}\". Goal: {self.config.goal or 'not set'}. "
            f"Answer the user's message using the context below. Be concise; markdown is fine. "
            f"If asked to do something outside your capabilities, say what you can do instead."
        )
        prompt = "\n\n".join(context_parts) + f"\n\nUser message: {command.get('text', '')}"
        stub = f"[llm disabled] state: {json.dumps(self.state, default=str)[:500]}"
        try:
            return await self.ask_llm(prompt, system=system, stub=stub)
        except Exception as e:
            logger.error(f"on_command failed: {e}")
            return f"error: {e}"

    async def ask_llm(
        self,
        prompt: str,
        *,
        system: Optional[str] = None,
        max_tokens: int = 2000,
        stub: Optional[str] = None,
    ) -> str:
        """Call the LLM off the event loop, with the SDK guardrails applied."""
        return await asyncio.to_thread(
            llm.ask, prompt, system=system, max_tokens=max_tokens,
            data_dir=self.config.data_dir, stub=stub,
        )

    async def ask_json(
        self,
        prompt: str,
        *,
        system: Optional[str] = None,
        max_tokens: int = 3000,
        stub: Optional[dict] = None,
    ) -> dict:
        """Like ask_llm but parses the first JSON object out of the reply ({} if none)."""
        return await asyncio.to_thread(
            llm.ask_json, prompt, system=system, max_tokens=max_tokens,
            data_dir=self.config.data_dir, stub=stub,
        )

    async def fetch(self, url: str, **kwargs) -> "http.Response":
        """HTTP request off the event loop: public-host guard, retries, timeout. See dispatchkit.http."""
        return await asyncio.to_thread(http.fetch, url, **kwargs)

    # --- State persistence ---

    @property
    def _persisted_state(self) -> dict:
        return self.state

    @_persisted_state.setter
    def _persisted_state(self, value: dict) -> None:
        self.state = value

    def save_state(self, state: Optional[dict] = None):
        """Persist self.state (or the dict given) to disk; survives restarts."""
        if state is not None:
            self.state = state
        state = self.state
        state_path = self.config.data_dir / self.config.state_file
        state_path.parent.mkdir(parents=True, exist_ok=True)
        with open(state_path, "w") as f:
            json.dump(state, f, indent=2)

    def load_state(self) -> dict:
        """Load persisted state from disk."""
        state_path = self.config.data_dir / self.config.state_file
        if state_path.exists():
            with open(state_path) as f:
                self.state = json.load(f)
        return self.state

    # --- Structured output (records) ---

    def record(
        self,
        kind: str,
        payload: dict,
        *,
        title: Optional[str] = None,
        score: Optional[float] = None,
        tags: Optional[list] = None,
        dedupe_key: Optional[str] = None,
        alert: Optional[str] = None,
    ) -> dict:
        """Append a structured record to data/records.jsonl (surfaced via the platform API).

        alert="info" or "error" flags the record for the user: it shows as a
        badge on the worker's canvas node until they open the worker.
        """
        rec = append_record(
            self.config.data_dir,
            self.config.agent_id,
            kind,
            payload,
            title=title,
            score=score,
            tags=tags,
            dedupe_key=dedupe_key,
            alert=alert,
        )
        self._metrics["records_written"] = self._metrics.get("records_written", 0) + 1
        if notify.dispatch(rec, self.config.name, self.config.data_dir):
            self._metrics["notifications_sent"] = self._metrics.get("notifications_sent", 0) + 1
        return rec

    def alert(
        self,
        title: str,
        payload: Optional[dict] = None,
        *,
        level: str = "error",
        kind: str = "alert",
        tags: Optional[list] = None,
        dedupe_key: Optional[str] = None,
    ) -> dict:
        """Record something the user must see: a badge appears on the worker until it is opened."""
        return self.record(
            kind,
            payload or {},
            title=title,
            tags=tags,
            dedupe_key=dedupe_key,
            alert=level,
        )

    # --- Setup state ---

    def needs_setup(self, missing, hint: str = "") -> None:
        """Mark the agent as blocked on configuration.

        Writes data/status.json, which the platform surfaces as a
        "needs configuration" state on the worker instead of a green
        "working" status. Call it from observe() when a required env var
        is empty; call clear_needs_setup() once configured.
        """
        if isinstance(missing, str):
            missing = [missing]
        status = {"state": "needs_setup", "missing": list(missing), "hint": hint}
        path = self.config.data_dir / "status.json"
        try:
            if path.exists() and json.loads(path.read_text()) == status:
                return
        except (json.JSONDecodeError, OSError):
            pass
        path.write_text(json.dumps(status))
        logger.info("needs_setup: missing %s — %s", ", ".join(status["missing"]), hint)

    def clear_needs_setup(self) -> None:
        """Remove the needs_setup marker once configuration is complete."""
        path = self.config.data_dir / "status.json"
        if path.exists():
            path.unlink()

    def load_records(self, kind: Optional[str] = None, limit: Optional[int] = None) -> list:
        """Read this agent's records, newest first."""
        return read_records(self.config.data_dir, kind=kind, limit=limit)

    def has_recent_record(self, dedupe_key: str, within_hours: float) -> bool:
        """True if a record with this dedupe_key was written within the window."""
        cutoff = time.time() - within_hours * 3600
        for rec in read_records(self.config.data_dir):
            if rec.get("dedupe_key") != dedupe_key:
                continue
            try:
                ts = datetime.fromisoformat(rec.get("ts", "").rstrip("Z")).timestamp()
            except ValueError:
                continue
            if ts >= cutoff:
                return True
        return False

    # --- Event emission ---

    async def emit(self, event_type: str, payload: dict):
        """Log a named event. There is no inter-agent bus; this is a structured log line."""
        logger.info(f"[event] {event_type} -> {json.dumps(payload, default=str)}")
        self._metrics["events_emitted"] += 1

    # --- HTTP health API ---

    def _build_api(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/health", self._handle_health)
        app.router.add_get("/status", self._handle_status)
        app.router.add_get("/metrics", self._handle_metrics)
        return app

    async def _handle_health(self, request: web.Request) -> web.Response:
        healthy = self.lifecycle in (AgentState.RUNNING, AgentState.PAUSED)
        return web.json_response({"healthy": healthy, "state": self.lifecycle.value}, status=200 if healthy else 503)

    async def _handle_status(self, request: web.Request) -> web.Response:
        return web.json_response({
            "agent_id": self.config.agent_id,
            "name": self.config.name,
            "state": self.lifecycle.value,
            "cycle": self._cycle,
            "uptime_seconds": int(time.time() - self._started_at) if self._started_at else 0,
            "last_tick": self._last_tick,
            "consecutive_errors": self._errors,
        })

    async def _handle_metrics(self, request: web.Request) -> web.Response:
        return web.json_response(self._metrics)

    # --- Main loop ---

    async def _tick(self):
        try:
            async with self._work_lock:
                observation = await self.observe()
                decision = await self.think(observation)
                if decision is not None:
                    result = await self.act(decision)
                    logger.debug(f"Tick {self._cycle}: acted -> {result}")
            self._errors = 0
            self._error_rounds = 0
        except Exception as e:
            self._errors += 1
            self._metrics["errors"] += 1
            logger.error(f"Tick {self._cycle} error ({self._errors}/{self.config.max_consecutive_errors}): {e}")
            if self._errors >= self.config.max_consecutive_errors:
                logger.critical("Max consecutive errors reached, entering error state")
                self.lifecycle = AgentState.ERROR
                self._alert_error_state(e)

        self._cycle += 1
        self._last_tick = time.time()
        self._metrics["ticks"] += 1

    def _alert_error_state(self, exc: Exception) -> None:
        dedupe_key = "agent-error-state"
        try:
            if self.has_recent_record(dedupe_key, within_hours=1):
                return
            self.alert(
                f"{self.config.name} stopped after {self._errors} consecutive errors",
                {"error": f"{type(exc).__name__}: {exc}"[:2000]},
                kind="error",
                dedupe_key=dedupe_key,
            )
        except Exception as alert_exc:
            logger.warning(f"Could not write error-state alert: {alert_exc}")

    async def _drain_inbox(self):
        """Process all pending inbox commands once."""
        prior_offset = load_cursor(self.config.data_dir)["offset"]
        commands, end_offset, seen_ids = read_new_commands(self.config.data_dir)
        for cmd in commands:
            cmd_offset = cmd.pop("_offset", end_offset)
            logger.info(f"Command received: {str(cmd.get('text', ''))[:100]}")
            async with self._work_lock:
                reply = await self.on_command(cmd)
            payload = reply if isinstance(reply, dict) else {"text": str(reply)}
            payload.setdefault("reply_to", cmd["id"])
            self.record(
                "reply",
                payload,
                title=(payload.get("text") or "")[:80],
                dedupe_key=f"reply:{cmd['id']}",
            )
            seen_ids = seen_ids + [cmd["id"]]
            commit_cursor(self.config.data_dir, cmd_offset, seen_ids)
            self._metrics["commands_handled"] = self._metrics.get("commands_handled", 0) + 1
        if end_offset != prior_offset:
            commit_cursor(self.config.data_dir, end_offset, seen_ids)

    async def _inbox_loop(self):
        while not self._shutdown_event.is_set():
            try:
                await self._drain_inbox()
            except Exception as e:
                logger.error(f"Inbox loop error: {e}")
            await asyncio.sleep(self.config.inbox_poll_seconds)

    async def _start_api(self):
        """Start HTTP API, retrying ports if taken."""
        self._app = self._build_api()
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()

        port = self.config.api_port
        for attempt in range(20):
            try:
                site = web.TCPSite(self._runner, "0.0.0.0", port)
                await site.start()
                self.config.api_port = port
                logger.info(f"Agent API listening on port {port}")
                return
            except OSError:
                port += 1

        logger.warning("Could not bind API port, running without HTTP API")
        await self._runner.cleanup()
        self._runner = None

    def _write_pid(self):
        pid_file = self.config.data_dir / ".pid"
        pid_file.write_text(str(os.getpid()))

    def _remove_pid(self):
        pid_file = self.config.data_dir / ".pid"
        if pid_file.exists():
            pid_file.unlink()

    def _write_boot_error(self, message: str):
        try:
            (self.config.data_dir / ".boot_error").write_text(message)
        except OSError:
            pass

    def _clear_boot_markers(self):
        for name in (".boot_error", ".crash_loop"):
            path = self.config.data_dir / name
            try:
                if path.exists():
                    path.unlink()
            except OSError:
                pass

    def _start_runtime_watchdog(self):
        """Hard exit after AGENT_MAX_RUNTIME_MIN minutes (set for scheduled
        run-once wakes) so a hung tick can't run — and bill — forever."""
        raw = os.environ.get("AGENT_MAX_RUNTIME_MIN", "")
        try:
            max_min = float(raw)
        except ValueError:
            return
        if max_min <= 0:
            return

        async def _watchdog():
            await asyncio.sleep(max_min * 60)
            logger.critical(f"Max runtime of {max_min} minutes exceeded, forcing exit")
            os._exit(2)

        asyncio.create_task(_watchdog())

    async def run(self):
        """Main entry point. Starts the API server and runs the observe-think-act loop."""
        self._started_at = time.time()
        self._write_pid()

        # Handle SIGTERM/SIGINT
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self._shutdown_event.set)

        # Start HTTP API
        await self._start_api()

        # Load persisted state
        self.load_state()

        self._start_runtime_watchdog()

        # Setup
        try:
            await self.setup()
        except Exception:
            import traceback
            self._write_boot_error("Agent setup failed:\n" + traceback.format_exc())
            raise
        self._clear_boot_markers()
        self.lifecycle = AgentState.RUNNING
        logger.info(f"Agent {self.config.name} ({self.config.agent_id}) running")

        # Run-once mode (scheduled wake): drain the inbox, run one tick, exit.
        # Phase gating via util.due/util.mark lives in data/state.json, so a
        # single tick is exactly one resident-loop iteration.
        if self.config.run_once:
            logger.info("Run mode: once (scheduled wake)")
            try:
                if self.config.chat_enabled:
                    try:
                        await self._drain_inbox()
                    except Exception as e:
                        logger.error(f"Inbox drain error: {e}")
                await self._tick()
            finally:
                self.lifecycle = AgentState.STOPPING
                await self.teardown()
                if self._store is not None:
                    self._store.close()
                if not await asyncio.to_thread(notify.flush, 10.0):
                    logger.warning("shutting down with notifications still queued")
                if self._runner:
                    await self._runner.cleanup()
                self._remove_pid()
                self.lifecycle = AgentState.STOPPED
                logger.info("Agent stopped (run-once complete)")
            return

        inbox_task = asyncio.create_task(self._inbox_loop()) if self.config.chat_enabled else None

        # Main loop
        exit_code = 0
        try:
            while not self._shutdown_event.is_set():
                if self.lifecycle == AgentState.RUNNING:
                    await self._tick()
                elif self.lifecycle == AgentState.ERROR:
                    # Bounded recovery: back off exponentially, and after
                    # max_error_rounds give up and exit non-zero so the
                    # container stops (and stops billing) instead of quietly
                    # crash-looping forever.
                    self._error_rounds += 1
                    if self._error_rounds > self.config.max_error_rounds:
                        logger.critical(
                            f"Still failing after {self.config.max_error_rounds} recovery rounds, giving up"
                        )
                        self._write_boot_error(
                            f"Crash loop: {self.config.max_error_rounds} recovery rounds exhausted; "
                            "the agent stopped itself. Check the logs and fix the code, then restart."
                        )
                        try:
                            (self.config.data_dir / ".crash_loop").write_text(
                                f"{self._error_rounds} rounds at cycle {self._cycle}"
                            )
                        except OSError:
                            pass
                        exit_code = 3
                        self._shutdown_event.set()
                        continue
                    backoff = min(5 * (2 ** (self._error_rounds - 1)), 300)
                    logger.warning(
                        f"Error state: recovery round {self._error_rounds}/{self.config.max_error_rounds}, "
                        f"retrying in {backoff}s"
                    )
                    await asyncio.sleep(backoff)
                    self._errors = 0
                    self.lifecycle = AgentState.RUNNING

                try:
                    await asyncio.wait_for(
                        self._shutdown_event.wait(),
                        timeout=self.config.tick_interval,
                    )
                    break
                except asyncio.TimeoutError:
                    pass
        finally:
            self.lifecycle = AgentState.STOPPING
            if inbox_task:
                inbox_task.cancel()
            await self.teardown()
            if self._store is not None:
                self._store.close()
            if not await asyncio.to_thread(notify.flush, 10.0):
                logger.warning("shutting down with notifications still queued")
            if self._runner:
                await self._runner.cleanup()
            self._remove_pid()
            self.lifecycle = AgentState.STOPPED
            logger.info("Agent stopped")
        if exit_code:
            raise SystemExit(exit_code)
