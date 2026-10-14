"""Minimal MCP stdio server over the dispatcher API — no SDK dependency.

Speaks newline-delimited JSON-RPC 2.0: initialize, tools/list, tools/call.
Register with Claude Code as: claude mcp add dispatcher -- dispatcher mcp
"""
import json
import sys
from pathlib import Path

from .api import ApiError, Client

PROTOCOL_VERSION = "2024-11-05"

TOOLS = [
    {
        "name": "get_target",
        "description": "Which dispatcher instance this server acts on (production bills real agent-minutes on start). Call before creating or starting anything.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "authoring_guide",
        "description": "The worker authoring contract and build-test loop: SDK rules, files in/out, the ui/index.html bridge and visual standard. Read before writing worker code.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "upload_file",
        "description": "Upload a local file into the worker's data/uploads/ (what a user does through the interface). Use it to give a worker test fixtures.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent_id": {"type": "string"},
                "path": {"type": "string", "description": "Absolute path of the local file"},
                "name": {"type": "string", "description": "Name to store it under; defaults to the file's basename"},
            },
            "required": ["agent_id", "path"],
        },
    },
    {
        "name": "list_uploads",
        "description": "Files uploaded to the worker (data/uploads/)",
        "inputSchema": {"type": "object", "properties": {"agent_id": {"type": "string"}}, "required": ["agent_id"]},
    },
    {
        "name": "delete_upload",
        "description": "Remove one uploaded file from the worker",
        "inputSchema": {
            "type": "object",
            "properties": {"agent_id": {"type": "string"}, "name": {"type": "string"}},
            "required": ["agent_id", "name"],
        },
    },
    {
        "name": "list_outputs",
        "description": "Files the worker has published (data/out/), e.g. generated spreadsheets",
        "inputSchema": {"type": "object", "properties": {"agent_id": {"type": "string"}}, "required": ["agent_id"]},
    },
    {
        "name": "download_output",
        "description": "Save one published file to a local path so it can be inspected",
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent_id": {"type": "string"},
                "name": {"type": "string"},
                "dest": {"type": "string", "description": "Local directory or file path to save to"},
            },
            "required": ["agent_id", "name", "dest"],
        },
    },
    {
        "name": "send_command",
        "description": "Send a chat command to the worker's inbox (handled by on_command while it runs); read the reply with agent_chat",
        "inputSchema": {
            "type": "object",
            "properties": {"agent_id": {"type": "string"}, "text": {"type": "string"}},
            "required": ["agent_id", "text"],
        },
    },
    {
        "name": "agent_chat",
        "description": "Recent chat between the user and the worker, including the worker's replies to commands",
        "inputSchema": {
            "type": "object",
            "properties": {"agent_id": {"type": "string"}, "limit": {"type": "integer"}},
            "required": ["agent_id"],
        },
    },
    {
        "name": "list_agents",
        "description": "List the user's dispatcher.build agents with status",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "create_agent",
        "description": (
            "Create a new worker (blank, or from a catalog template_id). Returns the manifest with its id. "
            "New workers get an hourly interval schedule; use set_agent_schedule to change it. "
            "Typical flow: create_agent -> write_file (main.py etc.) -> set_agent_env (keys) -> "
            "set_agent_schedule -> start_agent -> agent_logs / agent_records."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "1-80 chars"},
                "template_id": {"type": "string", "description": "Catalog agent to copy (e.g. site-monitor); omit for a blank DispatchKit skeleton"},
                "base_id": {"type": "string", "description": "Place the worker in this base/department (see list_bases); defaults to the first base"},
                "goal": {"type": "string", "description": "One-paragraph statement of what the worker is for"},
                "env_vars": {"type": "object", "additionalProperties": {"type": "string"}, "description": "Initial env vars / secrets"},
                "cpu_limit": {"type": "number", "description": "vCPU, default 0.25, max 4"},
                "memory_limit_mb": {"type": "integer", "description": "Default 512, 128-30720"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "list_bases",
        "description": "List the user's bases/departments on the canvas (id, name, description, color)",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "create_base",
        "description": "Create a new base/department on the canvas; pass its id as base_id to create_agent",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "1-60 chars"},
                "description": {"type": "string", "description": "Up to 200 chars; defaults to '<name> department'"},
                "color": {"type": "string", "description": "#rrggbb hex"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "delete_agent",
        "description": "Stop and permanently delete a worker, its files, data and schedule",
        "inputSchema": {
            "type": "object",
            "properties": {"agent_id": {"type": "string"}},
            "required": ["agent_id"],
        },
    },
    {
        "name": "get_agent_manifest",
        "description": "Read a worker's agent.json (goal, commands, tick_interval, tags, resources; secret env values masked)",
        "inputSchema": {
            "type": "object",
            "properties": {"agent_id": {"type": "string"}},
            "required": ["agent_id"],
        },
    },
    {
        "name": "update_agent_manifest",
        "description": "Update the editable manifest fields: goal, commands, tick_interval (5-86400 seconds), tags, inputs (agent links; prefer link_agents/unlink_agents)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent_id": {"type": "string"},
                "goal": {"type": "string"},
                "commands": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "Chat commands the worker handles in on_command(), e.g. [{\"name\": \"status\", \"description\": \"...\"}]",
                },
                "tick_interval": {"type": "number"},
                "tags": {"type": "array", "items": {"type": "string"}},
                "inputs": {"type": "array", "items": {"type": "object"}, "description": "[{agent_id, as}] linked sources; replaces the whole list"},
            },
            "required": ["agent_id"],
        },
    },
    {
        "name": "link_agents",
        "description": (
            "Link a source worker to a target worker so files the source publishes (self.publish(name, content) -> data/out/) "
            "are copied into the target's data/in/<as>/ before every start or scheduled wake. Both workers must be yours. "
            "The target reads them with self.input_path(as, name) or any relative path under data/in/<as>/."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent_id": {"type": "string", "description": "The target (receiving) worker"},
                "source_agent_id": {"type": "string", "description": "The worker whose data/out/ is shared"},
                "as": {"type": "string", "description": "Folder name under data/in/, lowercase letters/digits/-/_ (e.g. harvester)"},
            },
            "required": ["agent_id", "source_agent_id", "as"],
        },
    },
    {
        "name": "unlink_agents",
        "description": "Remove a link by its folder name; the target's data/in/<as>/ is cleared on the next sync",
        "inputSchema": {
            "type": "object",
            "properties": {"agent_id": {"type": "string"}, "as": {"type": "string"}},
            "required": ["agent_id", "as"],
        },
    },
    {
        "name": "list_agent_inputs",
        "description": "Show a worker's links: linked sources, files that have landed in data/in/, what it publishes in data/out/, and the last sync report",
        "inputSchema": {
            "type": "object",
            "properties": {"agent_id": {"type": "string"}},
            "required": ["agent_id"],
        },
    },
    {
        "name": "sync_agent_inputs",
        "description": "Copy linked sources' published files into a worker now (also happens automatically before every start/wake)",
        "inputSchema": {
            "type": "object",
            "properties": {"agent_id": {"type": "string"}},
            "required": ["agent_id"],
        },
    },
    {
        "name": "get_agent_env",
        "description": "Read a worker's env vars (secret values masked) and container resources",
        "inputSchema": {
            "type": "object",
            "properties": {"agent_id": {"type": "string"}},
            "required": ["agent_id"],
        },
    },
    {
        "name": "set_agent_env",
        "description": (
            "Set env vars / secrets on a worker (API keys, BEDROCK_MODEL_ID, LLM_MAX_CALLS_PER_DAY, ...). "
            "Merges into existing vars by default. A running worker needs restart_agent to pick them up."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent_id": {"type": "string"},
                "env_vars": {"type": "object", "additionalProperties": {"type": "string"}},
                "merge": {"type": "boolean", "description": "false replaces the whole env map; default true"},
            },
            "required": ["agent_id", "env_vars"],
        },
    },
    {
        "name": "restart_agent",
        "description": "Restart a worker so code/env changes take effect. Pass recreate=true after changing requirements.txt",
        "inputSchema": {
            "type": "object",
            "properties": {"agent_id": {"type": "string"}, "recreate": {"type": "boolean"}},
            "required": ["agent_id"],
        },
    },
    {
        "name": "get_agent_schedule",
        "description": "Read a worker's run schedule",
        "inputSchema": {
            "type": "object",
            "properties": {"agent_id": {"type": "string"}},
            "required": ["agent_id"],
        },
    },
    {
        "name": "set_agent_schedule",
        "description": (
            "Set how a worker runs: 'interval' wakes it every every_minutes (5-10080) for one pass and stops "
            "(billed only while running; at_minute pins a time of day for daily+ intervals), "
            "'on_demand' never wakes it: start_agent (or its interface) runs it once and it exits, for input-output workers, "
            "'always_on' keeps the container resident (billed continuously), "
            "'local' marks it as run outside the platform (never woken, never billed)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent_id": {"type": "string"},
                "mode": {"type": "string", "enum": ["interval", "always_on", "on_demand", "local"]},
                "every_minutes": {"type": "integer"},
                "at_minute": {"type": "integer", "description": "Minutes after midnight UTC, 0-1439"},
            },
            "required": ["agent_id", "mode"],
        },
    },
    {
        "name": "list_files",
        "description": "List an agent's source files",
        "inputSchema": {
            "type": "object",
            "properties": {"agent_id": {"type": "string"}},
            "required": ["agent_id"],
        },
    },
    {
        "name": "read_file",
        "description": "Read one source file from an agent",
        "inputSchema": {
            "type": "object",
            "properties": {"agent_id": {"type": "string"}, "filename": {"type": "string"}},
            "required": ["agent_id", "filename"],
        },
    },
    {
        "name": "write_file",
        "description": "Write one source file to an agent (Python is syntax-checked server-side)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent_id": {"type": "string"},
                "filename": {"type": "string"},
                "code": {"type": "string"},
            },
            "required": ["agent_id", "filename", "code"],
        },
    },
    {
        "name": "start_agent",
        "description": "Start the agent's hosted container (billed while running)",
        "inputSchema": {
            "type": "object",
            "properties": {"agent_id": {"type": "string"}},
            "required": ["agent_id"],
        },
    },
    {
        "name": "stop_agent",
        "description": "Stop the agent's hosted container",
        "inputSchema": {
            "type": "object",
            "properties": {"agent_id": {"type": "string"}},
            "required": ["agent_id"],
        },
    },
    {
        "name": "agent_status",
        "description": "Get an agent's runtime status",
        "inputSchema": {
            "type": "object",
            "properties": {"agent_id": {"type": "string"}},
            "required": ["agent_id"],
        },
    },
    {
        "name": "agent_logs",
        "description": "Get recent log lines from a running agent",
        "inputSchema": {
            "type": "object",
            "properties": {"agent_id": {"type": "string"}, "tail": {"type": "integer"}},
            "required": ["agent_id"],
        },
    },
    {
        "name": "agent_records",
        "description": "Get the structured records an agent has produced",
        "inputSchema": {
            "type": "object",
            "properties": {"agent_id": {"type": "string"}, "limit": {"type": "integer"}},
            "required": ["agent_id"],
        },
    },
]


