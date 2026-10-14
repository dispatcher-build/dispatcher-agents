# Contributing an agent

Agents are directories. Adding one is a pull request that adds
`agents/<your-agent>/`, and CI checks it against the contract.

## The bar

1. **Runnable by anyone.** No personal login, no browser profile, no account
   only you have. Keyless public data, or a single API key the user pastes into
   their own config. Secrets default to empty strings in `agent.json` — never
   ship a key or an email address in the manifest.
2. **Uses the SDK loop.** `main.py` subclasses `dispatchkit.Agent` and implements
   `@phase` methods on a `dispatchkit.Agent`. No hand-rolled `while True`.
3. **Runs standalone.** `LLM_DISABLED=1 python3 -u main.py` from the agent
   directory boots and survives without any platform service.
4. **Says something useful.** Findings go through `self.record(...)` so they
   show up in the Records tab and the API. A payload key ending in `_md`
   renders as markdown.
5. **Answers chat.** `status` at minimum, declared in the manifest's `commands`
   so the UI shows a chip for it.
6. **Deps in the base image where possible.** Extra `requirements.txt` entries
   install on every cold start. `requests`, `bs4`, `pandas`, `feedparser`,
   `boto3`, `httpx`, `lxml`, `yfinance` and the `anthropic` SDK are already there.

## Before you open the PR

```bash
python3 validate_agents.py agents/<your-agent>                    # contract check
python3 validate_agents.py agents/<your-agent> --smoke \
    --sdk ./dispatchkit-sdk                                          # boots cleanly
```

Both run in CI. The smoke check boots your agent with `LLM_DISABLED=1` and
fails on a traceback or an early exit.

## What gets rejected

- Anything whose purpose is to abuse a third party: credential stuffing, scraping
  behind a login you do not own, spam, engagement farming.
- Agents that phone secrets or user data anywhere other than the service they are
  configured for.
- Agents that need a browser profile or a logged-in session to work at all.
- Vendored binaries, model weights, or committed `data/` directories.

## Review

Every PR is reviewed by a human before merge. Merged agents are published to the
hosted marketplace with your GitHub handle as the author. You keep copyright;
the MIT license in this repo covers redistribution.

Questions: open a discussion at https://github.com/dispatcher-build/dispatcher-agents/discussions.
