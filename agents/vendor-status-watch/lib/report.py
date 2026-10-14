"""Prompt and offline stub for the daily vendor-health brief."""

import json


def build_prompt(vendors: dict, events: list) -> tuple:
    system = (
        "You are an operations analyst writing a daily vendor-health brief for a "
        "small business that depends on these SaaS vendors. Be concrete and calm: "
        "lead with anything currently degraded, then what broke and recovered since "
        "the last brief, then a one-line all-clear for the rest. Markdown, short."
    )
    prompt = (
        "Current vendor health:\n"
        + json.dumps({n: {"indicator": v.get("indicator"), "description": v.get("description")} for n, v in vendors.items()}, indent=2)
        + "\n\nEvents since the last brief (incidents and recoveries):\n"
        + json.dumps(events[-40:], indent=2)
        + "\n\nWrite the brief."
    )
    return system, prompt


def stub_report(vendors: dict, events: list) -> str:
    lines = ["# Vendor health brief", ""]
    degraded = {n: v for n, v in vendors.items() if v.get("indicator") not in ("none", "unknown")}
    if degraded:
        lines.append("## Degraded right now")
        for name, v in degraded.items():
            lines.append(f"- **{name}** — {v.get('description') or v.get('indicator')}")
        lines.append("")
    if events:
        lines.append(f"## Events since last brief ({len(events)})")
        for e in events[-20:]:
            lines.append(f"- {e.get('vendor')}: {e.get('kind')} — {e.get('title')}")
        lines.append("")
    healthy = [n for n, v in vendors.items() if v.get("indicator") == "none"]
    if healthy:
        lines.append(f"All other vendors operational: {', '.join(healthy)}.")
    return "\n".join(lines)
