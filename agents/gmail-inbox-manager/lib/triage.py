"""Pure triage logic: prompt building, response parsing, the heuristic stub
classifier used when the LLM is disabled or over budget, and the mapping from
a category plus autonomy level to Gmail label changes."""

import json
import re

CATEGORIES = [
    "Action needed",
    "Waiting on them",
    "FYI",
    "Newsletter",
    "Notification",
    "Receipt",
    "Promotion",
]
AUTONOMY_LEVELS = ("observe", "organise", "autopilot")

_PROMO_RE = re.compile(r"\b(\d+% off|sale|discount|offer|deal|coupon|promo|save \$?\d+|last chance)\b", re.I)
_NOREPLY_RE = re.compile(r"(no-?reply|donotreply|do-not-reply|notification|mailer-daemon|alerts?@|noreply)", re.I)
_RECEIPT_RE = re.compile(r"\b(receipt|invoice|order confirmation|your order|payment (received|confirmation)|statement|subscription renew)", re.I)
_ASK_RE = re.compile(r"(\?|\bplease\b|\bcan you\b|\bcould you\b|\bneed\b|\basap\b|\burgent\b|\bdeadline\b|\bapprove\b|\breview\b|action required|\bconfirm\b|\bsign\b)", re.I)
_URGENT_RE = re.compile(r"\b(urgent|asap|today|eod|end of day|deadline|immediately|overdue|final notice)\b", re.I)

SYSTEM_PROMPT = (
    "You triage a business inbox. Classify each message into exactly one category from: "
    "Action needed (the recipient must do something), Waiting on them (the recipient sent "
    "it and is waiting on the other party), FYI (worth reading, nothing to do), Newsletter, "
    "Notification (automated system mail), Receipt (payments, invoices, order confirmations), "
    "Promotion. Mark urgent=true only when a deadline or explicit urgency is present. "
    "Return only JSON."
)


def sender_domain(email: str) -> str:
    return email.rsplit("@", 1)[-1].lower() if "@" in email else ""


def is_vip(msg: dict, vips: list) -> bool:
    email = msg.get("from_email", "").lower()
    domain = sender_domain(email)
    for vip in vips:
        v = vip.strip().lower()
        if not v:
            continue
        if v == email or (v == domain) or (v.startswith("@") and v[1:] == domain):
            return True
    return False


def apply_rules(msg: dict, rules: list):
    subject = msg.get("subject", "").lower()
    sender = msg.get("from", "").lower()
    for rule in rules:
        pattern = str(rule.get("pattern", "")).lower()
        if not pattern or rule.get("category") not in CATEGORIES:
            continue
        haystack = sender if rule.get("match") == "from" else subject
        if pattern in haystack:
            return rule["category"]
    return None


def hints_for(msg: dict, me: str) -> list:
    headers = msg.get("headers", {})
    hints = []
    if "list-unsubscribe" in headers:
        hints.append("list-unsubscribe")
    if headers.get("precedence", "").lower() in ("bulk", "list", "junk"):
        hints.append("bulk")
    if headers.get("auto-submitted", "").lower() not in ("", "no"):
        hints.append("auto-submitted")
    if me and msg.get("from_email", "").lower() == me.lower():
        hints.append("from-me")
    if "SENT" in msg.get("label_ids", []):
        hints.append("sent")
    return hints


