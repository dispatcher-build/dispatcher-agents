"""Digest prompt for the HN discussion brief."""


def build_prompt(items: list, keywords: list) -> tuple:
    system = (
        "You are a sharp community-intelligence analyst. You turn raw Hacker News "
        "mentions into a brief the reader can act on: what the community is saying, "
        "how sentiment is running, which threads matter, and what deserves a reply "
        "or a closer look. Be concrete and quote short representative phrases. "
        "Answer in clean markdown with sections: Overview, Themes, Notable threads "
        "(with links), Sentiment, Suggested actions."
    )
    lines = []
    for i in items:
        lines.append(
            f"- [{i['type']}] {i['title']} | keyword: {i['keyword']} | {i['points']} pts, "
            f"{i['num_comments']} comments | by {i['author']} | {i['hn_link']}\n"
            f"  excerpt: {i['excerpt'][:280]}"
        )
    prompt = (
        f"Keywords being tracked: {', '.join(keywords)}\n"
        f"New Hacker News mentions since the last brief ({len(items)} items):\n\n"
        + "\n".join(lines)
        + "\n\nWrite the discussion brief."
    )
    return system, prompt


def stub_digest(items: list) -> str:
    top = sorted(items, key=lambda i: i["points"], reverse=True)[:5]
    lines = [f"# HN discussion brief ({len(items)} mentions)", ""]
    for i in top:
        lines.append(f"- **{i['title']}** — {i['points']} pts, {i['num_comments']} comments — {i['hn_link']}")
    lines.append("")
    lines.append("(LLM disabled — listing top threads only.)")
    return "\n".join(lines)
