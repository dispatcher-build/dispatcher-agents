# DispatchKit Agents — the worker contract

Every agent is a self-contained directory running the DispatchKit SDK loop.
`_template/` is the canonical skeleton (blank agents are scaffolded from it);
`stock-research/` is the canonical full reference implementation.
This file is generated from the Dispatcher monorepo's agent README; edit it there.

## Directory layout

```
my-agent/
  agent.json         # manifest (identity, tick cadence, env defaults, chat config)
  main.py            # entrypoint — subclass dispatchkit.Agent
  requirements.txt   # extra deps ONLY (base image covers the common set)
  lib/               # your modules (on PYTHONPATH in the container)
  ui/index.html      # optional custom interface — rendered on the worker's Interface tab
  data/              # runtime state — never packaged/published
    state.json       #   persisted state (save_state/load_state)
    records.jsonl    #   structured output (self.record) — agent is the only writer
    inbox.jsonl      #   user commands — platform backend is the only writer
    llm_usage.json   #   daily LLM budget counter
    store.db         #   SQLite (self.store) — agent writes; platform reads read-only
    uploads/         #   files the user uploaded via the Interface
    out/             #   files you publish (self.publish) — linkable and downloadable
```

`ui/index.html` is a self-contained page (inline CSS/JS) that gets a
`window.dispatcher` bridge — `command`, `records`, `chat`, `query`, `status`, `uploads`, `upload`, `removeUpload`, `outputs`, `download` —
injected before it runs; see the Custom Agents doc on dispatcher.build. Without the file the
Interface tab is hidden. `data/store.db` is browsable read-only in the app's
Database drawer and via `GET /api/v2/agents/{id}/db/*`.

## Container contract

The directory is mounted at `/agent` and run as `python -u /agent/main.py`
(Docker bind-mount locally, per-agent EFS access point on Fargate).
`entrypoint.sh` pip-installs `requirements.txt` when its hash changes — keep it
lean; `dispatchkit`, `requests`, `bs4`, `pandas`, `anthropic[bedrock]`, `yfinance`,
`feedparser` are already in the image. The same code must also run with plain
`python3 main.py` from the agent dir (no platform services required).

## Lifecycle

Subclass `dispatchkit.Agent` and declare the work as phases:

```python
from dispatchkit import Agent, phase

class Radar(Agent):
    async def setup(self):            # once at start; read config from env vars
        self.keywords = os.environ.get("KEYWORDS", "").split(",")

    @phase(every_minutes=45)
    async def scan(self): ...          # the first due phase runs each tick

    @phase(every_hours=24)
    async def digest(self): ...

    @phase(when=lambda a: bool(a.state.get("pending")))
    async def flush(self): ...         # condition-gated; combine with every_*

    @phase(every_hours=lambda a: a.refresh_hours)
    async def refresh(self): ...       # interval read from env in setup()

    async def teardown(self): ...      # once at shutdown
```

Phases are checked in declaration order and the first due one runs, so put the
most urgent first. After a phase runs the SDK stamps `self.state["last_<name>"]`
and saves state, which is what makes the schedule survive restarts and
scheduled wakes. `self.state` is a plain dict persisted to `data/state.json`;
call `self.save_state()` after changing it outside a phase.

For agents whose sensing does not fit a schedule, override
`observe() -> obs`, `think(obs) -> decision | None` and `act(decision)`
directly; the loop is the same, the defaults simply dispatch phases.

The SDK owns the tick loop (`tick_interval` seconds from the manifest), a
control API on `:8080` (auto-increments locally), state persistence, the
records/inbox plumbing, and graceful shutdown. Never write your own
`while True` loop.

## Structured output — records

```python
self.record(kind, payload, title=..., score=..., tags=[...], dedupe_key=...)
```

Appends to `data/records.jsonl`; served by `GET /api/v2/agents/{id}/records`
and rendered as the Output feed on the worker's Overview. The platform only knows the envelope —
`payload` is yours. Convention: payload keys ending in `_md` render as
markdown in the UI. Use `dedupe_key` + `self.has_recent_record(key, hours)`
to avoid repeating work (e.g. one finding per ticker per week).

### Alerts — flagging something the user must see

