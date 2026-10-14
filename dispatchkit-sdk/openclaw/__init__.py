"""Backwards-compatibility alias: agents installed before the 2026-08-25 rename
import `openclaw`; the SDK now lives at `dispatchkit`. Installed agent dirs are
frozen copies we can't rewrite (prod EFS), so this alias must stay until no
agent dir imports `openclaw` anymore."""

import importlib
import sys

import dispatchkit
from dispatchkit import *  # noqa: F401,F403

for _mod in ("agent", "events", "inbox", "llm", "notify", "records", "util"):
    sys.modules[f"openclaw.{_mod}"] = importlib.import_module(f"dispatchkit.{_mod}")

__all__ = getattr(dispatchkit, "__all__", [])
