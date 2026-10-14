"""Prompts and offline stubs for alert context and the pricing report."""


def context_prompt(label: str, url: str, old_price: float, new_price: float, pct: float) -> tuple:
    system = (
        "You are a pricing analyst for a small business watching a competitor. "
        "In 2-4 sentences: what this price move likely means (promotion, "
        "repositioning, cost pass-through, clearance), and what the reader "
        "should consider doing about their own pricing or quoting. "
        "Be concrete, no hedging boilerplate."
    )
    direction = "cut" if new_price < old_price else "raised"
    prompt = (
        f"Competitor product: {label} ({url})\n"
        f"Price {direction} from {old_price} to {new_price} ({pct:+.1f}%).\n\n"
        f"What does this move likely mean and how should the reader respond?"
    )
    return system, prompt


def stub_context() -> str:
    return "(AI context is available when the LLM is enabled.)"


def report_prompt(rows: list, window_hours: float) -> tuple:
    system = (
        "You are a pricing analyst. Turn the raw competitor price table into "
        "a report the reader can act on: the biggest movers of the window, "
        "what the moves suggest about each competitor's strategy, and where "
        "the reader has room to move. Answer in clean markdown with sections: "
        "Overview, Price table, Biggest movers, Suggested actions."
    )
    lines = []
    for r in rows:
        move = f" ({r['window_pct']:+.1f}% this window)" if r.get("window_pct") is not None else ""
        currency = f" {r['currency']}" if r.get("currency") else ""
        lines.append(f"- {r['label']}: {r['last']}{currency}{move} | {r['url']}")
    prompt = (
        f"Reporting window: the last {window_hours:g} hours.\n"
        f"Watched products ({len(rows)}):\n" + "\n".join(lines)
        + "\n\nWrite the pricing report."
    )
    return system, prompt


def stub_report(rows: list) -> str:
    lines = [f"# Pricing report ({len(rows)} products watched)", ""]
    for r in rows:
        move = f" ({r['window_pct']:+.1f}% this window)" if r.get("window_pct") is not None else ""
        lines.append(f"- **{r['label']}**: {r['last']}{move} — {r['url']}")
    lines.append("")
    lines.append("(LLM disabled — listing the price table only.)")
    return "\n".join(lines)