```python
self.record("finding", payload, title="...", alert="info")   # any record can be flagged
self.alert("Quota exhausted, stopping", {"detail": ...})      # level="error" by default
self.alert("3 new leads", payload, level="info", kind="lead")
```

`alert="info"` or `"error"` marks a record as something the user should look at.
Flagged records are also indexed in `data/alerts.jsonl`, and the canvas shows a
count badge on the worker (red if any unseen alert is an error, blue otherwise)
until the user opens it. Most records should **not** be alerts — a chatty agent
that flags everything trains the user to ignore the badge. Flag the actionable
few: a result worth acting on today, or a failure the agent cannot recover from.

The SDK flags one thing on its own: when consecutive tick errors reach
`max_consecutive_errors` and the loop enters the error state, it writes a
`kind="error"` alert (deduped to once an hour) so the failure is visible on the
canvas rather than only in the container logs. See
the Notifications doc on dispatcher.build for the user-facing contract.

## Missing configuration — say so, don't idle silently

An agent that needs a customer credential must not run green and quietly do
nothing. Declare the block in `setup()`:

```python
if not self.api_key:
    self.needs_setup("GOOGLE_PLACES_API_KEY", "Get one at console.cloud.google.com → Places API")
else:
    self.clear_needs_setup()
```

This writes `data/status.json`, which the platform surfaces as an amber "Needs
configuration" panel on the worker's Overview — the missing var names, your hint,
and a button into the config editor — instead of the green "working" line. Pass a
list for several vars. The live audit
(`validate_agents.py --live`) accepts this in place of records, so a key-gated
agent still passes.

## Chat — every agent is conversational

The platform appends user messages to `data/inbox.jsonl`
(`POST /api/v2/agents/{id}/command`); the SDK drains it every ~2s (independent
of tick cadence) and calls `on_command(cmd)`. The reply is written as a
`kind="reply"` record and surfaced in the Chat tab.

- Default behavior (no override): the SDK answers from your goal + state +
  recent records via the LLM — zero code for a conversational agent.
- Override for structured commands, falling back for everything else:

```python
async def on_command(self, cmd):
    if cmd["text"].strip().lower() == "status":
        return self._status_text()
    return await super().on_command(cmd)
```

Declare quick actions in the manifest so the UI shows chips:

```json
"commands": [{"name": "status", "description": "Current queue and caps", "template": "status"}]
```

Set `"chat_enabled": false` to opt out entirely.

## Scheduling — around-the-clock phases

There is no cron. Keep a short `tick_interval` and let `@phase(every_hours=…)`
gate the work; the stamped `last_<name>` timestamps in `self.state` are the
schedule. `util.due(state, key, hours)` / `util.mark(state, key)` are the same
primitives if you need a sub-schedule inside a phase.

For daily caps, store a date-keyed counter in `self.state` and reset it when
the date changes (see stock-research `deep_dives_today`).

## LLM usage

```python
text = await self.ask_llm(prompt, system=..., stub="offline answer")
data = await self.ask_json(prompt, system=..., stub={...})   # first JSON object in the reply, {} if none
```

