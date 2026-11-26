"""
Claude Dev: parallel Claude Code sessions on one repository, run on your machine.

Installed once per repository, the worker is the **lead**. Its page lists the sessions
and starts new ones, and its own Claude Code conversation answers questions about all
of them and hands them work. Every session is a worker of its own (its own hex on the
canvas), created by the lead with DEV_ROLE=session: one git worktree, branch, pair of
app ports and Claude Code conversation. Both use this machine's Claude Code login (your
Claude subscription) unless ANTHROPIC_API_KEY or CLAUDE_CODE_USE_BEDROCK=1 is set, and
load the repo's CLAUDE.md and your Claude Code settings.

  dispatcher run --remote <agent-id>     # what the desktop app runs for every local worker

Lead:     +<name> [task] starts a session, @<name> <text> forwards to one, `sessions`
          lists them, anything else is a question or request for the lead.
Session:  up | down | diff | sync | pr | stop | close | status; anything else is a task.
"""

import asyncio
import fcntl
import json
import logging
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "lib"))

from dispatchkit import Agent, phase

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

HERE = Path(__file__).parent
ROLE = os.environ.get("DEV_ROLE", "lead").strip().lower() or "lead"
SESSION_NAME = os.environ.get("DEV_SESSION", "").strip()
REPO = os.environ.get("DEV_REPO", "").strip()
BASE_BRANCH = os.environ.get("DEV_BASE_BRANCH", "main").strip() or "main"
PERMISSION_MODE = os.environ.get("DEV_PERMISSION_MODE", "acceptEdits").strip() or "acceptEdits"
MODEL = os.environ.get("DEV_MODEL", "").strip() or None
BRIEF = os.environ.get("DEV_BRIEF", "").strip()
SETUP_COMMAND = os.environ.get("DEV_SETUP_COMMAND", "").strip()
UP_COMMAND = os.environ.get("DEV_UP_COMMAND", "").strip()
DOWN_COMMAND = os.environ.get("DEV_DOWN_COMMAND", "").strip()
SEED_COMMAND = os.environ.get("DEV_SEED_COMMAND", "").strip()
SHARED_ENV = ("DEV_REPO", "DEV_BASE_BRANCH", "DEV_PERMISSION_MODE", "DEV_MODEL", "DEV_BRIEF", "DEV_SETUP_COMMAND", "DEV_SEED_COMMAND", "DEV_UP_COMMAND", "DEV_DOWN_COMMAND")
SESSION_FILES = ("main.py", "requirements.txt", "ui/index.html")
SESSION_WORDS = {"up", "down", "diff", "sync", "pr", "stop", "close", "status"}
SESSION_COMMANDS = [
    {"name": "status", "description": "Branch, changes, ports, app and PR"},
    {"name": "up", "description": "Start this session's copy of the app on its own ports"},
    {"name": "diff", "description": "What this session changed"},
    {"name": "pr", "description": "Rebase, push and open or update the pull request"},
]
EDIT_TOOLS = ["Read", "Edit", "Write", "MultiEdit", "Glob", "Grep", "Bash", "WebFetch", "WebSearch", "TodoWrite"]
LEAD_TOOLS = ["Read", "Glob", "Grep", "Bash", "WebFetch", "WebSearch", "TodoWrite", "mcp__team__*"]
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,60}$")
SLOTS_FILE = Path.home() / ".dispatchkit" / "dev-slots.json"
APP_START_SECONDS = 240
SETUP_TIMEOUT_SECONDS = 1800
ENV_TEMPLATE_SUFFIXES = (".example", ".sample", ".template", ".dist")
AUTH_HINT = "Claude Code is not signed in on this machine. Run `claude` in a terminal, log in, then send the task again."

DETECT_PROMPT = (
    "Work out how to run this repository's development app from this directory. Only read files; "
    "do not run, install or change anything. Reply with only a JSON object and no other text:\n"
    '{"setup": "<bash command that installs dependencies, safe to run again; empty string if none>", '
    '"up": "<bash command that starts the app in the foreground for local development>", '
    '"path": "<path to open in the browser, e.g. / or /dashboard>"}\n'
    "Both commands run with bash from the repository root. The app must listen on the ports in these "
    "environment variables: PORT (also FRONTEND_PORT) for the web interface, API_PORT (also "
    "BACKEND_PORT) for an API server if there is one. Reference them with shell expansion, for example "
    "`npm run dev -- --port $PORT`, unless the project's own scripts already read them. Prefer the "
    "project's documented dev script, and use the project's own package manager and virtualenv layout."
)

SESSION_PROMPT = (
    "You are one of several Claude Code sessions working on this repository at the same time, each in "
    "its own git worktree. This session works only in this worktree, on branch {branch} (base {base}). "
    "Finish each task completely: edit, run the relevant checks, and commit with a clear message. Reply "
    "with a short summary of what changed and anything the user must decide. Do not push or open pull "
    "requests yourself; the user does that from Dispatcher.\n\n"
    "This session's own copy of the app uses PORT={port} and API_PORT={api_port} (also FRONTEND_PORT "
    "and BACKEND_PORT). They are set in your environment, and Dispatcher's Start app runs the app on "
    "them. Use http://localhost:{port} (API http://localhost:{api_port}) for anything you run or test, "
    "never the default ports, which belong to other sessions. DEV_SESSION holds this session's name; "
    "use it to name anything else that must not be shared with other sessions, such as a database."
)

