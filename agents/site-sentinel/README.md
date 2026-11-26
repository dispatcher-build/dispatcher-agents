# Site Sentinel

Watch the websites this business depends on around the clock. Detect outages, broken pages and slow responses within minutes, confirm them before raising an alarm, explain in plain language what broke and what to do, warn before TLS certificates expire, and report uptime on a regular cadence.

[![Add to Dispatcher](https://dispatcher.build/add-to-dispatcher.svg)](https://dispatcher.build/install?agent=site-sentinel)

It works out of the box on its defaults.

## Run it locally

```bash
pip install ../../dispatchkit-sdk -r ../../requirements-agents.txt
LLM_DISABLED=1 python3 -u main.py
```
