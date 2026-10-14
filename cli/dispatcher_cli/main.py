import argparse
import getpass
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

from .api import ApiError, Client, load_config, save_config

AGENT_META = ".dispatcher.json"


def _client(args):
    return Client(url=getattr(args, "url", None))


def _agent_id(args):
    if args.agent:
        return args.agent
    meta = Path(args.dir or ".") / AGENT_META
    if meta.is_file():
        return json.loads(meta.read_text())["agent_id"]
    sys.exit("no agent id given and no .dispatcher.json in the target directory")


def cmd_login(args):
    cfg = load_config()
    url = args.url or cfg["url"]
    token = args.token or getpass.getpass("Personal access token (dsp_...): ").strip()
    client = Client(url=url, token=token)
    agents = client.list_agents()
    save_config(url, token)
    print(f"Logged in to {url} — {len(agents)} agent(s) visible.")


def cmd_list(args):
    agents = _client(args).list_agents()
    if not agents:
        print("No agents yet — `dispatcher create <name>` makes one.")
        return
    width = max(len(a["id"]) for a in agents)
    for a in agents:
        print(f"{a['id']:<{width}}  {a.get('status', '?'):<8}  {a.get('name', '')}")


def cmd_create(args):
    data = _client(args).create_agent(args.name, template_id=args.template, base_id=args.base)
    agent = data.get("agent") or data
    print(agent.get("id", json.dumps(data)))


def cmd_bases(args):
    bases = _client(args).list_bases()
    if not bases:
        print("No bases yet.")
        return
    width = max(len(b["id"]) for b in bases)
    for b in bases:
        print(f"{b['id']:<{width}}  {b.get('name', '')}")


def cmd_pull(args):
    client = _client(args)
    agent_id = args.agent
    dest = Path(args.dir or agent_id)
    dest.mkdir(parents=True, exist_ok=True)
    files = client.list_files(agent_id)
    for f in files:
        rel = f["path"]
        content = client.read_file(agent_id, rel)
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        print(f"pulled {rel}")
    (dest / AGENT_META).write_text(json.dumps({"agent_id": agent_id}, indent=2) + "\n")
    try:
        manifest = client.get_manifest(agent_id)
        manifest = manifest.get("manifest", manifest)
        schedule = client.get_schedule(agent_id)
        portable = {k: manifest.get(k) for k in (
            "name", "runtime", "entrypoint", "goal", "commands", "tick_interval", "tags", "resources", "env_vars", "chat_enabled",
        ) if manifest.get(k) is not None}
        portable["schedule"] = {k: schedule.get(k) for k in ("mode", "every_minutes", "at_minute") if schedule.get(k) is not None}
        (dest / "agent.json").write_text(json.dumps(portable, indent=2) + "\n")
        print("pulled agent.json (secret values are masked; `dispatcher install` skips them)")
    except ApiError as e:
        print(f"warning: could not write agent.json ({e})", file=sys.stderr)
    print(f"{len(files)} file(s) → {dest}/")