def stub_classify(messages: list, vips: list, rules: list, me: str) -> dict:
    results = []
    for i, msg in enumerate(messages):
        subject = msg.get("subject", "")
        text = f"{subject} {msg.get('snippet', '')}"
        hints = hints_for(msg, me)
        forced = apply_rules(msg, rules)
        urgent = bool(_URGENT_RE.search(text))
        if forced:
            category, reason = forced, "matched a rule"
        elif "from-me" in hints or "sent" in hints:
            category, reason = "Waiting on them", "you sent the last message"
        elif is_vip(msg, vips):
            category, reason = "Action needed", "VIP sender"
        elif "list-unsubscribe" in hints or "bulk" in hints:
            if _PROMO_RE.search(subject):
                category, reason = "Promotion", "bulk mail with an offer"
            else:
                category, reason = "Newsletter", "bulk mailing list"
        elif "auto-submitted" in hints or _NOREPLY_RE.search(msg.get("from", "")):
            category, reason = "Notification", "automated sender"
        elif _RECEIPT_RE.search(text):
            category, reason = "Receipt", "payment or order wording"
        elif _ASK_RE.search(text):
            category, reason = "Action needed", "contains a request"
        else:
            category, reason = "FYI", "no request found"
        results.append({"i": i, "category": category, "urgent": urgent and category == "Action needed", "reason": reason})
    return {"results": results, "summary_md": ""}


def build_prompt(messages: list, vips: list, rules: list, me: str) -> tuple:
    items = []
    for i, msg in enumerate(messages):
        items.append({
            "i": i,
            "from": msg.get("from", "")[:120],
            "subject": msg.get("subject", "")[:200],
            "snippet": msg.get("snippet", "")[:300],
            "hints": hints_for(msg, me),
        })
    rule_lines = [f"{r.get('match')}:{r.get('pattern')} -> {r.get('category')}" for r in rules] or ["(none)"]
    prompt = (
        f"My address: {me or 'unknown'}\n"
        f"VIP senders: {', '.join(vips) or '(none)'}\n"
        f"Rules (always obey): {'; '.join(rule_lines)}\n\n"
        f"Messages:\n{json.dumps(items, ensure_ascii=False)}\n\n"
        "Return exactly:\n"
        '{"results":[{"i":0,"category":"Action needed","urgent":false,"reason":"<12 words>"}],\n'
        ' "summary_md":"<3-6 markdown bullets: what arrived and what needs attention, most important first>"}'
    )
    return SYSTEM_PROMPT, prompt


def parse_response(data: dict, n: int) -> dict:
    by_index = {}
    for item in (data or {}).get("results", []) or []:
        try:
            i = int(item.get("i"))
        except (TypeError, ValueError, AttributeError):
            continue
        if 0 <= i < n:
            category = item.get("category") if item.get("category") in CATEGORIES else "FYI"
            by_index[i] = {
                "i": i,
                "category": category,
                "urgent": bool(item.get("urgent")) and category == "Action needed",
                "reason": str(item.get("reason", ""))[:160],
            }
    results = [by_index.get(i, {"i": i, "category": "FYI", "urgent": False, "reason": "unclassified"}) for i in range(n)]
    summary = data.get("summary_md") if isinstance(data, dict) else ""
    return {"results": results, "summary_md": str(summary or "")}


def plan_actions(category: str, urgent: bool, autonomy: str, archive_categories: set, label_ids: dict) -> dict:
    add, remove = [], []
    if autonomy == "observe":
        return {"add": add, "remove": remove}
    label = label_ids.get(category)
    if label:
        add.append(label)
    if category == "Action needed":
        add.append("STARRED")
    if autonomy == "autopilot" and category in archive_categories:
        remove.append("INBOX")
    return {"add": add, "remove": remove}


def summary_md(results: list, messages: list, autonomy: str, applied: dict) -> str:
    counts = {}
    for r in results:
        counts[r["category"]] = counts.get(r["category"], 0) + 1
    lines = [f"- **{len(messages)} new messages** triaged in `{autonomy}` mode"]
    for category in CATEGORIES:
        if counts.get(category):
            lines.append(f"- {category}: {counts[category]}")
    actions = [messages[r["i"]] for r in results if r["category"] == "Action needed"]
    for msg in actions[:5]:
        lines.append(f"- Action: {msg.get('subject', '')[:80]} from {msg.get('from_email', '')}")
    if applied.get("archived"):
        lines.append(f"- Archived {applied['archived']} low-value message(s) out of the inbox")
    if applied.get("starred"):
        lines.append(f"- Starred {applied['starred']} action item(s)")
    return "\n".join(lines)
