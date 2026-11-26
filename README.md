# Dispatcher Agents

An open directory of autonomous workers built on the DispatchKit agent contract.
Every agent here is a plain directory of Python you can read, run locally, fork,
or run on hosted compute at [dispatcher.build](https://dispatcher.build).

**Contributions welcome** — see [CONTRIBUTING.md](CONTRIBUTING.md).

## Add one to Dispatcher in a click

Pick a team or a single agent. The button opens a preview showing exactly what you
are adding, then puts it in your workspace stopped. Nothing runs, and nothing is
billed, until you press start.

| Team | Agents | |
| --- | --- | --- |
| **Indie Dev Desk**<br>What the developer community is saying, which repos are moving, whether your vendors are up, and the day's AI news. | Hacker News Radar, GitHub Repo Radar, Vendor Status Watch, AI News Digest | [![Add to Dispatcher](https://dispatcher.build/add-to-dispatcher.svg)](https://dispatcher.build/install?pack=indie-dev-desk) |
| **Founder Watch**<br>Who is talking about you, what your competitors changed, and what moved in your industry. | Brand Mention Monitor, Competitor Price Watch, Website Change Monitor, AI News Digest | [![Add to Dispatcher](https://dispatcher.build/add-to-dispatcher.svg)](https://dispatcher.build/install?pack=founder-watch) |
| **Storefront Guard**<br>Know the moment your site or a vendor goes down, and see new customer reviews and mentions as they land. | Site Sentinel, Vendor Status Watch, Review Radar, Brand Mention Monitor | [![Add to Dispatcher](https://dispatcher.build/add-to-dispatcher.svg)](https://dispatcher.build/install?pack=storefront-guard) |

### Or one agent at a time

| Agent | What it does | |
| --- | --- | --- |
| [Claude Dev](agents/claude-dev) | A lead for parallel Claude Code sessions on one of your repositories, on your own computer with your Claude… | [![Add to Dispatcher](https://dispatcher.build/add-to-dispatcher.svg)](https://dispatcher.build/install?agent=claude-dev) |
| [Stock Research](agents/stock-research) | Continuously hunt for undervalued companies in the configured stock universe (default: technology and data-center… | [![Add to Dispatcher](https://dispatcher.build/add-to-dispatcher.svg)](https://dispatcher.build/install?agent=stock-research) |
| [AI News Digest](agents/news-digest) | Watch the configured news feeds around the clock, flag items matching alert keywords the moment they appear, and… | [![Add to Dispatcher](https://dispatcher.build/add-to-dispatcher.svg)](https://dispatcher.build/install?agent=news-digest) |
| [Website Change Monitor](agents/site-monitor) | Watch the configured web pages for meaningful content changes and downtime. When a page changes, explain what changed… | [![Add to Dispatcher](https://dispatcher.build/add-to-dispatcher.svg)](https://dispatcher.build/install?agent=site-monitor) |
| [GitHub Repo Radar](agents/github-repo-radar) | Track the configured public GitHub repositories: announce new releases as they ship, keep a star/issue history, and… | [![Add to Dispatcher](https://dispatcher.build/add-to-dispatcher.svg)](https://dispatcher.build/install?agent=github-repo-radar) |
| [Hacker News Radar](agents/hn-radar) | Watch Hacker News around the clock for the configured keywords — brands, products, competitors, topics — surface new… | [![Add to Dispatcher](https://dispatcher.build/add-to-dispatcher.svg)](https://dispatcher.build/install?agent=hn-radar) |
| [Site Sentinel](agents/site-sentinel) | Watch the websites this business depends on around the clock. Detect outages, broken pages and slow responses within… | [![Add to Dispatcher](https://dispatcher.build/add-to-dispatcher.svg)](https://dispatcher.build/install?agent=site-sentinel) |
| [Brand Mention Monitor](agents/brand-mention-monitor) | Watch Hacker News, Reddit and the news around the clock for the configured keywords — your brand, products and… | [![Add to Dispatcher](https://dispatcher.build/add-to-dispatcher.svg)](https://dispatcher.build/install?agent=brand-mention-monitor) |
| [Review Radar](agents/review-radar) | Watch the business's Google reviews around the clock, surface every new review the moment it lands with a drafted owner… | [![Add to Dispatcher](https://dispatcher.build/add-to-dispatcher.svg)](https://dispatcher.build/install?agent=review-radar) |
| [Competitor Price Watch](agents/competitor-price-watch) | Watch competitor product pages around the clock, read the live price from each one, keep the full price history, alert… | [![Add to Dispatcher](https://dispatcher.build/add-to-dispatcher.svg)](https://dispatcher.build/install?agent=competitor-price-watch) |
| [Outbound Marketing Email](agents/outbound-marketing-email) | Run outbound email campaigns for its owner. Holds several campaigns at once, each with its own audience, subject and… | [![Add to Dispatcher](https://dispatcher.build/add-to-dispatcher.svg)](https://dispatcher.build/install?agent=outbound-marketing-email) |
| [Vendor Status Watch](agents/vendor-status-watch) | Watch the public status pages of the SaaS vendors this business depends on, record new incidents the moment they open… | [![Add to Dispatcher](https://dispatcher.build/add-to-dispatcher.svg)](https://dispatcher.build/install?agent=vendor-status-watch) |
| [Gmail Inbox Manager](agents/gmail-inbox-manager) | Keep the Gmail inbox clean and surface what matters: triage new mail into categories, apply Dispatcher/<Category>… | [![Add to Dispatcher](https://dispatcher.build/add-to-dispatcher.svg)](https://dispatcher.build/install?agent=gmail-inbox-manager) |

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
| `packs/` | teams of agents that install together (`packs/<name>.json`) |
| `dispatchkit-sdk/` | the SDK agents import (`pip install ./dispatchkit-sdk`) |
| `cli/` | the `dispatcher` CLI and MCP server (`pip install ./cli`) |
| `validate_agents.py` | the contract checker CI runs on every PR |
| `docs/AGENT_CONTRACT.md` | how to build one |

## License

MIT — see [LICENSE](LICENSE).
