"""Which models each provider CLI currently offers, for the status page's model menus.

The menus used to offer only models already configured somewhere in this harness, so a
newly released model (gpt-6-luna, 2026-10-02) could only be typed in by hand. Each CLI
can tell us itself: Codex keeps the catalog it fetched in <CODEX_HOME>/models_cache.json,
Antigravity lists its models with `agy models`, and Claude Code takes stable aliases.
Best-effort and cached: a CLI that is missing or slow just contributes nothing.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from pathlib import Path

from memtrace_harness.cli_process import CliProcessRunner
from memtrace_harness.config import HarnessConfig

logger = logging.getLogger(__name__)

CACHE_SECONDS = 3600
AGY_TIMEOUT_SECONDS = 15
CLAUDE_ALIASES = ("opus", "sonnet", "haiku")
_MODEL_ID = re.compile(r"^[a-z0-9][a-z0-9.\-]*$")

_lock = threading.Lock()
_cache: tuple[float, dict[str, list[str]]] | None = None


def _codex_models(home: Path) -> list[str]:
    """Union of every Codex home's cached catalog (the harness may run Codex with its
    own CODEX_HOME, e.g. ~/.codex-william), newest-first as Codex lists them, hidden
    entries left out."""
    models: list[str] = []
    for cache in sorted(home.glob(".codex*/models_cache.json")):
        try:
            entries = json.loads(cache.read_text(encoding="utf-8")).get("models") or []
        except (OSError, ValueError, AttributeError):
            continue
        for entry in entries:
            slug = entry.get("slug") if isinstance(entry, dict) else None
            if slug and entry.get("visibility", "list") == "list" and slug not in models:
                models.append(slug)
    return models


def _antigravity_models(config: HarnessConfig) -> list[str]:
    result = CliProcessRunner().run(
        [config.antigravity_command, "models"], cwd=Path.home(), timeout_seconds=AGY_TIMEOUT_SECONDS
    )
    if result.return_code != 0:
        return []
    models = []
    for line in result.stdout.splitlines():
        first = line.strip().split("\t")[0].split(" ")[0]
        if _MODEL_ID.match(first) and first not in models:
            models.append(first)
    return models


def provider_models(config: HarnessConfig, *, home: Path | None = None) -> dict[str, list[str]]:
    """{provider: models it currently offers}, cached for CACHE_SECONDS."""
    global _cache
    with _lock:
        if _cache is not None and time.monotonic() - _cache[0] < CACHE_SECONDS:
            return _cache[1]
        catalog: dict[str, list[str]] = {"claude": list(CLAUDE_ALIASES), "codex": [], "antigravity": []}
        try:
            catalog["codex"] = _codex_models(home or Path.home())
        except Exception:
            logger.exception("reading the Codex model catalog failed; menus fall back to configured models")
        try:
            catalog["antigravity"] = _antigravity_models(config)
        except Exception:
            logger.exception("listing Antigravity models failed; menus fall back to configured models")
        _cache = (time.monotonic(), catalog)
        return catalog


def clear_cache() -> None:
    global _cache
    with _lock:
        _cache = None
