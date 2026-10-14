"""
LLM deep-dive on a screened or news-triggered candidate. One Bedrock call per
dive via dispatchkit.llm (LLM_DISABLED stub mode, daily budget, truncation built
in); the domain caps (dives/day, per-ticker cooldown) live in the agent.
"""

import json
import logging
import re

import requests

from dispatchkit import llm

logger = logging.getLogger(__name__)

MAX_ARTICLES = 2
MAX_ARTICLE_CHARS = 4000
MAX_TOKENS = 8000

ANALYST_SYSTEM = """You are a disciplined value-oriented equity analyst covering semiconductors and the data-center buildout supply chain (chip equipment, power/cooling, electrical construction, networking, DC REITs).

Given a candidate's valuation metrics, segment peer comparison, recent SEC filings, and news context, assess whether the stock is undervalued. Be skeptical: cheap-for-a-reason is not undervalued. Weigh cyclicality, customer concentration, and whether data-center capex exposure is priced in.

Respond with ONLY a JSON object, no other text:
{
  "verdict": "undervalued" | "fair" | "avoid",
  "conviction": <0-100>,
  "one_liner": "<one sentence summary>",
  "thesis_md": "<markdown analysis: valuation vs peers, quality of the business, why the market may be mispricing it>",
  "risks": ["..."],
  "catalysts": ["..."]
}"""


def _fetch_article(url: str) -> str:
    try:
        from bs4 import BeautifulSoup
        resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup(["script", "style", "nav", "header", "footer"]):
            tag.decompose()
        text = re.sub(r"\s+", " ", soup.get_text(" ")).strip()
        return text[:MAX_ARTICLE_CHARS]
    except Exception as e:
        logger.warning(f"Article fetch failed {url}: {e}")
        return ""


def _build_prompt(ticker: str, metrics: dict, peer_rows: list, filings: list, headlines: list, articles: list) -> str:
    peer_lines = [
        f"- {r['ticker']} ({r['name']}): composite {r['composite']}, "
        f"EV/EBITDA {r['metrics'].get('ev_ebitda')}, FCF yield {r['metrics'].get('fcf_yield')}, "
        f"PEG {r['metrics'].get('peg')}, GM trend {r['metrics'].get('gross_margin_trend')}pp"
        for r in peer_rows[:8]
    ]
    filing_lines = [f"- {f['date']} {f['form']}: {f['description']}" for f in filings]
    headline_lines = [f"- {h}" for h in headlines]
    article_blocks = [f"--- Article {i + 1} ---\n{a}" for i, a in enumerate(articles) if a]

    sections = [
        f"Candidate: {ticker} ({metrics.get('name', ticker)})",
        f"Metrics: {json.dumps(metrics, default=str)}",
        "Segment peers:\n" + "\n".join(peer_lines) if peer_lines else "",
        "Recent SEC filings:\n" + "\n".join(filing_lines) if filing_lines else "",
        "Triggering news:\n" + "\n".join(headline_lines) if headline_lines else "",
        "\n\n".join(article_blocks),
        "Assess whether this stock is undervalued and respond with the JSON object.",
    ]
    return "\n\n".join(s for s in sections if s)


def _normalize(parsed: dict) -> dict:
    try:
        conviction = max(0, min(100, int(parsed.get("conviction", 50))))
    except (TypeError, ValueError):
        conviction = 50
    return {
        "verdict": parsed.get("verdict") or "fair",
        "conviction": conviction,
        "one_liner": parsed.get("one_liner", ""),
        "thesis_md": parsed.get("thesis_md", ""),
        "risks": parsed.get("risks", []),
        "catalysts": parsed.get("catalysts", []),
    }


def deep_dive(ticker: str, metrics: dict, peer_rows: list, filings: list,
              headlines: list, article_urls: list) -> dict:
    stub = {
        "verdict": "fair",
        "conviction": 50,
        "one_liner": f"stub analysis for {ticker} (LLM disabled)",
        "thesis_md": f"## {ticker}\n\nLLM disabled — stub finding for testing.\n\n"
                     f"Metrics: `{json.dumps(metrics, default=str)}`",
        "risks": ["stub"],
        "catalysts": ["stub"],
    }
    if not llm.enabled():
        return stub

    articles = [_fetch_article(url) for url in article_urls[:MAX_ARTICLES]]
    prompt = _build_prompt(ticker, metrics, peer_rows, filings, headlines, articles)

    parsed = llm.ask_json(prompt, system=ANALYST_SYSTEM, max_tokens=MAX_TOKENS, stub=stub)
    return _normalize(parsed)
