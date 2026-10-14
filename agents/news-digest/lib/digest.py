"""Digest prompt construction for the news digest agent."""


def build_prompt(items: list, topic: str) -> tuple:
    lines = []
    for i, item in enumerate(items, 1):
        lines.append(
            f"{i}. [{item['source']}] {item['title']}\n"
            f"   {item['summary']}\n"
            f"   link: {item['link']}"
        )

    system = (
        "You are an analyst writing an executive news digest. Group the items into "
        "3-6 themes, lead with a 2-3 sentence executive summary, and for each theme "
        "explain in plain language what happened and why it matters. Reference items "
        "as markdown links using their titles. End with a short 'Worth watching' "
        "list. Output clean markdown only — no preamble."
    )
    prompt = (
        f"Focus area: {topic}\n\n"
        f"News items collected since the last digest:\n\n" + "\n".join(lines)
    )
    return system, prompt


def stub_digest(items: list) -> str:
    lines = [f"- [{i['source']}] [{i['title']}]({i['link']})" for i in items[:15]]
    return "## Digest (LLM disabled — raw items)\n\n" + "\n".join(lines)