Guardrails are built in and env-tunable: `LLM_DISABLED=1` (stub mode, free
testing), `LLM_MAX_CALLS_PER_DAY` (default 50), `LLM_MAX_INPUT_CHARS`,
`BEDROCK_MODEL_ID`, `AWS_REGION`. Keep domain-level caps in your agent too
(e.g. stock-research's `MAX_DEEP_DIVES_PER_DAY`). Outside an Agent the same
calls are `dispatchkit.llm.ask` / `ask_json`.

## HTTP

```python
resp = await self.fetch(url, params={"q": term}, headers={...}, timeout=20)
resp.status, resp.text, resp.json()
```

`self.fetch` (and `dispatchkit.http.fetch` outside an Agent) applies the
public-host guard below, a timeout, and three retries with backoff on network
errors and 408/429/5xx (honouring `Retry-After`). Non-2xx raises
`http.HTTPError` unless `raise_for_status=False`. Use it for every
user-configured URL instead of calling `requests`/`urllib` yourself.

## Manifest essentials (`agent.json`)

`tick_interval`, `resources.{cpu_limit, memory_limit_mb}`, `env_vars`
(defaults — users override at install via the config UI; secret-looking keys
`*_KEY/TOKEN/SECRET/PASSWORD/CREDENTIAL` are masked in API responses and
stripped on publish), `chat_enabled`, `commands`, `goal`,
`coordination.event_bus_url: null` (there is no inter-agent event bus;
`self.emit()` only writes a log line and `on_event()` is never called). `goal`, `commands`,
`tick_interval` and `tags` are editable after install through
`PUT /api/v2/agents/{id}/manifest` (the worker view's assistant uses it);
`env_vars`/`resources` go through `PUT /{id}/env`; everything else is
read-only once the agent exists. Env names the platform owns (`AGENT_*`
runtime keys, `USER_ID`, `HOME`, `PATH`, and the `PYTHON*`, `LD_*`, `AWS_*`,
`DISPATCHKIT_*`, `ECS_*` prefixes) are rejected on write and dropped at start;
an agent that needs to know its own id reads `AGENT_ID` from the environment
the orchestrator sets.

## Outbound URLs from user config

Any URL that comes from config or chat (webhooks, pages to monitor, product
pages) goes through `dispatchkit.netsafe.assert_public_url` before a request is
made: only `http`/`https`, no credentials in the URL, and the host must resolve
to public unicast addresses. Loopback, RFC 1918, link-local (which includes the
ECS credential endpoint and the metadata service) and multicast are refused
with `netsafe.UnsafeUrl`, and the httpx hook `netsafe.httpx_guard` re-checks
every redirect hop. `notify` applies this to `NOTIFY_WEBHOOK_URL` and never
follows a webhook redirect. Set `DISPATCHKIT_ALLOW_PRIVATE_URLS=1` only for a
local run against a dev server on your own machine.

## Sharing files between agents — links

Agents never see each other's directories. The one sanctioned way to pass data
between two workers is a **link**: the source publishes files, the platform copies
them into the target before the target starts.

- **Publish**: `self.publish("recipients.csv", text_or_bytes)` writes atomically to
  `data/out/<name>`. Only regular files directly under `data/out/` are ever shared;
  names are `[A-Za-z0-9][A-Za-z0-9._-]{0,127}`, no subdirectories, no dotfiles.
- **Link**: the receiving agent lists the source in its manifest —
  `"inputs": [{"agent_id": "<source id>", "as": "harvester"}]` — via
  `PUT /manifest`, the MCP `link_agents` tool, or the chat assistant. Both agents
  must belong to the same user; at most 8 links; an agent cannot link itself.
- **Receive**: before every start, restart and scheduled wake (and on
  `POST /{id}/inputs/sync`), the platform copies each source's `data/out/` into the
  target's `data/in/<as>/`, replacing changed files, leaving unchanged ones alone
  (same content, same mtime — so a file-fingerprinting agent does not reload on
  every wake), and deleting files the source removed. Read them with
  `self.input_path("harvester", "recipients.csv")` or `self.inputs()`; relative
  config paths such as `LIST_PATH=in/harvester/recipients.csv` also resolve under
  `data/`.
- **Limits**: 64 MiB per file, 256 MiB per link, 64 files. Over-cap or badly named
  files are skipped and reported in `GET /{id}/inputs`, never partially copied.
- **Two-way** is just two links (A lists B, B lists A). Links are never transitive.

## Files from and to the user — uploads and downloads

The same `data/out/` a link reads from is also downloadable by the owner
(`GET /{id}/outputs/{name}`, `dispatcher.download(name)` in a `ui/index.html`),
so `self.publish("estimate.xlsx", bytes)` is how a worker hands a file back.
Files the user uploads through the Interface (`dispatcher.upload(file)`) land in
`data/uploads/`; read them from `Path(self.config.data_dir, "uploads")`. Uploads are capped at 32 MiB / 32 files and a fixed set
of document types; see the Custom Agents doc on dispatcher.build. Fingerprint what you have already
processed in `self.state` so a scheduled wake does not redo a plan it has seen.

A wake is one tick, so an agent that consumes an input should do the whole
read-then-act in a single `act()`; see `ses-blaster` for the load-then-send pattern.
