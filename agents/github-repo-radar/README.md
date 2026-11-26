# GitHub Repo Radar

Track the configured public GitHub repositories: announce new releases as they ship, keep a star/issue history, and write a periodic AI momentum report comparing activity across the watched repos. Works without credentials; add a GitHub token for higher API limits.

[![Add to Dispatcher](https://dispatcher.build/add-to-dispatcher.svg)](https://dispatcher.build/install?agent=github-repo-radar)

It works out of the box on its defaults.

## Run it locally

```bash
pip install ../../dispatchkit-sdk -r ../../requirements-agents.txt
LLM_DISABLED=1 python3 -u main.py
```
