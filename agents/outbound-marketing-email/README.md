# Outbound Marketing Email

Run outbound email campaigns for its owner. Holds several campaigns at once, each with its own audience, subject and body, and sends only the ones armed from the worker's interface. Pulls recipients from a database query, a file, or addresses pasted in; validates every address; paces sending within a daily cap and a rate so a large list goes out over days rather than one reputation-burning burst; never emails the same person twice for a campaign; and honours a suppression list. Sends through Resend or Amazon SES using the owner's own account.

[![Add to Dispatcher](https://dispatcher.build/add-to-dispatcher.svg)](https://dispatcher.build/install?agent=outbound-marketing-email)

## Before it works

Paste `RESEND_API_KEY`, `FROM_EMAIL` into the agent's config.

## Run it locally

```bash
pip install ../../dispatchkit-sdk -r ../../requirements-agents.txt
LLM_DISABLED=1 python3 -u main.py
```