def _push_dir(client, agent_id, src, quiet=False):
    pushed = 0
    warnings = []
    for path in sorted(src.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(src).as_posix()
        parts = path.relative_to(src).parts
        if rel in (AGENT_META, "agent.json") or any(p.startswith(".") or p in ("__pycache__", "data", "logs") for p in parts):
            continue
        try:
            res = client.write_file(agent_id, rel, path.read_text())
        except UnicodeDecodeError:
            print(f"skip {rel} (binary)")
            continue
        warnings.extend(res.get("warnings") or [])
        pushed += 1
        if not quiet:
            print(f"pushed {rel}")
    for w in warnings:
        print(f"warning: {w}", file=sys.stderr)
    return pushed


def cmd_push(args):
    client = _client(args)
    src = Path(args.dir or ".")
    agent_id = _agent_id(args)
    pushed = _push_dir(client, agent_id, src)
    print(f"{pushed} file(s) → agent {agent_id}")


GITHUB_URL_RE = re.compile(
    r"^(?:https?://)?github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)"
    r"(?:/(?:tree|blob)/(?P<ref>[^/]+)(?:/(?P<path>.*))?)?/?$"
)
SHORTHAND_RE = re.compile(r"^(?:gh:)?(?P<owner>[^/]+)/(?P<repo>[^/]+)(?:/(?P<path>.+))?$")


def _parse_source(source):
    """Return (owner, repo, subpath, ref) for a GitHub source, or None if local."""
    if Path(source).is_dir():
        return None
    match = GITHUB_URL_RE.match(source)
    if match:
        d = match.groupdict()
        return d["owner"], d["repo"], (d.get("path") or "").strip("/"), d.get("ref") or ""
    match = SHORTHAND_RE.match(source)
    if match and not source.startswith((".", "/")):
        d = match.groupdict()
        return d["owner"], d["repo"], (d.get("path") or "").strip("/"), ""
    sys.exit(f"cannot read '{source}' as a local directory or a GitHub source")


def _fetch_github(owner, repo, subpath, ref, dest):
    """Download a repo tarball and return the path to the requested subdirectory."""
    refs = [ref] if ref else ["main", "master"]
    last = None
    for candidate in refs:
        url = f"https://codeload.github.com/{owner}/{repo}/tar.gz/{candidate}"
        try:
            with urllib.request.urlopen(url, timeout=60) as res:
                data = res.read()
            break
        except urllib.error.HTTPError as e:
            last = f"{url} ({e.code})"
        except urllib.error.URLError as e:
            sys.exit(f"cannot reach github ({e.reason})")
    else:
        sys.exit(f"could not download the repo — last tried {last}")

    archive = dest / "repo.tar.gz"
    archive.write_bytes(data)
    with tarfile.open(archive) as tar:
        members = [m for m in tar.getmembers() if not (m.name.startswith("/") or ".." in Path(m.name).parts)]
        try:
            tar.extractall(dest, members=members, filter="data")
        except TypeError:  # python < 3.12
            tar.extractall(dest, members=members)

    roots = [d for d in dest.iterdir() if d.is_dir()]
    if not roots:
        sys.exit("the downloaded archive was empty")
    agent_dir = roots[0] / subpath if subpath else roots[0]
    if not agent_dir.is_dir():
        sys.exit(f"'{subpath}' is not a directory in {owner}/{repo}")
    return agent_dir


def cmd_install(args):
    client = _client(args)
    with tempfile.TemporaryDirectory() as tmp:
        parsed = _parse_source(args.source)
        if parsed is None:
            agent_dir = Path(args.source)
            origin = str(agent_dir)
        else:
            owner, repo, subpath, ref = parsed
            ref = args.ref or ref
            print(f"downloading {owner}/{repo}{'/' + subpath if subpath else ''}…")
            agent_dir = _fetch_github(owner, repo, subpath, ref, Path(tmp))
            origin = f"github.com/{owner}/{repo}"

        manifest_path = agent_dir / "agent.json"
        if not manifest_path.is_file():
            sys.exit(f"no agent.json in {agent_dir.name}/ — that is not an agent directory")
        try:
            manifest = json.loads(manifest_path.read_text())
        except json.JSONDecodeError as e:
            sys.exit(f"agent.json is not valid JSON: {e}")
        if not (agent_dir / manifest.get("entrypoint", "main.py")).is_file():
            sys.exit(f"entrypoint '{manifest.get('entrypoint', 'main.py')}' is missing")

        name = args.name or manifest.get("name") or agent_dir.name
        print(f"installing '{name}' from {origin}")
        secret_re = re.compile(r"(?<!MAX_)(?<!NUM_)(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|WEBHOOK_URL|WEBHOOK)S?$", re.I)
        reserved = ("AGENT_ID", "USER_ID", "AUTOMATION_MODE", "AGENT_RUN_MODE", "AGENT_MAX_RUNTIME_MIN", "HOME", "PATH")
        env_vars = {
            k: str(v) for k, v in (manifest.get("env_vars") or {}).items()
            if k not in reserved and not k.startswith(("PYTHON", "LD_", "AWS_", "DISPATCHKIT_", "ECS_"))
            and not secret_re.search(k) and "\u2022" not in str(v)
        }
        resources = manifest.get("resources") or {}
        data = client.create_agent(
            name,
            goal=(manifest.get("goal") or None),
            env_vars=env_vars or None,
            cpu_limit=resources.get("cpu_limit"),
            memory_limit_mb=resources.get("memory_limit_mb"),
            base_id=getattr(args, "base", None),
        )
        agent = data.get("agent") or data
        agent_id = agent.get("id")
        if not agent_id:
            sys.exit(f"unexpected create response: {json.dumps(data)}")

        pushed = _push_dir(client, agent_id, agent_dir, quiet=True)

    print(f"{pushed} file(s) → agent {agent_id}")
    carried = {k: manifest[k] for k in ("commands", "tick_interval", "tags") if manifest.get(k)}
    if carried:
        client.update_manifest(agent_id, carried)
    declared = manifest.get("schedule") or {}
    if declared.get("mode") in ("interval", "always_on", "local", "on_demand"):
        client.set_schedule(
            agent_id, declared["mode"],
            every_minutes=declared.get("every_minutes"),
            at_minute=declared.get("at_minute"),
        )
        print(f"schedule: {declared['mode']}"
              + (f" every {declared.get('every_minutes')} min" if declared["mode"] == "interval" else ""))
    else:
        print("schedule: hourly (default; change it in the app or over MCP with set_agent_schedule)")
    needs = [k for k, v in (manifest.get("env_vars") or {}).items()
             if not str(v or "").strip() and re.search(r"(?<!MAX_)(?<!NUM_)(KEY|TOKEN|SECRET|PASSWORD)S?$", k, re.I)]
    if needs:
        print(f"config needed before it can run: {', '.join(needs)}")
    print(f"next: dispatcher start --agent {agent_id}")


def cmd_run(args):
    src = Path(args.dir or ".")
    if not (src / "main.py").is_file():
        sys.exit(f"no main.py in {src}/ — nothing to run")
    env = dict(os.environ)
    if not args.llm:
        env["LLM_DISABLED"] = "1"
    sdk = args.sdk or os.environ.get("DISPATCHKIT_SDK")
    if sdk:
        env["PYTHONPATH"] = f"{sdk}:{env.get('PYTHONPATH', '')}".rstrip(":")
    sys.exit(subprocess.call([sys.executable, "-u", "main.py"], cwd=src, env=env))


def cmd_start(args):
    _client(args).start(_agent_id(args))
    print("started")


def cmd_stop(args):
    _client(args).stop(_agent_id(args))
    print("stopped")


def cmd_status(args):
    print(json.dumps(_client(args).status(_agent_id(args)), indent=2))


def cmd_logs(args):
    data = _client(args).logs(_agent_id(args), tail=args.tail)
    print(data.get("logs", data) if isinstance(data, dict) else data)


def cmd_records(args):
    data = _client(args).records(_agent_id(args), limit=args.limit)
    records = data.get("records", data) if isinstance(data, dict) else data
    print(json.dumps(records, indent=2))


def cmd_upload(args):
    client = _client(args)
    saved = client.upload_file(_agent_id(args), args.path, name=args.name)
    print(f"uploaded {saved['file']['name']} ({saved['file']['size']} bytes)")


def cmd_outputs(args):
    client = _client(args)
    for f in client.list_outputs(_agent_id(args)):
        print(f"{f['size']:>10}  {f['modified_at']}  {f['name']}")


def cmd_download(args):
    client = _client(args)
    result = client.download_output(_agent_id(args), args.name, args.dest)
    print(f"saved {result['saved_to']} ({result['size']} bytes)")


def cmd_mcp(args):
    from .mcp_server import serve
    serve()


def main():
    parser = argparse.ArgumentParser(prog="dispatcher", description="Author, sync, and run dispatcher.build agents")
    parser.add_argument("--url", help="override the dispatcher URL")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("login", help="store your personal access token")
    p.add_argument("--token")
    p.set_defaults(fn=cmd_login)

    p = sub.add_parser("list", help="list your agents")
    p.set_defaults(fn=cmd_list)

    p = sub.add_parser("create", help="create a blank agent (or from a template)")
    p.add_argument("name")
    p.add_argument("--template")
    p.add_argument("--base", help="base/department id to place the worker in (see `dispatcher bases`)")
    p.set_defaults(fn=cmd_create)

    p = sub.add_parser("bases", help="list your bases/departments")
    p.set_defaults(fn=cmd_bases)

    p = sub.add_parser("pull", help="download an agent's files into a local directory")
    p.add_argument("agent")
    p.add_argument("dir", nargs="?")
    p.set_defaults(fn=cmd_pull)

    p = sub.add_parser("push", help="upload local edits back to the agent")
    p.add_argument("--agent")
    p.add_argument("dir", nargs="?")
    p.set_defaults(fn=cmd_push)

    p = sub.add_parser("install", help="install an agent from GitHub or a local directory")
    p.add_argument("source", help="owner/repo/path, a github URL, or a local directory")
    p.add_argument("--as", dest="name", help="name for the new agent (defaults to the manifest name)")
    p.add_argument("--ref", default="", help="branch, tag or commit (defaults to main, then master)")
    p.add_argument("--base", help="base/department id to place the worker in (see `dispatcher bases`)")
    p.set_defaults(fn=cmd_install)

    p = sub.add_parser("run", help="run the agent locally (LLM disabled unless --llm)")
    p.add_argument("dir", nargs="?")
    p.add_argument("--llm", action="store_true")
    p.add_argument("--sdk", help="path to the dispatchkit SDK if not pip-installed")
    p.set_defaults(fn=cmd_run)

    for name, fn in (("start", cmd_start), ("stop", cmd_stop), ("status", cmd_status)):
        p = sub.add_parser(name, help=f"{name} the hosted agent")
        p.add_argument("--agent")
        p.add_argument("dir", nargs="?")
        p.set_defaults(fn=fn)

    p = sub.add_parser("logs", help="tail the hosted agent's logs")
    p.add_argument("--agent")
    p.add_argument("dir", nargs="?")
    p.add_argument("--tail", type=int, default=100)
    p.set_defaults(fn=cmd_logs)

    p = sub.add_parser("records", help="show the agent's recent records")
    p.add_argument("--agent")
    p.add_argument("dir", nargs="?")
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(fn=cmd_records)

    p = sub.add_parser("upload", help="upload a local file into the agent's data/uploads/")
    p.add_argument("path")
    p.add_argument("--name")
    p.add_argument("--agent")
    p.add_argument("--dir")
    p.set_defaults(fn=cmd_upload)

    p = sub.add_parser("outputs", help="list the files the agent has published")
    p.add_argument("--agent")
    p.add_argument("dir", nargs="?")
    p.set_defaults(fn=cmd_outputs)

    p = sub.add_parser("download", help="save one published file locally")
    p.add_argument("name")
    p.add_argument("--dest", default=".")
    p.add_argument("--agent")
    p.add_argument("--dir")
    p.set_defaults(fn=cmd_download)

    p = sub.add_parser("mcp", help="serve the dispatcher MCP server over stdio")
    p.set_defaults(fn=cmd_mcp)

    args = parser.parse_args()
    try:
        args.fn(args)
    except ApiError as e:
        sys.exit(f"error: {e}")


if __name__ == "__main__":
    main()
