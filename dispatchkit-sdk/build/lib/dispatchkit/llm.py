"""
LLM access for agents: AnthropicBedrock with built-in cost guardrails.

- LLM_DISABLED=1        -> return the stub, never touch the network
- LLM_MAX_INPUT_CHARS   -> prompt truncation (default 24000)
- LLM_MAX_CALLS_PER_DAY -> daily budget persisted in data_dir/llm_usage.json
                           (default 50, 0 = unlimited)
- BEDROCK_MODEL_ID / AWS_REGION -> model + region

Requires anthropic[bedrock] (baked into the agent image); imported lazily so
the SDK works without it when the LLM is unused.
"""

import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

USAGE_FILE = "llm_usage.json"
USAGE_LOG_FILE = "llm_usage.jsonl"
DEFAULT_MODEL = "us.anthropic.claude-sonnet-5"


class LLMBudgetExceeded(RuntimeError):
    pass


def enabled() -> bool:
    return os.environ.get("LLM_DISABLED", "0") != "1"


def _data_dir(data_dir: Optional[Path]) -> Path:
    return Path(data_dir or os.environ.get("AGENT_DATA_DIR", "./data"))


def _read_usage(data_dir: Path) -> dict:
    usage_path = data_dir / USAGE_FILE
    today = datetime.utcnow().strftime("%Y-%m-%d")
    usage = {"date": today, "calls": 0}
    if usage_path.exists():
        try:
            usage = json.loads(usage_path.read_text())
        except json.JSONDecodeError:
            pass
    if usage.get("date") != today:
        usage = {"date": today, "calls": 0}
    return usage


def _check_budget(data_dir: Path) -> None:
    max_calls = int(os.environ.get("LLM_MAX_CALLS_PER_DAY", "50"))
    if max_calls <= 0:
        return
    usage = _read_usage(data_dir)
    if usage["calls"] >= max_calls:
        raise LLMBudgetExceeded(f"LLM daily budget reached ({usage['calls']}/{max_calls})")


def _record_call(data_dir: Path) -> None:
    """Count a call against the daily budget only after it succeeded — a day
    of failed calls (bad credentials, throttling) must not exhaust the budget
    and wedge the agent for the rest of the day.
    """
    usage = _read_usage(data_dir)
    usage["calls"] += 1
    usage_path = data_dir / USAGE_FILE
    usage_path.parent.mkdir(parents=True, exist_ok=True)
    usage_path.write_text(json.dumps(usage))


def ask(
    prompt: str,
    *,
    system: Optional[str] = None,
    max_tokens: int = 2000,
    data_dir: Optional[Path] = None,
    stub: Optional[str] = None,
) -> str:
    if not enabled():
        return stub if stub is not None else "[llm disabled] " + prompt[:200]

    directory = _data_dir(data_dir)
    _check_budget(directory)

    max_chars = int(os.environ.get("LLM_MAX_INPUT_CHARS", "24000"))
    prompt = prompt[:max_chars]

    from anthropic import AnthropicBedrock

    client = AnthropicBedrock(aws_region=os.environ.get("AWS_REGION", "us-east-1"))
    model = os.environ.get("BEDROCK_MODEL_ID", DEFAULT_MODEL)

    kwargs = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    if system:
        kwargs["system"] = system

    response = client.messages.create(**kwargs)
    _record_call(directory)
    _log_usage(directory, model, response)
    text = "".join(block.text for block in response.content if block.type == "text")
    if getattr(response, "stop_reason", None) == "max_tokens":
        logger.warning(
            "LLM reply hit max_tokens=%s (%s chars of text returned); the model's reasoning counts "
            "against the limit, so raise max_tokens or shorten the prompt", max_tokens, len(text),
        )
    return text


def _log_usage(data_dir: Path, model: str, response) -> None:
    """Append per-call token usage to data_dir/llm_usage.jsonl for platform metering."""
    try:
        usage = getattr(response, "usage", None)
        entry = {
            "ts": datetime.utcnow().isoformat() + "Z",
            "model": model,
            "input_tokens": int(getattr(usage, "input_tokens", 0) or 0),
            "output_tokens": int(getattr(usage, "output_tokens", 0) or 0),
            "cache_creation_tokens": int(getattr(usage, "cache_creation_input_tokens", 0) or 0),
            "cache_read_tokens": int(getattr(usage, "cache_read_input_tokens", 0) or 0),
        }
        path = data_dir / USAGE_LOG_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        logger.warning("failed to log llm usage", exc_info=True)


def ask_json(
    prompt: str,
    *,
    system: Optional[str] = None,
    max_tokens: int = 3000,
    data_dir: Optional[Path] = None,
    stub: Optional[dict] = None,
) -> dict:
    if not enabled():
        return stub if stub is not None else {}

    text = ask(prompt, system=system, max_tokens=max_tokens, data_dir=data_dir)
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass
    logger.warning("ask_json: response was not parseable JSON")
    return {}