LEAD_PROMPT = (
    "You are the lead for a team of Claude Code sessions working on the repository at {repo} (base "
    "branch {base}). Each session is its own worker with its own git worktree at "
    "{worktrees}/<name>, its own branch and its own app ports; the user watches them as hexes on the "
    "Dispatcher canvas. You are the one they ask what is going on.\n\n"
    "Use list_sessions to see every session's branch, whether it is working, what it was last asked, "
    "its last answer, commits ahead of {base}, uncommitted files, ports and pull request. Use "
    "session_history for a session's recent tasks and answers. Read worktrees directly when you need "
    "detail (git -C <path> log, diff, status; gh pr view). Answer from what you find, briefly, and say "
    "when something needs the user's decision.\n\n"
    "When the user wants work done, give it to a session: send_to_session for an existing one, or "
    "new_session(name, task) to start one, with a short branch-style name and a task complete enough "
    "to be done without asking. Split a large request into independent sessions that touch different "
    "parts of the code. You never edit files yourself.\n\n"
    "When asked to review a session, read its diff against {base} in its worktree and its recent "
    "history, then say whether it is ready for a pull request, what is wrong or missing, and what you "
    "would send back; send the fixes to the session only if the user asked you to. When a session's "
    "work is merged or abandoned and the user wants it gone, use close_session, which removes its "
    "worktree and worker and keeps its branch."
)

REVIEW_PROMPT = (
    "Review the work in session `{name}`. Read its diff against {base} in its worktree and its recent "
    "tasks and answers. Say whether it is ready for a pull request, list anything wrong, risky or "
    "missing with file and line, and say what you would send back to the session. Do not send it "
    "anything yet."
)


def adopt_shell_path():
    shell = os.environ.get("SHELL") or "/bin/bash"
    try:
        out = subprocess.run([shell, "-ilc", 'printf "__PATH__%s__PATH__" "$PATH"'], capture_output=True, text=True, timeout=15, stdin=subprocess.DEVNULL).stdout
    except (OSError, subprocess.SubprocessError):
        return
    found = re.search(r"__PATH__(.*?)__PATH__", out, re.S)
    if not found:
        return
    current = [d for d in os.environ.get("PATH", "").split(os.pathsep) if d]
    extra = [d for d in found.group(1).split(os.pathsep) if d and d not in current]
    os.environ["PATH"] = os.pathsep.join(current + list(dict.fromkeys(extra)))


adopt_shell_path()


def run_git(cwd, *args, timeout=120):
    proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout).strip() or f"git {' '.join(args)} failed")
    return proc.stdout.strip()


def run_cmd(cwd, args, timeout=120, env=None):
    proc = subprocess.run(args, cwd=str(cwd), capture_output=True, text=True, timeout=timeout, env=env)
    return proc.returncode, (proc.stdout or "").strip(), (proc.stderr or "").strip()


def now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def slot_env(slot: int) -> dict:
    return {
        "PORT": str(3000 + 10 * slot),
        "API_PORT": str(8000 + 10 * slot),
        "FRONTEND_PORT": str(3000 + 10 * slot),
        "BACKEND_PORT": str(8000 + 10 * slot),
    }


def port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("0.0.0.0", port))
            return True
        except OSError:
            return False


