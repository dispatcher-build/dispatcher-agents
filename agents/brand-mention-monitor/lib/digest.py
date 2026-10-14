"""Digest prompt for the brand reputation brief."""


def build_prompt(items: list, keywords: list) -> tuple:
    system = (
        "You are a sharp brand-intelligence analyst. You turn raw mentions from "
        "Hacker News, Reddit and the news into a brief the reader can act on: "
        "what people are saying about each tracked keyword, how sentiment is "
        "running, where the conversation is happening, and what deserves a reply, "
        "a fix, or a closer look. Be concrete and quote short representative "
        "phrases. Answer in clean markdown with sections: Overview, By keyword "
        "(what's being said, sentiment), Notable threads & articles (with links), "
        "Suggested actions."
    )
    lines = []
    for i in items:
        engagement = f"{i['score']} pts, {i['num_comments']} comments" if i["source"] != "news" else "article"
        lines.append(
            f"- [{i['source']}/{i['where']}] {i['title']} | keyword: {i['keyword']} | "
            f"{engagement} | {i['link']}\n"
            f"  excerpt: {i['excerpt'][:280]}"
        )
    prompt = (
        f"Keywords being tracked: {', '.join(keywords)}\n"
        f"New mentions since the last brief ({len(items)} items):\n\n"
        + "\n".join(lines)
        + "\n\nWrite the reputation brief."
    )
    return system, prompt


def stub_digest(items: list) -> str:
    by_source: dict = {}
    for i in items:
        by_source.setdefault(i["source"], []).append(i)
    top = sorted(items, key=lambda i: i["score"], reverse=True)[:5]
    lines = [f"# Brand reputation brief ({len(items)} mentions)", ""]
    lines.append("Mentions by source: " + ", ".join(f"{s}: {len(v)}" for s, v in sorted(by_source.items())))
    lines.append("")
    for i in top:
        lines.append(f"- **{i['title']}** — {i['where']}, {i['score']} pts — {i['link']}")
    lines.append("")
    lines.append("(LLM disabled — listing top mentions only.)")
    return "\n".join(lines)
