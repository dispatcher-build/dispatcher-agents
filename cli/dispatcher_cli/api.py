import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

DEFAULT_URL = "https://dispatcher.build"
CONFIG_PATH = Path.home() / ".dispatcher" / "config.json"


class ApiError(RuntimeError):
    def __init__(self, status, message):
        super().__init__(f"{status}: {message}")
        self.status = status


def load_config():
    cfg = {}
    if CONFIG_PATH.is_file():
        try:
            cfg = json.loads(CONFIG_PATH.read_text())
        except json.JSONDecodeError:
            cfg = {}
    return {
        "url": os.environ.get("DISPATCHER_URL") or cfg.get("url") or DEFAULT_URL,
        "token": os.environ.get("DISPATCHER_TOKEN") or cfg.get("token") or "",
    }


def save_config(url, token):
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps({"url": url, "token": token}, indent=2) + "\n")
    CONFIG_PATH.chmod(0o600)


class Client:
    def __init__(self, url=None, token=None):
        cfg = load_config()
        self.url = (url or cfg["url"]).rstrip("/")
        self.token = token or cfg["token"]

    def request(self, method, path, body=None, params=None):
        if not self.token:
            raise ApiError(401, "no token configured — run `dispatcher login` first")
        url = f"{self.url}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "User-Agent": "dispatcher-cli",
        })
        try:
            with urllib.request.urlopen(req, timeout=60) as res:
                return json.loads(res.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            try:
                detail = json.loads(e.read().decode())
                msg = detail.get("detail") or detail.get("error") or str(detail)
            except Exception:
                msg = e.reason
            if e.code == 401:
                if "expired" in str(msg).lower():
                    msg = "token expired — mint a new one at https://dispatcher.build/tokens and run `dispatcher login`"
                else:
                    msg = (
                        f"{msg} — your token may be revoked or expired (tokens expire 90 days after minting); "
                        "mint a new one at https://dispatcher.build/tokens and run `dispatcher login`"
                    )
            raise ApiError(e.code, msg) from None
        except urllib.error.URLError as e:
            raise ApiError(0, f"cannot reach {self.url} ({e.reason})") from None

    def list_agents(self):
        return self.request("GET", "/api/v2/agents/")

    def list_bases(self):
        return self.request("GET", "/api/workspace").get("bases") or []

    def create_base(self, name, description=None, color=None):
        body = {"name": name}
        if description:
            body["description"] = description
        if color:
            body["color"] = color
        return self.request("POST", "/api/workspace/bases", body)

    def create_agent(self, name, template_id=None, goal=None, env_vars=None, cpu_limit=None, memory_limit_mb=None, base_id=None):
        body = {"name": name}
        if template_id:
            body["template_id"] = template_id
        if base_id:
            body["base_id"] = base_id
        if goal:
            body["goal"] = goal
        if env_vars:
            body["env_vars"] = env_vars
        if cpu_limit is not None:
            body["cpu_limit"] = cpu_limit
        if memory_limit_mb is not None:
            body["memory_limit_mb"] = memory_limit_mb
        return self.request("POST", "/api/v2/agents/create", body)

    def delete_agent(self, agent_id):
        return self.request("DELETE", f"/api/v2/agents/{agent_id}")

    def get_manifest(self, agent_id):
        return self.request("GET", f"/api/v2/agents/{agent_id}/manifest")

    def update_manifest(self, agent_id, fields):
        return self.request("PUT", f"/api/v2/agents/{agent_id}/manifest", fields)

    def get_inputs(self, agent_id):
        return self.request("GET", f"/api/v2/agents/{agent_id}/inputs")

    def sync_inputs(self, agent_id):
        return self.request("POST", f"/api/v2/agents/{agent_id}/inputs/sync", {})

    def link_agents(self, agent_id, source_agent_id, alias):
        current = [
            i for i in (self.get_manifest(agent_id).get("inputs") or [])
            if i.get("as") != alias and i.get("agent_id") != source_agent_id
        ]
        return self.update_manifest(agent_id, {"inputs": current + [{"agent_id": source_agent_id, "as": alias}]})

    def unlink_agents(self, agent_id, alias):
        current = [i for i in (self.get_manifest(agent_id).get("inputs") or []) if i.get("as") != alias]
        return self.update_manifest(agent_id, {"inputs": current})

    def get_env(self, agent_id):
        return self.request("GET", f"/api/v2/agents/{agent_id}/env")

    def set_env(self, agent_id, env_vars, merge=True):
        return self.request("PUT", f"/api/v2/agents/{agent_id}/env", {"env_vars": env_vars, "merge": merge})

    def restart(self, agent_id, recreate=False):
        return self.request("POST", f"/api/v2/agents/{agent_id}/restart", {"recreate": recreate})

    def get_schedule(self, agent_id):
        return self.request("GET", f"/api/agents/{agent_id}/schedule")

    def set_schedule(self, agent_id, mode, every_minutes=None, at_minute=None):
        body = {"mode": mode}
        if every_minutes is not None:
            body["every_minutes"] = every_minutes
        if at_minute is not None:
            body["at_minute"] = at_minute
        return self.request("PUT", f"/api/agents/{agent_id}/schedule", body)

    def list_files(self, agent_id):
        return self.request("GET", f"/api/v2/agents/{agent_id}/files")["files"]

    def read_file(self, agent_id, filename):
        return self.request("GET", f"/api/v2/agents/{agent_id}/code", params={"filename": filename})["code"]

    def write_file(self, agent_id, filename, code):
        return self.request("PUT", f"/api/v2/agents/{agent_id}/code", {"filename": filename, "code": code})

    def start(self, agent_id):
        return self.request("POST", f"/api/v2/agents/{agent_id}/start")

    def stop(self, agent_id):
        return self.request("POST", f"/api/v2/agents/{agent_id}/stop")

    def status(self, agent_id):
        return self.request("GET", f"/api/v2/agents/{agent_id}/status")

    def logs(self, agent_id, tail=100):
        return self.request("GET", f"/api/v2/agents/{agent_id}/logs", params={"tail": tail})

    def records(self, agent_id, limit=50):
        return self.request("GET", f"/api/v2/agents/{agent_id}/records", params={"limit": limit})

    def request_raw(self, method, path, data=None, params=None, content_type="application/octet-stream"):
        if not self.token:
            raise ApiError(401, "no token configured — run `dispatcher login` first")
        url = f"{self.url}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": f"Bearer {self.token}",
            "Content-Type": content_type,
            "User-Agent": "dispatcher-cli",
        })
        try:
            with urllib.request.urlopen(req, timeout=180) as res:
                return res.read()
        except urllib.error.HTTPError as e:
            try:
                detail = json.loads(e.read().decode())
                msg = detail.get("detail") or detail.get("error") or str(detail)
            except Exception:
                msg = e.reason
            raise ApiError(e.code, msg) from None
        except urllib.error.URLError as e:
            raise ApiError(0, f"cannot reach {self.url} ({e.reason})") from None

    def list_uploads(self, agent_id):
        return self.request("GET", f"/api/v2/agents/{agent_id}/uploads")["files"]

    def upload_file(self, agent_id, path, name=None):
        with open(path, "rb") as f:
            data = f.read()
        name = name or os.path.basename(path)
        raw = self.request_raw("POST", f"/api/v2/agents/{agent_id}/uploads", data=data, params={"name": name})
        return json.loads(raw.decode() or "{}")

    def delete_upload(self, agent_id, name):
        return self.request("DELETE", f"/api/v2/agents/{agent_id}/uploads/{urllib.parse.quote(name)}")

    def list_outputs(self, agent_id):
        return self.request("GET", f"/api/v2/agents/{agent_id}/outputs")["files"]

    def download_output(self, agent_id, name, dest):
        data = self.request_raw("GET", f"/api/v2/agents/{agent_id}/outputs/{urllib.parse.quote(name)}")
        dest = os.path.abspath(os.path.join(dest, name) if os.path.isdir(dest) else dest)
        with open(dest, "wb") as f:
            f.write(data)
        return {"saved_to": dest, "size": len(data)}

    def send_command(self, agent_id, text):
        return self.request("POST", f"/api/v2/agents/{agent_id}/command", {"text": text, "meta": {"via": "cli"}})

    def chat(self, agent_id, limit=20):
        return self.request("GET", f"/api/v2/agents/{agent_id}/chat", params={"limit": limit})
