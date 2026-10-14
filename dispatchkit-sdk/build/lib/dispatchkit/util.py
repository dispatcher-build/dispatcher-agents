"""
Scheduling helpers for phase-based agents: persist ISO timestamps in the
agent's state dict and check staleness to decide which phase is due.
"""

from datetime import datetime


def now_iso() -> str:
    return datetime.utcnow().isoformat()


def hours_since(iso_ts: str) -> float:
    if not iso_ts:
        return float("inf")
    try:
        return (datetime.utcnow() - datetime.fromisoformat(iso_ts.rstrip("Z"))).total_seconds() / 3600
    except ValueError:
        return float("inf")


def due(state: dict, key: str, every_hours: float) -> bool:
    return hours_since(state.get(key, "")) > every_hours


def mark(state: dict, key: str) -> None:
    state[key] = now_iso()
