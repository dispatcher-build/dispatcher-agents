# Stock Research

Continuously hunt for undervalued companies in the configured stock universe (default: technology and data-center infrastructure; extend via EXTRA_TICKERS or edit universe.json). Screen fundamentals daily, watch news around the clock, and produce deep-dive theses with conviction scores for the best candidates.

[![Add to Dispatcher](https://dispatcher.build/add-to-dispatcher.svg)](https://dispatcher.build/install?agent=stock-research)

It works out of the box on its defaults.

## Run it locally

```bash
pip install ../../dispatchkit-sdk -r ../../requirements-agents.txt
LLM_DISABLED=1 python3 -u main.py
```
