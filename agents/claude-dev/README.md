# Claude Dev

A lead for parallel Claude Code sessions on one of your repositories, on your own computer with your Claude subscription. Start a session from its page and it becomes its own worker with its own git worktree, branch and app ports; ask the lead what's going on across all of them, or have it hand work out.

[![Add to Dispatcher](https://dispatcher.build/add-to-dispatcher.svg)](https://dispatcher.build/install?agent=claude-dev)

## Before it works

Paste `DEV_REPO` into the agent's config.

## Run it locally

```bash
pip install ../../dispatchkit-sdk -r ../../requirements-agents.txt
LLM_DISABLED=1 python3 -u main.py
```