def call_tool(client, name, args):
    if name == "get_target":
        production = "dispatcher.build" in client.url
        return {
            "url": client.url,
            "environment": "production" if production else "development",
            "note": "start_agent bills real agent-minutes here" if production else "local instance; nothing here bills",
        }
    if name == "authoring_guide":
        return (Path(__file__).parent / "authoring_guide.md").read_text(encoding="utf-8")
    if name == "upload_file":
        return client.upload_file(args["agent_id"], args["path"], name=args.get("name"))
    if name == "list_uploads":
        return client.list_uploads(args["agent_id"])
    if name == "delete_upload":
        return client.delete_upload(args["agent_id"], args["name"])
    if name == "list_outputs":
        return client.list_outputs(args["agent_id"])
    if name == "download_output":
        return client.download_output(args["agent_id"], args["name"], args["dest"])
    if name == "send_command":
        return client.send_command(args["agent_id"], args["text"])
    if name == "agent_chat":
        return client.chat(args["agent_id"], limit=args.get("limit", 20))
    if name == "list_agents":
        return client.list_agents()
    if name == "create_agent":
        return client.create_agent(
            args["name"],
            template_id=args.get("template_id"),
            goal=args.get("goal"),
            env_vars=args.get("env_vars"),
            cpu_limit=args.get("cpu_limit"),
            memory_limit_mb=args.get("memory_limit_mb"),
            base_id=args.get("base_id"),
        )
    if name == "list_bases":
        return client.list_bases()
    if name == "create_base":
        return client.create_base(args["name"], description=args.get("description"), color=args.get("color"))
    if name == "delete_agent":
        return client.delete_agent(args["agent_id"])
    if name == "get_agent_manifest":
        return client.get_manifest(args["agent_id"])
    if name == "update_agent_manifest":
        fields = {k: args[k] for k in ("goal", "commands", "tick_interval", "tags", "inputs") if k in args}
        return client.update_manifest(args["agent_id"], fields)
    if name == "link_agents":
        return client.link_agents(args["agent_id"], args["source_agent_id"], args["as"])
    if name == "unlink_agents":
        return client.unlink_agents(args["agent_id"], args["as"])
    if name == "list_agent_inputs":
        return client.get_inputs(args["agent_id"])
    if name == "sync_agent_inputs":
        return client.sync_inputs(args["agent_id"])
    if name == "get_agent_env":
        return client.get_env(args["agent_id"])
    if name == "set_agent_env":
        return client.set_env(args["agent_id"], args["env_vars"], merge=args.get("merge", True))
    if name == "restart_agent":
        return client.restart(args["agent_id"], recreate=args.get("recreate", False))
    if name == "get_agent_schedule":
        return client.get_schedule(args["agent_id"])
    if name == "set_agent_schedule":
        return client.set_schedule(
            args["agent_id"], args["mode"],
            every_minutes=args.get("every_minutes"),
            at_minute=args.get("at_minute"),
        )
    if name == "list_files":
        return client.list_files(args["agent_id"])
    if name == "read_file":
        return client.read_file(args["agent_id"], args["filename"])
    if name == "write_file":
        return client.write_file(args["agent_id"], args["filename"], args["code"])
    if name == "start_agent":
        return client.start(args["agent_id"])
    if name == "stop_agent":
        return client.stop(args["agent_id"])
    if name == "agent_status":
        return client.status(args["agent_id"])
    if name == "agent_logs":
        return client.logs(args["agent_id"], tail=args.get("tail", 100))
    if name == "agent_records":
        return client.records(args["agent_id"], limit=args.get("limit", 50))
    raise ValueError(f"unknown tool {name}")


def serve():
    client = Client()
    out = sys.stdout

    def send(msg):
        out.write(json.dumps(msg) + "\n")
        out.flush()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        rid = req.get("id")
        method = req.get("method")

        if method == "initialize":
            send({"jsonrpc": "2.0", "id": rid, "result": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "dispatcher", "version": "0.4.0"},
            }})
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}})
        elif method == "tools/call":
            params = req.get("params") or {}
            try:
                result = call_tool(client, params.get("name"), params.get("arguments") or {})
                text = result if isinstance(result, str) else json.dumps(result, indent=2)
                send({"jsonrpc": "2.0", "id": rid, "result": {
                    "content": [{"type": "text", "text": text}],
                }})
            except (ApiError, ValueError, KeyError, OSError) as e:
                send({"jsonrpc": "2.0", "id": rid, "result": {
                    "content": [{"type": "text", "text": f"error: {e}"}],
                    "isError": True,
                }})
        elif method == "ping":
            send({"jsonrpc": "2.0", "id": rid, "result": {}})
        elif rid is not None:
            send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"method not found: {method}"}})
