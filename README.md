# Dispatcher Agents

An open directory of autonomous workers built on the DispatchKit agent contract.
Every agent here is a plain directory of Python you can read, run locally, fork,
or run on hosted compute at [dispatcher.build](https://dispatcher.build).

**Contributions welcome** — see [CONTRIBUTING.md](CONTRIBUTING.md).

## What an agent is

A directory with a manifest and an entrypoint:

```
my-agent/
  agent.json         # identity, tick cadence, env config, chat commands
  main.py            # subclasses dispatchkit.Agent
  requirements.txt   # extra deps only
  lib/               # your modules
  ui/index.html      # optional custom interface
```

The SDK owns the loop. You declare `@phase` methods (or override `observe / think / act`), emit
findings with `self.record(...)`, and answer chat in `on_command(...)`. There is
no cron and no `while True` — scheduling is phase timestamps in the agent's own
state. The full contract is in [docs/AGENT_CONTRACT.md](docs/AGENT_CONTRACT.md).

## Run one locally

```bash
pip install ./dispatchkit-sdk -r requirements-agents.txt
cd agents/hn-radar
LLM_DISABLED=1 python3 -u main.py       # free: no LLM calls, stubbed answers
```

`LLM_DISABLED=1` is the way to develop: every `ask_llm` call returns the stub
you passed, so you can iterate on the logic without spending anything.

## Run one on hosted compute

```bash
pip install ./cli
dispatcher login
dispatcher install dispatcher-build/dispatcher-agents/agents/hn-radar
```

Hosted runtime is metered per agent-minute. The source here is free forever;
you only pay if you want someone else's computer to keep it running.

## Layout

| Path | What |
| --- | --- |
| `agents/` | one directory per agent |
| `dispatchkit-sdk/` | the SDK agents import (`pip install ./dispatchkit-sdk`) |
| `cli/` | the `dispatcher` CLI and MCP server (`pip install ./cli`) |
| `validate_agents.py` | the contract checker CI runs on every PR |
| `docs/AGENT_CONTRACT.md` | how to build one |

## License

MIT — see [LICENSE](LICENSE).