def port_listening(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _slots(update):
    SLOTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(SLOTS_FILE, "a+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.seek(0)
        try:
            claims = json.loads(f.read() or "{}")
        except ValueError:
            claims = {}
        claims = {k: v for k, v in claims.items() if Path(k).exists()}
        result = update(claims)
        f.seek(0)
        f.truncate()
        f.write(json.dumps(claims, indent=2))
        return result


def claim_slot(key: str) -> int:
    def update(claims):
        if key not in claims:
            taken = set(claims.values())
            claims[key] = next(
                n for n in range(1, 200)
                if n not in taken and port_free(3000 + 10 * n) and port_free(8000 + 10 * n)
            )
        return claims[key]
    return _slots(update)


def release_slot(key: str):
    _slots(lambda claims: claims.pop(key, None))


def copy_env_files(repo: Path, dest: Path) -> list:
    code, out, _ = run_cmd(repo, ["git", "ls-files", "--others", "--ignored", "--exclude-standard", "--directory"])
    if code != 0:
        return []
    copied = []
    for rel in out.splitlines():
        name = Path(rel).name
        if rel.endswith("/") or not (name == ".env" or name.startswith(".env.")) or name.endswith(ENV_TEMPLATE_SUFFIXES):
            continue
        src, dst = repo / rel, dest / rel
        if dst.exists() or not src.is_file():
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        copied.append(rel)
    return copied


def parse_json_object(text: str):
    match = re.search(r"\{.*\}", text or "", re.S)
    if not match:
        return None
    try:
        value = json.loads(match.group(0))
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def log_tail(path: Path, lines: int = 15) -> str:
    try:
        return "\n".join(path.read_text(errors="replace").splitlines()[-lines:])
    except OSError:
        return ""


def slugify(text: str) -> str:
    words = re.findall(r"[a-z0-9]+", text.lower())
    return "-".join(words[:5])[:40].strip("-") or datetime.now().strftime("task-%m%d-%H%M")


def auth_mode() -> str:
    if os.environ.get("CLAUDE_CODE_USE_BEDROCK") == "1":
        return "Amazon Bedrock"
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "an Anthropic API key"
    return "your Claude subscription"


def pid_alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, TypeError, ValueError):
        return False


def tool_summary(inp: dict, cwd: str) -> str:
    for key in ("command", "file_path", "path", "pattern", "query", "url", "description", "name"):
        if inp.get(key):
            return str(inp[key]).replace(cwd + "/", "")[:160]
    return json.dumps(inp)[:160]


def repo_check():
    missing, hints = [], []
    repo = Path(REPO).expanduser().resolve() if REPO else None
    if not repo or not repo.is_dir() or run_cmd(repo, ["git", "rev-parse", "--git-dir"])[0] != 0:
        missing.append("DEV_REPO")
        hints.append("DEV_REPO must be the path of a git repository on this machine")
    try:
        import claude_agent_sdk  # noqa: F401
    except ImportError:
        missing.append("claude-agent-sdk")
        hints.append("pip install claude-agent-sdk in the Python that runs this worker")
    return repo, missing, hints


class Platform:
    def __init__(self):
        self.url = os.environ.get("DISPATCHKIT_REMOTE_URL", "").rstrip("/")
        self.token = os.environ.get("DISPATCHKIT_TOKEN", "")

    @property
    def ready(self) -> bool:
        return bool(self.url and self.token)

    def call(self, method: str, path: str, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            self.url + path, data=data, method=method,
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as res:
                return json.loads(res.read() or b"{}")
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:300]
            raise PlatformError(e.code, detail) from None

    async def acall(self, method: str, path: str, body=None):
        return await asyncio.to_thread(self.call, method, path, body)


class PlatformError(RuntimeError):
    def __init__(self, status: int, detail: str):
        super().__init__(f"Dispatcher answered {status}: {detail}")
        self.status = status


class DevBase(Agent):
    label = "lead"

    def _init_turns(self):
        self.client = None
        self.queue = asyncio.Queue()
        self.busy = False
        self.prompt = None
        self.started = None
        self.background = set()
        pending = self.state.pop("pending", None)
        if pending:
            tasks = "\n".join(f"- {' '.join(str(t).split())[:200]}" for t in pending)
            self._note(f"A restart interrupted {len(pending)} task(s). They were not re-run; send them again if you still want them:\n{tasks}", alert="info")
            self.save_state()
        self.consumer = asyncio.create_task(self._consume())

    def _spawn(self, coro):
        task = asyncio.create_task(coro)
        self.background.add(task)
        task.add_done_callback(self.background.discard)

    async def teardown(self):
        consumer = getattr(self, "consumer", None)
        if consumer:
            consumer.cancel()
        await self._disconnect()

    async def _disconnect(self):
        if getattr(self, "client", None) is not None:
            try:
                await self.client.disconnect()
            except Exception as e:
                logger.warning("disconnect: %s", e)
            self.client = None

    def _enqueue(self, text: str) -> str:
        waiting = self.busy or not self.queue.empty()
        self.state["active"] = now()
        self.state.setdefault("pending", []).append(text)
        self.save_state()
        self.queue.put_nowait(text)
        self._publish()
        return "Queued; it starts when the current task finishes." if waiting else "Working on it."

    async def _consume(self):
        while not self._shutdown_event.is_set():
            text = await self.queue.get()
            error = None
            try:
                await self._turn(text)
            except Exception as e:
                logger.exception("task failed")
                error = e
                self.busy, self.prompt = False, None
            try:
                self.state["pending"] = (self.state.get("pending") or [])[1:]
                self.save_state()
                if error is not None:
                    self._note(f"This task failed with an error and was not re-run: {error}\n\n{text[:300]}", alert="error")
                    self._publish()
            except Exception:
                logger.exception("could not record the end of the task")

    async def _stop(self) -> str:
        while not self.queue.empty():
            self.queue.get_nowait()
        self.state["pending"] = (self.state.get("pending") or [])[:int(self.busy)]
        self.save_state()
        if self.busy and self.client is not None:
            await self.client.interrupt()
            return "Stopping."
        return "Nothing is running."

    async def _turn(self, text: str):
        from claude_agent_sdk import AssistantMessage, ClaudeSDKClient, ResultMessage, TextBlock, ToolUseBlock

        self._before_turn()
        self.busy, self.prompt, self.started = True, text, now()
        self.record("prompt", {"session": self.label, "text": text}, title=f"{self.label}: {text[:80]}")
        self._publish()
        steps, answer, error = [], "", None
        try:
            if self.client is None:
                self.client = ClaudeSDKClient(options=self._options())
                await self.client.connect()
            await self.client.query(text)
            async for message in self.client.receive_response():
                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, ToolUseBlock):
                            step = {"tool": block.name.replace("mcp__team__", ""), "summary": tool_summary(block.input, str(self.cwd))}
                        elif isinstance(block, TextBlock) and block.text.strip():
                            step = {"tool": "text", "summary": block.text.strip()[:400]}
                        else:
                            continue
                        steps.append(step)
                        self.record("step", {"session": self.label, **step}, title=f"{self.label}: {step['tool']}")
                elif isinstance(message, ResultMessage):
                    self.state["claude_id"] = message.session_id
                    answer = message.result or answer
                    if message.is_error:
                        error = answer or message.subtype
        except Exception as e:
            logger.exception("turn failed")
            error = str(e)
            await self._disconnect()
        if error and re.search(r"log ?in|authenticat|401|api key", error, re.I):
            error = f"{error}\n\n{AUTH_HINT}"
        self.busy, self.prompt = False, None
        self.state["active"] = now()
        self.state["last_answer"] = (answer or error or "")[:600]
        self.save_state()
        self.record(
            "turn",
            {"session": self.label, "prompt": text, "steps": steps[-150:], "answer_md": answer, "error": error, "started": self.started, "finished": now()},
            title=f"{self.label}: {(answer or error or 'Done').strip().splitlines()[0][:90]}",
            alert="error" if error else None,
        )
        self._publish()

    def _note(self, text: str, alert=None):
        self.record("note", {"session": self.label, "text": text}, title=f"{self.label}: {text.splitlines()[0][:90]}", alert=alert)

    def _before_turn(self):
        pass


