#!/usr/bin/env python3
"""
Validate agent *directories* against the DispatchKit contract.

Self-contained on purpose: no imports from backend/src, no third-party deps, so
the same file ships into the public agent directory repo and runs as its PR
gate. (verify_registry.py is the sibling check for packages already in the
registry DB; this one checks source trees before they get there.)

Usage:
  python validate_agents.py backend/agents/*/        # explicit dirs
  python validate_agents.py --root agents            # every agent under a root
  python validate_agents.py --root agents --smoke    # also boot each agent
"""

import argparse
import ast
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# Installed in the agent base image (backend/Dockerfile.agent). Anything else in
# requirements.txt installs at boot, which is allowed but slows every start.
BAKED = {
    "aiohttp", "anthropic", "arxiv", "beautifulsoup4", "boto3", "bs4", "docker",
    "fastapi", "feedparser", "httpx", "lxml", "dispatchkit", "pandas", "psutil",
    "psycopg2-binary", "pydantic", "pygithub", "pypdf2", "python-dotenv",
    "redis", "requests", "selenium", "supabase", "uvicorn",
    "webdriver-manager", "yfinance",
}

SECRET_KEY_RE = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|WEBHOOK_URL|WEBHOOK)S?$", re.I)
EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.\w+")
SLUG_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{1,38}[a-z0-9])?$")

REQUIRED_MANIFEST_KEYS = ("id", "name", "entrypoint", "goal")
NEVER_COMMIT = ("data", "__pycache__", ".venv", "venv", "node_modules", ".env")
MAX_FILE_BYTES = 512 * 1024


class Problems:
    def __init__(self):
        self.errors: list = []
        self.warnings: list = []

    def error(self, msg: str):
        self.errors.append(msg)

    def warn(self, msg: str):
        self.warnings.append(msg)


