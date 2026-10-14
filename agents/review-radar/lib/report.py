"""Prompts and offline stubs for reply drafts and the reputation report."""


def reply_prompt(place_name: str, review: dict) -> tuple:
    system = (
        "You draft owner replies to Google reviews for a small business. "
        "Sound like a real owner: warm, specific to what the reviewer said, "
        "never defensive, no corporate boilerplate. For negative reviews, "
        "acknowledge the specific complaint, say what happens next, and "
        "invite the reviewer to continue offline. 2-4 sentences. "
        "Return only the reply text."
    )
    prompt = (
        f"Business: {place_name}\n"
        f"Review ({review['stars']}/5 stars) by {review['author']}:\n"
        f"{review['text'][:1200] or '(no text, rating only)'}\n\n"
        f"Draft the owner's reply."
    )
    return system, prompt


def stub_reply() -> str:
    return "(Reply drafting is available when AI is enabled.)"


def report_prompt(stats: list, pending: list, window_hours: float) -> tuple:
    system = (
        "You are a reputation analyst for a small business. Turn the raw "
        "review activity into a report the owner can act on: how each "
        "location's rating is trending, what new reviewers praised or "
        "complained about (quote short phrases), and which reviews still "
        "need a reply. Answer in clean markdown with sections: Overview, "
        "By location, New reviews, Suggested actions."
    )
    lines = [
        f"- {s['name']}: {s['rating']}/5 across {s['review_count']} reviews"
        + (f" ({s['missed_reviews']} recent reviews arrived without visible text)" if s.get("missed_reviews") else "")
        for s in stats
    ]
    review_lines = [
        f"- [{p['place']}] {p['stars']}/5 by {p['author']} ({p['published'][:10]}): {p['text'][:240]}"
        for p in pending
    ]
    prompt = (
        f"Reporting window: the last {window_hours:g} hours.\n"
        f"Locations:\n" + "\n".join(lines)
        + f"\n\nNew reviews this window ({len(pending)}):\n"
        + ("\n".join(review_lines) if review_lines else "(none)")
        + "\n\nWrite the reputation report."
    )
    return system, prompt


def stub_report(stats: list, pending: list) -> str:
    lines = [f"# Reputation report ({len(pending)} new reviews)", ""]
    for s in stats:
        lines.append(f"- **{s['name']}**: {s['rating']}/5 across {s['review_count']} reviews")
    if pending:
        lines.append("")
        lines.append("New reviews:")
        for p in pending[:10]:
            lines.append(f"- [{p['place']}] {p['stars']}/5 by {p['author']}: {p['text'][:120]}")
    lines.append("")
    lines.append("(LLM disabled — listing raw activity only.)")
    return "\n".join(lines)
