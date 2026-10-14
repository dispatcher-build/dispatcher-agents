"""
Agent skeleton — the canonical DispatchKit worker.

The SDK owns the loop. You declare phases with @phase and the SDK runs the
first one that is due each tick, stamping `last_<phase>` into self.state so
the schedule survives restarts and scheduled wakes. Surface results with
self.record(); flag the ones a person must see with self.alert().

See backend/agents/README.md for the full contract and
backend/agents/stock-research/ for a complete real agent.
"""

import asyncio
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "lib"))

from dispatchkit import Agent, phase

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

WORK_INTERVAL_HOURS = float(os.environ.get("WORK_INTERVAL_HOURS", "1"))


class MyAgent(Agent):
    async def setup(self):
        logger.info(f"{self.config.name} ready | goal: {self.config.goal}")

    @phase(every_hours=WORK_INTERVAL_HOURS)
    async def work(self):
        result = f"did one unit of work (cycle {self._cycle})"
        self.record(
            "note",
            payload={"body_md": f"**Work done:** {result}"},
            title=result,
            tags=["work"],
        )
        return {"result": result}

    async def on_command(self, command: dict):
        text = command.get("text", "").strip().lower()
        if text == "status":
            return f"Last work: {self.state.get('last_work', 'never')}. Interval: {WORK_INTERVAL_HOURS}h."
        return await super().on_command(command)


if __name__ == "__main__":
    asyncio.run(MyAgent().run())
