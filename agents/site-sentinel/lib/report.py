"""Prompts for incident explanations and the uptime report."""


def incident_prompt(url: str, check: dict, consecutive: int) -> tuple:
    system = (
        "You are a calm, plain-spoken operations engineer writing for a business "
        "owner, not a developer. Explain what the failure means for their "
        "business, the most likely causes in order of probability, and exactly "
        "what to do first. Three short sections: What happened, Likely cause, "
        "What to do now. No jargon, no speculation presented as fact."
    )
    dns = check.get("dns") or {}
    prompt = (
        f"Site: {url}\n"
        f"Failed checks in a row: {consecutive}\n"
        f"HTTP status: {check.get('status')} (0 means the request never completed)\n"
        f"Error: {check.get('error') or 'none'}\n"
        f"Response time: {check.get('latency_ms')} ms\n"
        f"DNS resolves: {dns.get('resolves', 'not checked')}"
        f"{' -> ' + ', '.join(dns.get('ips', [])) if dns.get('ips') else ''}\n\n"
        "Write the incident note."
    )
    return system, prompt


def stub_incident(url: str, check: dict) -> str:
    return (
        f"# {url} is not responding correctly\n\n"
        f"- Status: {check.get('status')}\n"
        f"- Error: {check.get('error') or 'unknown'}\n"
        f"- Response time: {check.get('latency_ms')} ms\n\n"
        "(LLM disabled — raw check output only.)"
    )


def report_prompt(stats: list, hours: float) -> tuple:
    system = (
        "You are an operations analyst writing a short uptime report for a "
        "business owner. Lead with whether anything needs their attention. Be "
        "concrete about numbers. Markdown, sections: Summary, Per site, "
        "Anything to act on. Keep it under 300 words."
    )
    lines = []
    for s in stats:
        lines.append(
            f"- {s['url']}: {s['uptime_pct']}% uptime over {s['checks']} checks, "
            f"{s['failures']} failed, median {s['median_ms']} ms, "
            f"{s['incidents']} incident(s), "
            f"cert {s.get('cert_days', 'n/a')} days from expiry"
        )
    prompt = (
        f"Reporting window: last {hours:.0f} hours.\n"
        f"Sites monitored:\n" + "\n".join(lines) + "\n\nWrite the uptime report."
    )
    return system, prompt


def stub_report(stats: list, hours: float) -> str:
    lines = [f"# Uptime report ({hours:.0f}h)", ""]
    for s in stats:
        lines.append(
            f"- **{s['url']}** — {s['uptime_pct']}% uptime, {s['failures']}/{s['checks']} failed, "
            f"median {s['median_ms']} ms"
        )
    lines += ["", "(LLM disabled — figures only.)"]
    return "\n".join(lines)
