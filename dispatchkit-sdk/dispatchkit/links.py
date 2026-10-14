"""Agent links: publish files for other workers, read files others published.

A worker publishes by writing regular files under data/out/. The platform
copies them into data/in/<name>/ of every worker that lists this one as an
input (see backend/agents/README.md), right before that worker starts or
wakes. Nothing else under data/ ever leaves the worker.
"""

import os
import re
from pathlib import Path
from typing import Dict, List, Optional, Union

OUT_DIR = "out"
IN_DIR = "in"
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def publish(data_dir: Path, name: str, content: Union[str, bytes]) -> Path:
    """Atomically write data/out/<name>. Returns the path written."""
    if not NAME_RE.match(name):
        raise ValueError(f"Invalid published file name {name!r}")
    out_dir = Path(data_dir) / OUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    final = out_dir / name
    tmp = out_dir / f".{name}.tmp"
    if isinstance(content, str):
        tmp.write_text(content, encoding="utf-8")
    else:
        tmp.write_bytes(content)
    os.replace(tmp, final)
    return final


def inputs(data_dir: Path) -> Dict[str, List[Path]]:
    """Every linked input folder and the regular files inside it."""
    in_dir = Path(data_dir) / IN_DIR
    if not in_dir.is_dir():
        return {}
    found: Dict[str, List[Path]] = {}
    for folder in sorted(in_dir.iterdir()):
        if not folder.is_dir() or folder.name.startswith("."):
            continue
        found[folder.name] = sorted(
            p for p in folder.iterdir() if p.is_file() and not p.name.startswith(".")
        )
    return found


def input_path(data_dir: Path, alias: str, name: str) -> Optional[Path]:
    """The file a linked source published under <alias>, or None when absent."""
    path = Path(data_dir) / IN_DIR / alias / name
    return path if path.is_file() else None
