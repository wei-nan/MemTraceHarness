"""A service's own environment file: KEY=VALUE lines, `#` comments, optional `export `, one layer of
matching quotes. (Deliberately not shared with the Harness's workspace loader: services and
Harnesses do not import each other.)"""

from __future__ import annotations

import os
from pathlib import Path


def parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        values[key.strip()] = value
    return values


def load_env_file(path: Path | None, prefixes: tuple[str, ...] = ("TELEGRAM_", "HARNESS_", "WEB_")) -> dict[str, str]:
    """The service's settings: its env file overlaid with matching variables of the real
    environment (which win, so a one-off override on the command line behaves as expected)."""
    values = parse_env_file(path) if path is not None else {}
    values.update({k: v for k, v in os.environ.items() if k.startswith(prefixes)})
    return values


def file_mode_problem(path: Path) -> str | None:
    """A file holding a token should be for its owner only."""
    if os.name == "nt":
        return None
    try:
        mode = path.stat().st_mode
    except OSError:
        return None
    if mode & 0o077:
        return f"{path} is readable by other users; run: chmod 600 {path}"
    return None
