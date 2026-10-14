"""
Quantitative valuation screen. Pure functions, no I/O.

Composite score = mean of within-segment percentile ranks across the metrics
that are present for a ticker (missing metrics don't count against it).
"""

from typing import Optional

METRIC_DIRECTIONS = {
    "ev_ebitda": -1,
    "fcf_yield": 1,
    "peg": -1,
    "net_debt_ebitda": -1,
    "gross_margin_trend": 1,
}

PASS_THRESHOLD = 0.65
MARGIN_DETERIORATION_FLOOR = -2.0


def _percentile_ranks(values: dict, direction: int) -> dict:
    """{ticker: value} -> {ticker: percentile 0..1}, higher = more attractive."""
    items = [(t, v) for t, v in values.items() if v is not None]
    if len(items) < 2:
        return {t: 0.5 for t, _ in items}
    items.sort(key=lambda kv: kv[1] * direction)
    n = len(items)
    return {t: i / (n - 1) for i, (t, _) in enumerate(items)}


def rank(metrics_by_ticker: dict, segments: dict, pass_threshold: float = PASS_THRESHOLD) -> list:
    """Rank the universe. Returns rows sorted by composite, best first."""
    rows = []
    for segment, tickers in segments.items():
        segment_metrics = {t: metrics_by_ticker[t] for t in tickers if t in metrics_by_ticker}
        if not segment_metrics:
            continue

        ranks_by_metric = {}
        for metric, direction in METRIC_DIRECTIONS.items():
            values = {t: m.get(metric) for t, m in segment_metrics.items()}
            ranks_by_metric[metric] = _percentile_ranks(values, direction)

        for ticker, metrics in segment_metrics.items():
            scores = [
                ranks_by_metric[metric][ticker]
                for metric in METRIC_DIRECTIONS
                if ticker in ranks_by_metric[metric] and metrics.get(metric) is not None
            ]
            if not scores:
                continue
            composite = sum(scores) / len(scores)
            trend = metrics.get("gross_margin_trend")
            passes = composite >= pass_threshold and (trend is None or trend > MARGIN_DETERIORATION_FLOOR)
            rows.append({
                "ticker": ticker,
                "segment": segment,
                "name": metrics.get("name", ticker),
                "composite": round(composite, 3),
                "passes": passes,
                "metrics": metrics,
            })

    rows.sort(key=lambda r: r["composite"], reverse=True)
    return rows


def _fmt(value: Optional[float], pct: bool = False) -> str:
    if value is None:
        return "—"
    return f"{value * 100:.1f}%" if pct else f"{value:.2f}"


def to_markdown_table(rows: list, top_n: int = 15) -> str:
    lines = [
        "| Ticker | Segment | Score | EV/EBITDA | FCF Yield | PEG | Net Debt/EBITDA | GM Trend |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for row in rows[:top_n]:
        m = row["metrics"]
        flag = " ✓" if row["passes"] else ""
        lines.append(
            f"| {row['ticker']}{flag} | {row['segment']} | {row['composite']:.2f} "
            f"| {_fmt(m.get('ev_ebitda'))} | {_fmt(m.get('fcf_yield'), pct=True)} "
            f"| {_fmt(m.get('peg'))} | {_fmt(m.get('net_debt_ebitda'))} "
            f"| {_fmt(m.get('gross_margin_trend'))}pp |"
        )
    return "\n".join(lines)
