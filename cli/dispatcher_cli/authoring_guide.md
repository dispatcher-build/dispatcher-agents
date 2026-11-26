# Building a Dispatcher worker over MCP

Read this once per session before writing worker code. It is the same contract the
in-app assistant follows.

## Before anything: know your target

Call `get_target`. `https://dispatcher.build` is production: `start_agent` there bills
real agent-minutes and the worker is visible to the account owner. A `localhost` URL is
a development instance. Say which one you are on before creating or starting anything.

## The loop

1. `list_bases`, then `create_agent` (name, goal, `base_id`). It starts from the blank
   template: a skeleton `main.py` that does nothing useful. Never leave it in place.
2. `write_file` the complete `main.py`, any `lib/*.py`, `requirements.txt`, `README.md`
   (rendered as Instructions on the Overview) and `ui/index.html` when the worker
   takes input or files from the user. Whole files, never partial.
3. `update_agent_manifest` with a one-sentence `goal`, the chat `commands` the worker
   handles, a `tick_interval` (15 for interface-driven workers) and `tags`.
4. `set_agent_env` for config and keys. A worker that needs a key the user has not
   supplied must call `self.needs_setup("VAR", hint)` rather than idling.
5. `set_agent_schedule`: `interval` for periodic jobs (loosest cadence that fits),
   `on_demand` for an input-output worker the user triggers from its interface (it
   runs once per use and costs nothing in between), `always_on` only when it must
   react within seconds around the clock.
6. Test: `upload_file` fixtures if it takes files, `start_agent`, `send_command`,
   then `agent_logs`, `agent_records`, `agent_chat`, `list_outputs` and
   `download_output` to check the real result. Fix and `restart_agent` (with
   `recreate: true` after a `requirements.txt` change). `stop_agent` when done.
7. Report the worker id, what it does, what the user must supply, and what was verified.

## Code contract

- Subclass `dispatchkit.Agent`; never write your own loop. `from dispatchkit import Agent, phase`.
- Work is `@phase(every_hours=H | every_minutes=M | when=lambda a: ...)` async methods.
  The first due phase runs each tick and the SDK stamps `state["last_<name>"]`.
- `self.state` is a persisted dict; call `self.save_state()` after changing it outside a phase.
- Output is `self.record(kind, payload, title=..., tags=[...], dedupe_key=...)`. Records are
  what the Overview shows. Put readable text in a payload key ending `_md`, or in `text`/`summary`.
- `self.alert(title, payload, level="info")` badges the worker on the canvas; use `level="error"` only when
  something is broken. Always pass `level`: leaving it out means error and shows a red badge. Use sparingly.
- Chat: `async def on_command(self, command)` returns a str or dict; fall back to
  `await super().on_command(command)`. `command["text"]` is the message.
- LLM: `await self.ask_llm(prompt, system=..., stub=...)` and `await self.ask_json(...)`
  are text-only and budgeted (`LLM_MAX_CALLS_PER_DAY`, `LLM_DISABLED=1` returns the stub).
  For PDFs or images call Bedrock directly with `anthropic.AnthropicBedrock` content blocks
  (`{"type": "document", "source": {"type": "base64", "media_type": "application/pdf", ...}}`)
  and keep the guardrails: check `dispatchkit.llm.enabled()`, call `llm._check_budget(data_dir)`
  before and `llm._record_call` / `llm._log_usage` after.
- HTTP: `await self.fetch(url, ...)` (retries, public-host guard). Storage: `self.store` (SQLite).
- Base image has `requests`, `bs4`, `pandas`, `anthropic[bedrock]`, `httpx`, `lxml`, `feedparser`,
  `yfinance`. Anything else goes in `requirements.txt`; prefer pure-Python packages (the image is Alpine).
- No explanatory comments in code. Config comes from `os.environ`.

## Files in and out

- The user uploads through the interface into `data/uploads/` (pdf, csv, tsv, txt, json, xlsx,
  xls, docx, png, jpg; 32 MB each, 32 files). Read `Path(self.config.data_dir, "uploads")`.
- Hand files back with `self.publish(name, bytes)`; they land in `data/out/` and are
  downloadable. Fingerprint processed inputs in `self.state` so a wake does not redo them.
- Emit a `progress` record per step and one result record when done, so the interface can poll.

## Interface contract (`ui/index.html`)

One self-contained page, inline CSS and JS, no external scripts or fonts. It is the main
panel of the worker's Overview, so keep it to what the user acts on. `window.dispatcher` is
injected, every call returns a Promise and rejects with the server's message:

`command(text)`, `run(text?)`, `records({kind, limit})`, `chat({limit})`, `query(sql)`, `status()`,
`uploads()`, `upload(file, name?)`, `readUpload(name)`, `removeUpload(name)`, `outputs()`, `download(name)`,
`dictate({lang?})` (resolves `{text}` when the speaker stops; the app's page runs the browser's
speech recognition because the sandboxed frame cannot use the microphone), `dictateStop()`,
`onDictation(cb)` (`cb(text, final)` with the running transcript), `preview(url)` (shows an
app running on the user's own computer, a `localhost` address only, in a panel above the
page; `preview('')` hides it).

The sandbox has no `localStorage`. A page keeps its own state by uploading JSON
(`upload(new Blob([json]), 'inputs.json')`) and reading it back with `readUpload`; the worker
reads the same file from `data/uploads/`. Lists and tables come from `query(sql)` against the
worker's SQLite store, which the platform can read even while the worker is stopped.

The frame grows to the page's content height and the Overview scrolls as one document:
no fixed page heights, no `100vh` sections, no outer scrollbar. Drop zones must be
block-level (`label { display:block }`), every user-supplied string is HTML-escaped,
and errors are translated into plain language for a non-technical user.

Render current state on load (uploads, outputs, latest result), poll every few seconds
while a run is in flight and show errors inline. Drive the primary action with
`dispatcher.run('command text')`: it queues the command and starts one run, so the user
never starts the worker by hand. A run-once worker drains its inbox before its tick, so
the command reaches `on_command`, and the container exits when the job is done. Look: the app injects its theme into the frame as CSS variables on `<html>`,
switching live between light and dark: `--bg`, `--panel`, `--inset`, `--border`, `--border-strong`,
`--text`, `--dim`, `--faint`, `--link`, `--ok`, `--warn`, `--err`, plus a base `body` style
(system-ui 14px, page background, page text). Use those variables for every neutral colour and
never hardcode a grey, black or white; pick one accent colour of your own. Keep the page
almost empty: usually the latest result, the one input the worker needs and one primary
button. No page title or header (the app already shows the worker's name and goal), no tabs,
settings, stats, charts, help text or history list (the app lists past results below the
page), no outer frame; a single column of plain sections. Add more only when the user asks.
Real copy, no emojis.

## Definition of done

The worker produced a real result from real input on this instance, you read that result
(record, log line, downloaded file), and you told the user exactly what was and was not verified.
