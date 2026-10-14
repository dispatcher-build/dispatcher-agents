"""
Free, keyless market data: yfinance for prices/fundamentals, SEC EDGAR for
filings and fallback fundamentals. All price/valuation access lives here so
the data source can be swapped without touching the agent.
"""

import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

import requests

logger = logging.getLogger(__name__)

SEC_HEADERS = {"User-Agent": os.environ.get("SEC_USER_AGENT", "dispatcher-agent contact@atlas-research.io")}
SEC_THROTTLE_SECONDS = 0.5
YF_THROTTLE_SECONDS = 1.0

CIK_MAP_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
COMPANY_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"


def load_cik_map(cache_path: Path) -> dict:
    """Ticker -> CIK map from SEC, cached for a day."""
    if cache_path.exists() and time.time() - cache_path.stat().st_mtime < 24 * 3600:
        return json.loads(cache_path.read_text())

    try:
        resp = requests.get(CIK_MAP_URL, headers=SEC_HEADERS, timeout=30)
        resp.raise_for_status()
        raw = resp.json()
        cik_map = {row["ticker"].upper(): row["cik_str"] for row in raw.values()}
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(cik_map))
        return cik_map
    except Exception as e:
        logger.warning(f"CIK map fetch failed: {e}")
        if cache_path.exists():
            return json.loads(cache_path.read_text())
        return {}


def recent_filings(ticker: str, cik_map: dict, limit: int = 8) -> list:
    """Recent SEC filings for a ticker: [{form, date, description}]."""
    cik = cik_map.get(ticker.upper())
    if not cik:
        return []
    try:
        time.sleep(SEC_THROTTLE_SECONDS)
        resp = requests.get(SUBMISSIONS_URL.format(cik=cik), headers=SEC_HEADERS, timeout=30)
        resp.raise_for_status()
        recent = resp.json().get("filings", {}).get("recent", {})
        filings = []
        for form, date, desc in zip(
            recent.get("form", []),
            recent.get("filingDate", []),
            recent.get("primaryDocDescription", []),
        ):
            if form in ("10-K", "10-Q", "8-K", "20-F", "6-K"):
                filings.append({"form": form, "date": date, "description": desc or form})
            if len(filings) >= limit:
                break
        return filings
    except Exception as e:
        logger.warning(f"EDGAR submissions failed for {ticker}: {e}")
        return []


def _margin_trend(quarterly_financials) -> Optional[float]:
    """Gross-margin change in percentage points, recent half vs older half."""
    try:
        revenue = quarterly_financials.loc["Total Revenue"].dropna()
        gross = quarterly_financials.loc["Gross Profit"].dropna()
        margins = (gross / revenue).dropna()
        if len(margins) < 4:
            return None
        values = list(margins)[:8]
        half = len(values) // 2
        recent = sum(values[:half]) / half
        older = sum(values[half:]) / (len(values) - half)
        return round((recent - older) * 100, 2)
    except Exception:
        return None


def _yf_metrics(ticker: str) -> Optional[dict]:
    import yfinance as yf

    t = yf.Ticker(ticker)
    info = t.info or {}
    price = info.get("currentPrice") or info.get("regularMarketPrice")
    market_cap = info.get("marketCap")
    if not price or not market_cap:
        return None

    ev = info.get("enterpriseValue")
    ebitda = info.get("ebitda")
    fcf = info.get("freeCashflow")
    if fcf is None:
        try:
            cf = t.cashflow
            fcf = float(cf.loc["Free Cash Flow"].dropna().iloc[0])
        except Exception:
            fcf = None

    total_debt = info.get("totalDebt") or 0
    total_cash = info.get("totalCash") or 0
    net_debt_ebitda = round((total_debt - total_cash) / ebitda, 2) if ebitda else None

    trend = None
    try:
        trend = _margin_trend(t.quarterly_financials)
    except Exception:
        pass

    return {
        "name": info.get("shortName") or ticker,
        "price": price,
        "market_cap": market_cap,
        "ev_ebitda": info.get("enterpriseToEbitda") or (round(ev / ebitda, 2) if ev and ebitda else None),
        "fcf_yield": round(fcf / market_cap, 4) if fcf else None,
        "peg": info.get("trailingPegRatio"),
        "forward_pe": info.get("forwardPE"),
        "net_debt_ebitda": net_debt_ebitda,
        "gross_margin_trend": trend,
        "source": "yfinance",
    }


def _yahoo_chart_price(ticker: str) -> Optional[float]:
    try:
        resp = requests.get(
            YAHOO_CHART_URL.format(ticker=ticker),
            headers={"User-Agent": "Mozilla/5.0"},
            params={"range": "1d", "interval": "1d"},
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()["chart"]["result"][0]["meta"]["regularMarketPrice"]
    except Exception:
        return None


def _fact_values(facts: dict, tag: str, unit: str = "USD", n: int = 8) -> list:
    try:
        entries = facts["facts"]["us-gaap"][tag]["units"][unit]
        entries = [e for e in entries if e.get("form") in ("10-K", "10-Q") and e.get("val") is not None]
        entries.sort(key=lambda e: e.get("end", ""), reverse=True)
        return [e["val"] for e in entries[:n]]
    except Exception:
        return []


def _edgar_metrics(ticker: str, cik_map: dict) -> Optional[dict]:
    """Crude fallback when yfinance fails: price via Yahoo chart, facts via EDGAR."""
    cik = cik_map.get(ticker.upper())
    price = _yahoo_chart_price(ticker)
    if not cik or not price:
        return None
    try:
        time.sleep(SEC_THROTTLE_SECONDS)
        resp = requests.get(COMPANY_FACTS_URL.format(cik=cik), headers=SEC_HEADERS, timeout=30)
        resp.raise_for_status()
        facts = resp.json()

        shares = None
        try:
            dei = facts["facts"]["dei"]["EntityCommonStockSharesOutstanding"]["units"]["shares"]
            shares = sorted(dei, key=lambda e: e.get("end", ""), reverse=True)[0]["val"]
        except Exception:
            pass
        if not shares:
            return None

        market_cap = price * shares
        op_income = _fact_values(facts, "OperatingIncomeLoss", n=4)
        dep_amort = _fact_values(facts, "DepreciationDepletionAndAmortization", n=4)
        ebitda = (sum(op_income) + sum(dep_amort)) if op_income else None

        return {
            "name": facts.get("entityName") or ticker,
            "price": price,
            "market_cap": market_cap,
            "ev_ebitda": round(market_cap / ebitda, 2) if ebitda and ebitda > 0 else None,
            "fcf_yield": None,
            "peg": None,
            "forward_pe": None,
            "net_debt_ebitda": None,
            "gross_margin_trend": None,
            "source": "edgar",
        }
    except Exception as e:
        logger.warning(f"EDGAR fallback failed for {ticker}: {e}")
        return None


def refresh_universe(tickers: list, cik_map: dict) -> dict:
    """Fetch valuation metrics for every ticker. Returns {ticker: metrics}."""
    metrics = {}
    for ticker in tickers:
        result = None
        try:
            result = _yf_metrics(ticker)
        except Exception as e:
            logger.warning(f"yfinance failed for {ticker}: {e}")
        if result is None:
            result = _edgar_metrics(ticker, cik_map)
        if result:
            metrics[ticker] = result
        else:
            logger.warning(f"No data for {ticker}")
        time.sleep(YF_THROTTLE_SECONDS)
    return metrics