def subclasses_agent(code: str) -> bool:
    """Whether the source subclasses dispatchkit.Agent (what gates the chat loop)."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for base in node.bases:
            if isinstance(base, ast.Name) and base.id == "Agent":
                return True
            if isinstance(base, ast.Attribute) and base.attr == "Agent":
                return True
    return False


def check_manifest(agent_dir: Path, p: Problems) -> dict:
    manifest_path = agent_dir / "agent.json"
    if not manifest_path.is_file():
        p.error("agent.json is missing")
        return {}
    try:
        manifest = json.loads(manifest_path.read_text())
    except json.JSONDecodeError as e:
        p.error(f"agent.json is not valid JSON: {e}")
        return {}

    for key in REQUIRED_MANIFEST_KEYS:
        if not str(manifest.get(key, "")).strip():
            p.error(f"agent.json is missing '{key}'")

    if not SLUG_RE.match(agent_dir.name):
        p.error(f"directory name '{agent_dir.name}' must be lowercase kebab-case")
    if manifest.get("id") and manifest["id"] != agent_dir.name:
        p.error(f"agent.json id '{manifest['id']}' does not match the directory name '{agent_dir.name}'")

    goal = str(manifest.get("goal", ""))
    if goal and len(goal) < 40:
        p.warn("goal is very short — it is what the marketplace and the LLM both read")
    if not manifest.get("tags"):
        p.warn("no tags — the agent will be hard to find")

    if (manifest.get("coordination") or {}).get("event_bus_url"):
        p.error("coordination.event_bus_url must be null (there is no event bus in prod)")

    tick = manifest.get("tick_interval")
    if isinstance(tick, (int, float)) and tick < 5:
        p.warn(f"tick_interval of {tick}s is very aggressive; phase timestamps are the way to schedule work")

    for key, value in (manifest.get("env_vars") or {}).items():
        value = str(value or "")
        if not value:
            continue
        if SECRET_KEY_RE.search(key):
            p.error(f"env var '{key}' ships a value — secrets must default to empty")
        elif EMAIL_RE.search(value):
            p.error(f"env var '{key}' ships an email address — no PII in defaults")

    return manifest


def tracked_runtime_state(agent_dir: Path) -> set:
    """Runtime paths that git is actually tracking (empty when git is absent)."""
    try:
        out = subprocess.run(
            ["git", "ls-files", "--", *NEVER_COMMIT],
            cwd=agent_dir, capture_output=True, text=True, timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return set()
    if out.returncode != 0:
        return set()
    return {line.split("/")[0] + "/" for line in out.stdout.splitlines() if line.strip()}


def check_files(agent_dir: Path, manifest: dict, p: Problems) -> None:
    entrypoint = manifest.get("entrypoint", "main.py")
    entry_path = agent_dir / entrypoint
    if not entry_path.is_file():
        p.error(f"entrypoint '{entrypoint}' does not exist")

    # Runtime state (data/, __pycache__) exists in any working checkout and is
    # gitignored; it is only a problem when it is actually tracked.
    for offender in sorted(tracked_runtime_state(agent_dir)):
        p.error(f"{offender} is committed but is runtime state — it belongs in .gitignore")

    for root, dirs, names in os.walk(agent_dir):
        dirs[:] = [d for d in dirs if d not in NEVER_COMMIT]
        for name in names:
            path = Path(root) / name
            rel = path.relative_to(agent_dir).as_posix()
            if path.suffix == ".pyc":
                p.error(f"{rel} should not be committed")
                continue
            if path.stat().st_size > MAX_FILE_BYTES:
                p.warn(f"{rel} is {path.stat().st_size // 1024}KB — large files belong in the agent's data dir")
            if path.suffix == ".py":
                try:
                    ast.parse(path.read_text(encoding="utf-8", errors="replace"), filename=rel)
                except SyntaxError as e:
                    p.error(f"{rel} does not compile: {e}")

    if entry_path.is_file():
        code = entry_path.read_text(encoding="utf-8", errors="replace")
        is_sdk = subclasses_agent(code)
        if not is_sdk:
            p.error(f"{entrypoint} does not subclass dispatchkit.Agent")
        if manifest.get("chat_enabled") is True and not is_sdk:
            p.error("chat_enabled is true but the agent has no SDK inbox loop, so chat can never reply")
        if re.search(r"^\s*while\s+True\s*:", code, re.M):
            p.warn("looks like a hand-rolled `while True` loop — the SDK owns the tick loop")

    reqs_path = agent_dir / "requirements.txt"
    if reqs_path.is_file():
        for line in reqs_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            pkg = re.split(r"[<>=!\[]", line)[0].strip().lower()
            if pkg and pkg not in BAKED:
                p.warn(f"requirement '{line}' is not in the base image and will install on every cold start")


def smoke(agent_dir: Path, sdk_path: str, seconds: float, p: Problems) -> None:
    """Boot the agent with the LLM disabled and require it to survive startup."""
    entry = agent_dir / "main.py"
    if not entry.is_file():
        return
    env = {
        **os.environ,
        "LLM_DISABLED": "1",
        "DRY_RUN": "1",
        "AGENT_API_PORT": "0",
        "PYTHONPATH": f"{sdk_path}:{os.environ.get('PYTHONPATH', '')}".rstrip(":"),
    }
    with tempfile.TemporaryDirectory() as data_dir:
        env["AGENT_DATA_DIR"] = data_dir
        proc = subprocess.Popen(
            [sys.executable, "-u", "main.py"], cwd=agent_dir, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        deadline = time.time() + seconds
        while time.time() < deadline:
            if proc.poll() is not None:
                out = (proc.stdout.read() or "")[-1500:]
                p.error(f"exited during startup (code {proc.returncode}):\n{out}")
                return
            time.sleep(0.25)
        proc.terminate()
        try:
            out = proc.communicate(timeout=10)[0] or ""
        except subprocess.TimeoutExpired:
            proc.kill()
            out = proc.communicate()[0] or ""
        if "Traceback" in out:
            p.error("traceback during startup:\n" + out[-1500:])


HTTP_FAILURE_RE = re.compile(r"Client Error|Server Error|HTTPError|ConnectionError|ConnectTimeout|ReadTimeout|No module named", re.I)


def live(agent_dir: Path, manifest: dict, sdk_path: str, seconds: float, p: Problems) -> None:
    """Run the agent against its real data sources (LLM stubbed) with the
    manifest's default env, and require at least one record — or an explicit
    needs_setup signal — before the deadline."""
    entry = agent_dir / "main.py"
    if not entry.is_file():
        return
    env = {
        **os.environ,
        **{k: str(v or "") for k, v in (manifest.get("env_vars") or {}).items()},
        "LLM_DISABLED": "1",
        "DRY_RUN": "1",
        "AGENT_API_PORT": "0",
        "PYTHONPATH": f"{sdk_path}:{os.environ.get('PYTHONPATH', '')}".rstrip(":"),
    }
    with tempfile.TemporaryDirectory() as data_dir:
        env["AGENT_DATA_DIR"] = data_dir
        records_path = Path(data_dir) / "records.jsonl"
        status_path = Path(data_dir) / "status.json"
        proc = subprocess.Popen(
            [sys.executable, "-u", "main.py"], cwd=agent_dir, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        deadline = time.time() + seconds
        while time.time() < deadline:
            if proc.poll() is not None:
                out = (proc.stdout.read() or "")[-1500:]
                p.error(f"exited during live run (code {proc.returncode}):\n{out}")
                return
            if records_path.is_file() and records_path.stat().st_size > 0:
                break
            if status_path.is_file():
                try:
                    if json.loads(status_path.read_text()).get("state") == "needs_setup":
                        break
                except (json.JSONDecodeError, OSError):
                    pass
            time.sleep(0.5)
        proc.terminate()
        try:
            out = proc.communicate(timeout=10)[0] or ""
        except subprocess.TimeoutExpired:
            proc.kill()
            out = proc.communicate()[0] or ""

        if "Traceback" in out:
            p.error("traceback during live run:\n" + out[-1500:])

        records = []
        if records_path.is_file():
            for line in records_path.read_text().splitlines():
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        needs_setup = None
        if status_path.is_file():
            try:
                status = json.loads(status_path.read_text())
                if status.get("state") == "needs_setup":
                    needs_setup = status
            except (json.JSONDecodeError, OSError):
                pass

        if records:
            kinds = sorted({str(r.get("kind", "?")) for r in records})
            print(f"    live    {len(records)} record(s) in {kinds}")
        elif needs_setup:
            print(f"    live    needs_setup: missing {needs_setup.get('missing')}")
        else:
            p.error(f"no records and no needs_setup signal after {seconds:.0f}s of live run")

        failures = sorted({line.strip()[:160] for line in out.splitlines() if HTTP_FAILURE_RE.search(line)})
        for line in failures[:5]:
            p.warn(f"live-run source failure: {line}")


def validate(agent_dir: Path, args) -> Problems:
    p = Problems()
    manifest = check_manifest(agent_dir, p)
    check_files(agent_dir, manifest, p)
    if args.smoke and not p.errors:
        smoke(agent_dir, args.sdk, args.smoke_seconds, p)
    if args.live and not p.errors:
        live(agent_dir, manifest, args.sdk, args.live_seconds, p)
    return p


def discover(root: Path) -> list:
    return sorted(d for d in root.iterdir() if d.is_dir() and (d / "agent.json").is_file())


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate DispatchKit agent directories")
    parser.add_argument("dirs", nargs="*", type=Path, help="agent directories to check")
    parser.add_argument("--root", type=Path, help="check every agent directory under this root")
    parser.add_argument("--smoke", action="store_true", help="also boot each agent with LLM_DISABLED=1")
    parser.add_argument("--smoke-seconds", type=float, default=8.0)
    parser.add_argument("--live", action="store_true",
                        help="run each agent against its real data sources (LLM stubbed) and require a record or needs_setup signal")
    parser.add_argument("--live-seconds", type=float, default=120.0)
    parser.add_argument("--sdk", default=os.environ.get("DISPATCHKIT_SDK", ""),
                        help="path to the dispatchkit SDK if it is not pip-installed")
    parser.add_argument("--strict", action="store_true", help="treat warnings as failures")
    args = parser.parse_args()

    targets = [d for d in args.dirs if d.is_dir()]
    if args.root:
        targets += discover(args.root)
    targets = [d for d in dict.fromkeys(targets) if d.name != "_template"]
    if not targets:
        print("no agent directories found", file=sys.stderr)
        return 2

    failed = 0
    for agent_dir in targets:
        p = validate(agent_dir, args)
        if p.errors:
            failed += 1
            print(f"[FAIL] {agent_dir.name}")
        elif p.warnings:
            print(f"[WARN] {agent_dir.name}")
        else:
            print(f"[PASS] {agent_dir.name}")
        for e in p.errors:
            print(f"    ERROR   {e}")
        for w in p.warnings:
            print(f"    warning {w}")
        if args.strict and p.warnings:
            failed += 1

    print()
    print(f"{len(targets) - failed} of {len(targets)} agent(s) pass.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
