# Website Change Monitor

Watch the configured web pages for meaningful content changes and downtime. When a page changes, explain what changed and why it matters; when it goes down or recovers, say so immediately. Useful for competitor pages, pricing pages, docs, and status pages.

[![Add to Dispatcher](https://dispatcher.build/add-to-dispatcher.svg)](https://dispatcher.build/install?agent=site-monitor)

It works out of the box on its defaults.

## Run it locally

```bash
pip install ../../dispatchkit-sdk -r ../../requirements-agents.txt
LLM_DISABLED=1 python3 -u main.py
```
