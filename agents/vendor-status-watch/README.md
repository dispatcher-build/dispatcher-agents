# Vendor Status Watch

Watches the public status pages of the SaaS vendors a business depends on and
turns them into one feed: incidents recorded the moment they open, recoveries
when they resolve, and a daily AI vendor-health brief.

Works with any Atlassian Statuspage-compatible status site (GitHub, Cloudflare,
Zoom, Twilio, Dropbox, OpenAI, Anthropic and thousands more) — the vendor list
is `Name|https://status.example.com` pairs in `VENDORS`, and more can be added
from chat with `watch Datadog|https://status.datadoghq.com`.

Useful with zero configuration: the default vendor list covers services almost
every business touches.

Records: `baseline` (first scan snapshot), `incident`, `recovered`, `report`.

Run standalone:

```bash
PYTHONPATH=../../dispatchkit-sdk LLM_DISABLED=1 python3 -u main.py
```
