# Gmail Inbox Manager

Keeps your inbox clean and tells you what actually needs you. Every few minutes
it reads new mail, sorts each message into one of seven categories, applies a
`Dispatcher/<Category>` label, stars the ones that need action, and, if you let
it, sweeps the noise out of the inbox. Everything it does is a label change, so
every batch can be undone.

## What it never does

- Never deletes a message.
- Never sends or replies.
- Never marks anything read or unread.

## Autonomy levels

| Level | What happens |
| --- | --- |
| `observe` | Reports only. Labels nothing. Start here. |
| `organise` | Applies category labels and stars action items. The inbox is untouched. |
| `autopilot` | Also archives the categories you choose (default: Newsletter, Notification, Promotion). |

Change it from the Interface tab or by chatting `set autonomy autopilot`.

## Categories

Action needed, Waiting on them, FYI, Newsletter, Notification, Receipt,
Promotion. Urgent or VIP action items are flagged so they show up on the canvas
badge; set VIP senders (an address or a whole domain) in the Interface tab.
Rules like `rule add from:invoices@acme.com -> Receipt` always win over the
classifier.

## Setup

1. In console.cloud.google.com, enable the Gmail API, add yourself as a test
   user on the OAuth consent screen, and create an OAuth client of type
   Desktop app. While the consent screen is in Testing, Google expires refresh
   tokens after 7 days; publish the app to get a long-lived one.
2. Download `lib/gmail_auth.py` from the Code tab (or `dispatcher pull`) and
   run it on your own machine:
   `python lib/gmail_auth.py --client-id <id> --client-secret <secret>`
3. Paste `GMAIL_CLIENT_ID`, `GMAIL_CLIENT_SECRET` and `GMAIL_REFRESH_TOKEN` into
   the Config tab and start the worker in `observe`.
4. After a few triage records look right, move to `organise` or `autopilot`.

Settings changed in the Interface tab override the Config tab values; chat
`set autonomy default` to go back to the Config value.

## Chat

`status`, `triage now`, `settings`, `set autonomy <mode>`, `set archive <A,B>`,
`vip add|remove|list`, `rule add|remove|list`, `undo last`, `undo <batch>`.
