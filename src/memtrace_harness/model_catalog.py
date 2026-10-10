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
import urllib.error
import urllib.request
from pathlib import Path

from memtrace_harness.cli_process import CliProcessRunner
from memtrace_harness.config import HarnessConfig

logger = logging.getLogger(__name__)

CACHE_SECONDS = 600
AGY_TIMEOUT_SECONDS = 15
API_TIMEOUT_SECONDS = 10
# The Claude CLI cannot list its models (and provider HTTP is off limits), so this is a seed:
# the stable aliases plus the full ids of the current and previous generations. A model that
# is newer than this list is added from the status page ("自訂"), which remembers it in
# `.harness/extra_models.json` for every dropdown — no code change needed.
CLAUDE_ALIASES = (
    "opus",
    "sonnet",
    "haiku",
    "claude-fable-5-1",
    "claude-opus-5-5",
    "claude-opus-5",
    "claude-sonnet-5-5",
    "claude-sonnet-5",
    "claude-haiku-5-5",
    "claude-opus-4-8",
    "claude-sonnet-4-6",
    "claude-haiku-4-5",
)
EXTRA_MODELS_FILE = "extra_models.json"
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


def _claude_models_from_api(workspace_root: Path | None) -> list[str]:
    """Claude's ids from the Models endpoint, newest first. The one place the Harness talks to a
    provider over HTTP: a read-only listing with a key that is allowed to do nothing else (see
    "Model list key" in the operating contract). Empty when no key is set or the call fails."""
    from memtrace_harness.workspace import model_list_key

    key = model_list_key(workspace_root)
    if not key:
        return []
    request = urllib.request.Request(
        "https://api.anthropic.com/v1/models?limit=100",
        headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
    )
    try:
        with urllib.request.urlopen(request, timeout=API_TIMEOUT_SECONDS) as response:
            data = json.loads(response.read().decode("utf-8")).get("data") or []
    except urllib.error.HTTPError as exc:
        logger.warning("listing Claude models failed: HTTP %s; using the built-in list", exc.code)
        return []
    except (OSError, ValueError):
        logger.warning("listing Claude models failed; using the built-in list")
        return []
    return [
        item["id"]
        for item in data
        if isinstance(item, dict) and isinstance(item.get("id"), str) and _MODEL_ID.match(item["id"])
    ]


def provider_models(config: HarnessConfig, *, home: Path | None = None) -> dict[str, list[str]]:
    """{provider: models it currently offers}, cached for CACHE_SECONDS."""
    global _cache
    with _lock:
        if _cache is not None and time.monotonic() - _cache[0] < CACHE_SECONDS:
            return _cache[1]
        catalog: dict[str, list[str]] = {"claude": list(CLAUDE_ALIASES), "codex": [], "antigravity": []}
        try:
            listed = _claude_models_from_api(config.workspace_root)
            if listed:
                catalog["claude"] = list(CLAUDE_ALIASES[:3]) + [m for m in listed if m not in CLAUDE_ALIASES[:3]]
        except Exception:
            logger.exception("listing Claude models failed; menus use the built-in list")
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


def _extra_models_path(root: Path) -> Path:
    return root / ".harness" / EXTRA_MODELS_FILE


def extra_models(root: Path | None) -> dict[str, list[str]]:
    """Models the operator added by hand, {provider: [model, ...]}."""
    if root is None:
        return {}
    try:
        data = json.loads(_extra_models_path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        str(provider): [m for m in models if isinstance(m, str)]
        for provider, models in data.items()
        if isinstance(models, list)
    }


def remember_models(root: Path | None, pairs: list[tuple[str, str]]) -> None:
    """Add (provider, model) pairs to the operator's own model list so every dropdown offers them."""
    if root is None:
        return
    known = extra_models(root)
    changed = False
    for provider, model in pairs:
        model = model.strip()
        if model and _MODEL_ID.match(model) and model not in known.setdefault(provider, []):
            known[provider].append(model)
            changed = True
    if changed:
        path = _extra_models_path(root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(known, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