class Session(DevBase):
    async def setup(self):
        self.load_state()
        self.cwd = None
        self.starting = False
        self.platform = Platform()
        self.label = SESSION_NAME or "session"
        repo, missing, hints = repo_check()
        if not NAME.match(SESSION_NAME):
            missing.append("DEV_SESSION")
            hints.append("DEV_SESSION must be a branch-style name; start sessions from the lead")
        if missing:
            self.needs_setup(missing, "; ".join(hints))
            return
        self.clear_needs_setup()
        self.repo = repo
        path = repo.parent / f"{repo.name}.worktrees" / SESSION_NAME
        if not path.is_dir():
            path.parent.mkdir(parents=True, exist_ok=True)
            if run_cmd(repo, ["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{SESSION_NAME}"])[0] == 0:
                run_git(repo, "worktree", "add", str(path), SESSION_NAME)
                origin = "its existing branch"
            else:
                start = self._fresh_base()
                run_git(repo, "worktree", "add", "--no-track", "-b", SESSION_NAME, str(path), start)
                origin = f"`{start}`"
            copy_env_files(repo, path)
            self.slot = claim_slot(str(path))
            env = slot_env(self.slot)
            self._note(f"Worktree created from {origin}. App ports {env['PORT']} and {env['API_PORT']}.")
        else:
            code, out, _ = run_cmd(path, ["git", "rev-parse", "--show-toplevel", "--git-common-dir"])
            found = out.splitlines()
            common = run_cmd(repo, ["git", "rev-parse", "--git-common-dir"])[1]
            if code != 0 or len(found) != 2 or not os.path.samefile(found[0], path) or not os.path.samefile(path / found[1], repo / common):
                self.needs_setup("worktree", f"The folder {path} already exists but is not a git worktree of {repo}. Move or delete that folder, then restart this session."[:500])
                return
            copy_env_files(repo, path)
            self.slot = claim_slot(str(path))
        self.cwd = path
        self._init_turns()
        self._publish(force=True)
        logger.info("session %s ready in %s, slot %s, Claude via %s", SESSION_NAME, path, self.slot, auth_mode())

    def _fresh_base(self) -> str:
        remote = f"origin/{BASE_BRANCH}"
        if run_cmd(self.repo, ["git", "fetch", "origin", BASE_BRANCH], timeout=300)[0] != 0:
            return BASE_BRANCH
        if run_cmd(self.repo, ["git", "merge-base", "--is-ancestor", remote, BASE_BRANCH])[0] == 0:
            return BASE_BRANCH
        return remote

    def _options(self):
        from claude_agent_sdk import ClaudeAgentOptions

        env = slot_env(self.slot)
        branch = run_git(self.cwd, "rev-parse", "--abbrev-ref", "HEAD")
        append = SESSION_PROMPT.format(branch=branch, base=BASE_BRANCH, port=env["PORT"], api_port=env["API_PORT"])
        if BRIEF:
            append += "\n\n" + BRIEF
        return ClaudeAgentOptions(
            cwd=str(self.cwd), permission_mode=PERMISSION_MODE, resume=self.state.get("claude_id"), model=MODEL,
            allowed_tools=EDIT_TOOLS, env=env, setting_sources=["user", "project", "local"],
            system_prompt={"type": "preset", "preset": "claude_code", "append": append},
        )

    async def on_command(self, command: dict):
        text = (command.get("text") or "").strip()
        if self.cwd is None:
            return "This session is waiting for configuration; start sessions from the Claude Dev lead."
        word = text.lower()
        if not text or word == "status":
            return self._line()
        if word == "stop":
            return await self._stop()
        if word == "up":
            if self.starting:
                return "The app is already starting. Wait for it to finish."
            self.starting = True
            self._spawn(self._up())
            return f"Starting the app on port {slot_env(self.slot)['PORT']}."
        if word == "down":
            return self._down()
        if word == "diff":
            stat = run_git(self.cwd, "diff", "--stat", f"{BASE_BRANCH}...HEAD") or "(no committed changes)"
            working = run_git(self.cwd, "diff", "--stat") or "(working tree clean)"
            return {"text": f"Committed vs {BASE_BRANCH}:\n{stat}\n\nUncommitted:\n{working}", "diff_md": f"```\n{stat}\n```\n\nUncommitted:\n```\n{working}\n```"}
        if word in ("sync", "pr", "close") and self.busy:
            return "Still working. Wait for it to finish, or stop it first."
        if word in ("sync", "pr", "close") and self.starting:
            return "The app is still starting. Wait for it to finish, then try again."
        if word == "sync":
            return self._sync()
        if word == "pr":
            return self._pr()
        if word == "close":
            return await self._close()
        return self._enqueue(text)

    def _tracked_changes(self) -> bool:
        return bool(run_git(self.cwd, "status", "--porcelain", "--untracked-files=no"))

    def _ahead_behind(self):
        code, out, _ = run_cmd(self.cwd, ["git", "rev-list", "--left-right", "--count", f"{BASE_BRANCH}...HEAD"])
        if code != 0 or not out:
            return 0, 0
        behind, ahead = (int(x) for x in out.split())
        return ahead, behind

    def _before_turn(self):
        if self._tracked_changes():
            return
        ahead, behind = self._ahead_behind()
        if ahead or not behind:
            return
        if run_cmd(self.cwd, ["git", "merge", "--ff-only", BASE_BRANCH])[0] == 0:
            self._note(f"Caught up with `{BASE_BRANCH}` ({behind} new commits).")

    def _sync(self) -> str:
        if self._tracked_changes():
            return "There are uncommitted changes. Ask the session to commit first, then sync again."
        ahead, behind = self._ahead_behind()
        if not behind:
            return f"Already up to date with `{BASE_BRANCH}`."
        if not ahead:
            run_git(self.cwd, "merge", "--ff-only", BASE_BRANCH)
            return f"Fast-forwarded to `{BASE_BRANCH}` ({behind} new commits)."
        code, out, err = run_cmd(self.cwd, ["git", "rebase", BASE_BRANCH], timeout=300)
        if code != 0:
            conflicts = run_git(self.cwd, "diff", "--name-only", "--diff-filter=U")
            run_cmd(self.cwd, ["git", "rebase", "--abort"])
            return f"Rebasing onto `{BASE_BRANCH}` conflicts, so nothing changed. Ask the session to rebase and resolve:\n{conflicts or (err or out)[:500]}"
        return f"Rebased {ahead} commit(s) onto `{BASE_BRANCH}` ({behind} new commits underneath)."

    def _pr(self) -> str:
        branch = run_git(self.cwd, "rev-parse", "--abbrev-ref", "HEAD")
        if self._tracked_changes():
            return "There are uncommitted changes. Ask the session to commit first, then open the PR again."
        if run_cmd(self.cwd, ["git", "fetch", "origin", BASE_BRANCH], timeout=300)[0] != 0:
            return f"Could not fetch `origin/{BASE_BRANCH}`. Check the repository has an `origin` remote you can reach."
        code, out, err = run_cmd(self.cwd, ["git", "rebase", f"origin/{BASE_BRANCH}"], timeout=300)
        if code != 0:
            conflicts = run_git(self.cwd, "diff", "--name-only", "--diff-filter=U")
            run_cmd(self.cwd, ["git", "rebase", "--abort"])
            files = conflicts or (err or out)[:500]
            self._note(f"Rebase conflict, nothing pushed:\n{files}", alert="error")
            return f"Rebasing onto `{BASE_BRANCH}` hit conflicts, so nothing was pushed. Ask the session to rebase and resolve:\n{files}"
        if run_git(self.cwd, "rev-list", "--count", f"origin/{BASE_BRANCH}..HEAD") == "0":
            return f"`{branch}` has nothing that isn't already in `{BASE_BRANCH}`. Close this session when you're done with it."
        run_git(self.cwd, "push", "-u", "--force-with-lease", "--force-if-includes", "origin", branch, timeout=300)
        code, url, _ = run_cmd(self.cwd, ["gh", "pr", "list", "--head", branch, "--state", "open", "--json", "url", "--jq", ".[0].url // empty"])
        opened = False
        if code != 0 or not url:
            code, out, err = run_cmd(self.cwd, ["gh", "pr", "create", "--fill", "--base", BASE_BRANCH, "--head", branch], timeout=300)
            if code != 0:
                self._note(f"Pushed `{branch}` but could not open the pull request: {err or out}", alert="error")
                return f"Pushed `{branch}` but could not open the pull request: {err or out}"
            url, opened = out.splitlines()[-1].strip(), True
        self.state["pr_url"] = url
        self.save_state()
        self.record("pr", {"session": self.label, "url": url, "branch": branch, "text": f"Pull request: {url}"}, title=f"PR for {branch}", alert="info")
        self._publish()
        return f"{'Opened' if opened else 'Updated'} {url}"

    async def _close(self) -> str:
        if run_git(self.cwd, "status", "--porcelain"):
            return "There are uncommitted changes or new files. Ask the session to commit or discard them first."
        branch = run_git(self.cwd, "rev-parse", "--abbrev-ref", "HEAD")
        unmerged = self._ahead_behind()[0]
        self._down()
        try:
            run_git(self.repo, "worktree", "remove", "--force", str(self.cwd))
        except RuntimeError as e:
            return f"Could not remove the worktree: {e}"
        release_slot(str(self.cwd))
        kept = f"Branch `{branch}` is kept with {unmerged} commit(s) not in `{BASE_BRANCH}`." if unmerged else f"Branch `{branch}` is kept."
        self._note(f"Session closed. {kept}")
        self.record("session", {**self._view(closed=True)}, title=f"{self.label}: closed")
        self.cwd = None
        if self.platform.ready:
            self._spawn(self._remove_self())
        return f"Closed. {kept} This worker removes itself from the canvas in a moment."

    async def _remove_self(self):
        await asyncio.sleep(5)
        try:
            await self.platform.acall("DELETE", f"/api/v2/agents/{self.config.agent_id}")
        except Exception as e:
            logger.warning("could not delete this session worker: %s", e)
            return
        self._shutdown_event.set()

    async def _detect_app(self) -> dict:
        from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ResultMessage, TextBlock, query

        options = ClaudeAgentOptions(cwd=str(self.cwd), allowed_tools=["Read", "Glob", "Grep"], max_turns=25, model=MODEL)
        reply = ""
        async for message in query(prompt=DETECT_PROMPT, options=options):
            if isinstance(message, ResultMessage) and message.result:
                reply = message.result
            elif isinstance(message, AssistantMessage):
                reply = "".join(b.text for b in message.content if isinstance(b, TextBlock)) or reply
        found = parse_json_object(reply) or {}
        return {"setup": str(found.get("setup") or "").strip(), "up": str(found.get("up") or "").strip(), "path": str(found.get("path") or "/").strip() or "/"}

    async def _app_commands(self) -> dict:
        detected = self.state.get("app_commands") or {}
        if not UP_COMMAND and not detected.get("up"):
            detected = await self._detect_app()
            self.state["app_commands"] = detected
            self.save_state()
        return {
            "setup": SETUP_COMMAND or detected.get("setup", ""),
            "up": UP_COMMAND or detected.get("up", ""),
            "path": detected.get("path", "/") if not UP_COMMAND else "/",
        }

    async def _run_logged(self, command: str, env: dict, log: Path, timeout: int) -> int:
        with open(log, "ab") as out:
            out.write(f"\n$ {command}\n".encode())
            out.flush()
            proc = await asyncio.create_subprocess_shell(command, cwd=str(self.cwd), env=env, stdout=out, stderr=subprocess.STDOUT, executable="/bin/bash")
            try:
                return await asyncio.wait_for(proc.wait(), timeout)
            except asyncio.TimeoutError:
                proc.kill()
                return -1

    async def _up(self):
        try:
            self._note(await self._start_app())
        except Exception as e:
            logger.exception("up failed")
            self._note(f"Starting the app failed: {e}", alert="error")
        finally:
            self.starting = False
        self._publish()

    async def _start_app(self) -> str:
        if self.state.get("app_pid") and pid_alive(self.state["app_pid"]):
            return f"The app is already running at {self.state.get('app_url')}."
        self._before_turn()
        commands = await self._app_commands()
        if not commands["up"]:
            self.state["app_commands"] = None
            self.save_state()
            return "I could not work out how to start this project's app. Set DEV_UP_COMMAND (and DEV_SETUP_COMMAND if it needs one) on the lead, then start a new session."
        env = {**os.environ, **slot_env(self.slot)}
        log = self.config.data_dir / "app.log"
        if commands["setup"] and self.state.get("setup_done") != commands["setup"]:
            code = await self._run_logged(commands["setup"], env, log, SETUP_TIMEOUT_SECONDS)
            if code != 0:
                return f"Installing dependencies failed (`{commands['setup']}`, exit {code}):\n```\n{log_tail(log)}\n```"
            self.state["setup_done"] = commands["setup"]
        if SEED_COMMAND and self.state.get("seed_done") != SEED_COMMAND:
            code = await self._run_logged(SEED_COMMAND, env, log, SETUP_TIMEOUT_SECONDS)
            if code != 0:
                return f"Preparing this session's data failed (`{SEED_COMMAND}`, exit {code}):\n```\n{log_tail(log)}\n```"
            self.state["seed_done"] = SEED_COMMAND
            self.save_state()
        with open(log, "ab") as out:
            out.write(f"\n$ {commands['up']}\n".encode())
        proc = subprocess.Popen(commands["up"], shell=True, executable="/bin/bash", cwd=str(self.cwd), env=env, stdout=open(log, "ab"), stderr=subprocess.STDOUT, start_new_session=True)
        port = int(env["PORT"])
        path = commands["path"] if commands["path"].startswith("/") else "/" + commands["path"]
        self.state["app_pid"], self.state["app_url"] = proc.pid, f"http://localhost:{port}{path}"
        self.save_state()
        self._publish()
        waited = 0
        while waited < APP_START_SECONDS and proc.poll() is None and not port_listening(port):
            await asyncio.sleep(2)
            waited += 2
        if proc.poll() is not None:
            self.state["app_pid"], self.state["app_url"] = None, None
            if not UP_COMMAND:
                self.state["app_commands"] = None
            self.save_state()
            return f"The app exited (`{commands['up']}`, code {proc.returncode}):\n```\n{log_tail(log)}\n```"
        if not port_listening(port):
            return f"Started `{commands['up']}`, but nothing answers on port {port} yet. It may still be building."
        return f"App running at {self.state['app_url']}."

    def _down(self) -> str:
        if DOWN_COMMAND:
            subprocess.run(DOWN_COMMAND, shell=True, cwd=str(self.cwd), env={**os.environ, **slot_env(self.slot)}, timeout=300)
        pid = self.state.get("app_pid")
        if pid and pid_alive(pid):
            try:
                os.killpg(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            except OSError as e:
                return f"Could not stop the app (pid {pid}): {e}"
        self.state["app_pid"], self.state["app_url"] = None, None
        self.save_state()
        self._publish()
        return "App stopped."

    def _view(self, closed: bool = False) -> dict:
        env = slot_env(self.slot)
        view = {
            "name": self.label, "repo": str(self.repo), "base": BASE_BRANCH, "auth": auth_mode(), "closed": closed,
            "slot": self.slot, "port": int(env["PORT"]), "api_port": int(env["API_PORT"]),
            "pr_url": self.state.get("pr_url"), "working": self.busy, "queued": self.queue.qsize(),
            "prompt": self.prompt, "started": self.started, "active": self.state.get("active"),
            "last_answer": self.state.get("last_answer"),
        }
        if closed or self.cwd is None:
            return view
        ahead, behind = self._ahead_behind()
        view.update({
            "path": str(self.cwd), "branch": run_git(self.cwd, "rev-parse", "--abbrev-ref", "HEAD"),
            "dirty": len([l for l in run_git(self.cwd, "status", "--porcelain").splitlines() if l.strip()]),
            "ahead": ahead, "behind": behind,
            "last_commit": run_git(self.cwd, "log", "-1", "--format=%s"),
            "app_url": self.state.get("app_url") if pid_alive(self.state.get("app_pid")) else None,
        })
        return view

    def _line(self) -> str:
        v = self._view()
        state = "working" if v["working"] else "idle"
        dirty = f"{v['dirty']} changed file(s)" if v["dirty"] else "clean"
        extra = (f", app at {v['app_url']}" if v["app_url"] else "") + (f", PR {v['pr_url']}" if v["pr_url"] else "")
        return f"On `{v['branch']}`: {state}, {dirty}, {v['ahead']} ahead / {v['behind']} behind `{BASE_BRANCH}`, ports {v['port']}/{v['api_port']}{extra}."

    def _publish(self, force: bool = False):
        if self.cwd is None:
            return
        view = self._view()
        if not force and view == self.state.get("view"):
            return
        self.state["view"] = view
        self.save_state()
        self.record("session", view, title=f"{self.label}: {'working' if view['working'] else 'idle'} on {view['branch']}")

    @phase(every_minutes=1)
    async def observe_git(self):
        self._publish()


class Lead(DevBase):
    async def setup(self):
        self.load_state()
        self.state.setdefault("sessions", {})
        self.cwd = None
        self.team = []
        self.platform = Platform()
        repo, missing, hints = repo_check()
        if not self.platform.ready:
            missing.append("Dispatcher")
            hints.append("run this worker from Dispatcher (the desktop app, or `dispatcher run --remote <id>`) so it can create session workers")
        if missing:
            self.needs_setup(missing, "; ".join(hints))
            return
        self.clear_needs_setup()
        self.repo = repo
        self.cwd = repo
        self._init_turns()
        await self._refresh_team(force=True)
        logger.info("lead ready on %s with %d session(s), Claude via %s", repo, len(self.state["sessions"]), auth_mode())

    def _options(self):
        from claude_agent_sdk import ClaudeAgentOptions

        append = LEAD_PROMPT.format(repo=self.repo, base=BASE_BRANCH, worktrees=f"{self.repo.parent}/{self.repo.name}.worktrees")
        if BRIEF:
            append += "\n\n" + BRIEF
        return ClaudeAgentOptions(
            cwd=str(self.repo), permission_mode=PERMISSION_MODE, resume=self.state.get("claude_id"), model=MODEL,
            allowed_tools=LEAD_TOOLS, disallowed_tools=["Edit", "Write", "MultiEdit", "NotebookEdit"],
            mcp_servers={"team": self._team_server()}, setting_sources=["user", "project", "local"],
            system_prompt={"type": "preset", "preset": "claude_code", "append": append},
        )

    def _team_server(self):
        from claude_agent_sdk import create_sdk_mcp_server, tool

        def text(value):
            return {"content": [{"type": "text", "text": value if isinstance(value, str) else json.dumps(value, indent=1)}]}

        @tool("list_sessions", "Every session: name, branch, working or idle, current or last task, last answer, commits ahead, uncommitted files, ports, app URL, pull request, worktree path.", {})
        async def list_sessions(args):
            await self._refresh_team()
            return text(self.team or "No sessions yet.")

        @tool("session_history", "A session's most recent tasks and answers, newest first.", {"name": str, "limit": int})
        async def session_history(args):
            entry = self.state["sessions"].get(args.get("name", ""))
            if not entry:
                return text(f"No session called {args.get('name')}.")
            limit = max(1, min(int(args.get("limit") or 5), 20))
            res = await self.platform.acall("GET", f"/api/v2/agents/{entry['agent_id']}/records?kind=turn&limit={limit}")
            turns = [{"asked": r["payload"].get("prompt"), "answer": (r["payload"].get("answer_md") or r["payload"].get("error") or "")[:2000], "finished": r["payload"].get("finished")} for r in res.get("records", [])]
            return text(turns or "No finished tasks yet.")

        @tool("send_to_session", "Send a task, or one of up, down, diff, sync, pr, status, to a session.", {"name": str, "text": str})
        async def send_to_session(args):
            return text(await self._forward(args.get("name", ""), args.get("text", "")))

        @tool("new_session", "Start a new session (a new worker with its own worktree, branch and ports) and give it a task.", {"name": str, "task": str})
        async def new_session(args):
            return text(await self._create(args.get("name", ""), args.get("task", "")))

        @tool("close_session", "Close a session: stop its app, remove its worktree and its worker, keep its branch. Refused by the session while it has uncommitted changes or is working.", {"name": str})
        async def close_session(args):
            return text(await self._forward(args.get("name", ""), "close"))

        return create_sdk_mcp_server("team", tools=[list_sessions, session_history, send_to_session, new_session, close_session])

    async def on_command(self, command: dict):
        text = (command.get("text") or "").strip()
        if self.cwd is None:
            return "This worker is waiting for configuration. Set DEV_REPO to the path of a git repository on this machine."
        if not text or text.lower() in ("sessions", "status"):
            await self._refresh_team()
            return self._team_text()
        if text.lower() == "stop":
            return await self._stop()
        if text.startswith("+"):
            name, _, task = text[1:].partition(" ")
            return await self._create(name.strip() or slugify(task), task.strip())
        if text.startswith("@"):
            name, _, rest = text[1:].partition(" ")
            return await self._forward(name, rest.strip())
        verb, _, target = text.partition(" ")
        if verb.lower() in ("review", "close") and target.strip() in self.state["sessions"]:
            if verb.lower() == "close":
                return await self._forward(target.strip(), "close")
            return self._enqueue(REVIEW_PROMPT.format(name=target.strip(), base=BASE_BRANCH))
        return self._enqueue(text)

    async def _forward(self, name: str, text: str) -> str:
        entry = self.state["sessions"].get(name)
        if not entry:
            return f"There is no session called `{name}`. {self._team_text()}"
        if not text:
            return f"Tell `{name}` what to do."
        await self.platform.acall("POST", f"/api/v2/agents/{entry['agent_id']}/command", {"text": text, "meta": {"via": "lead"}})
        self._note(f"Sent to `{name}`: {text[:200]}")
        return f"Sent to `{name}`."

    async def _create(self, name: str, task: str) -> str:
        if name in self.state["sessions"]:
            return f"A session called `{name}` already exists."
        if not NAME.match(name) or run_cmd(self.repo, ["git", "check-ref-format", "--branch", name])[0] != 0:
            return f"`{name}` can't be a session name. Use letters, numbers, dashes or dots, e.g. `+fix-login-redirect`."
        try:
            me = await self.platform.acall("GET", f"/api/v2/agents/{self.config.agent_id}/manifest")
            base_id = (me.get("manifest") or me).get("base_id") or ""
            body = {"name": name, "goal": f"Claude Code session `{name}` on {self.repo.name}.", "base_id": base_id}
            try:
                created = await self.platform.acall("POST", "/api/v2/agents/create", {**body, "template_id": "claude-dev"})
            except PlatformError:
                created = await self.platform.acall("POST", "/api/v2/agents/create", body)
            agent_id = created["agent"]["id"]
            await self.platform.acall("PUT", f"/api/agents/{agent_id}/schedule", {"mode": "local"})
            self.state["sessions"][name] = {"agent_id": agent_id, "created": now()}
            self.save_state()
            for rel in SESSION_FILES:
                await self.platform.acall("PUT", f"/api/v2/agents/{agent_id}/code", {"filename": rel, "code": (HERE / rel).read_text()})
            env = {key: os.environ.get(key, "") for key in SHARED_ENV}
            env.update({"DEV_ROLE": "session", "DEV_SESSION": name, "DEV_LEAD": self.config.agent_id})
            await self.platform.acall("PUT", f"/api/v2/agents/{agent_id}/env", {"env_vars": env, "merge": False})
            await self.platform.acall("PUT", f"/api/v2/agents/{agent_id}/manifest", {"commands": SESSION_COMMANDS, "tags": ["dev", "claude-code", "session"], "tick_interval": 60})
            if task:
                await self.platform.acall("POST", f"/api/v2/agents/{agent_id}/command", {"text": task, "meta": {"via": "lead"}})
        except Exception as e:
            logger.exception("creating session %s failed", name)
            return f"Could not create `{name}`: {e}"
        self._note(f"Started session `{name}`" + (f": {task[:200]}" if task else "."))
        await self._refresh_team(force=True)
        return f"Session `{name}` created. It gets its own hex and starts on this computer in a few seconds" + (", then works on the task." if task else ".")

    async def _refresh_team(self, force: bool = False):
        team = []
        for name, entry in list(self.state["sessions"].items()):
            agent_id = entry["agent_id"]
            try:
                res = await self.platform.acall("GET", f"/api/v2/agents/{agent_id}/records?kind=session&limit=1")
                status = await self.platform.acall("GET", f"/api/v2/agents/{agent_id}/status")
            except PlatformError as e:
                if e.status == 404:
                    self.state["sessions"].pop(name, None)
                    self.save_state()
                continue
            except Exception as e:
                logger.warning("team refresh for %s failed: %s", name, e)
                continue
            view = (res.get("records") or [{}])[0].get("payload") or {}
            if view.get("closed"):
                continue
            alive = bool((status.get("local") or {}).get("alive"))
            team.append({**view, "name": name, "agent_id": agent_id, "alive": alive, "working": bool(view.get("working")) and alive, "created": entry.get("created")})
        self.team = team
        roster = {"repo": str(self.repo), "base": BASE_BRANCH, "auth": auth_mode(), "sessions": team, "lead": {"working": self.busy, "queued": self.queue.qsize(), "prompt": self.prompt, "started": self.started}}
        if not force and roster == self.state.get("roster"):
            return
        self.state["roster"] = roster
        self.save_state()
        working = sum(1 for s in team if s.get("working"))
        self.record("team", roster, title=f"{len(team)} session(s), {working} working")

    def _publish(self, force: bool = False):
        self._spawn(self._refresh_team(force=force))

    def _team_text(self) -> str:
        if not self.team:
            return "No sessions yet. Start one with `+<name> <task>`."
        lines = []
        for s in self.team:
            state = "working" if s.get("working") else "idle" if s.get("alive") else "not running"
            pr = f", PR {s['pr_url']}" if s.get("pr_url") else ""
            lines.append(f"`{s['name']}`: {state}, {s.get('ahead', 0)} ahead, {s.get('dirty', 0)} changed, port {s.get('port')}{pr}")
        return "\n".join(lines)

    @phase()
    async def watch(self):
        if self.cwd is not None:
            await self._refresh_team()


if __name__ == "__main__":
    asyncio.run((Session if ROLE == "session" else Lead)().run())
