"""
DispatchKit Events

Event is the shape passed to Agent.on_event and described by Agent.emit.
There is no inter-agent bus: emit() logs a structured line and on_event()
is never invoked by the runtime, so both are safe no-ops for agents that
still call or override them.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class Event:
    type: str
    source: str
    payload: dict = field(default_factory=dict)
    timestamp: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    target: Optional[str] = None
