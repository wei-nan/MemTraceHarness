"""A throw-away workspace for tests that need a real `.harness.env`."""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from memtrace_harness.config import HarnessConfig
from memtrace_harness.workspace import (
    MODEL_ENV_VARS,
    init_workspace,
    load_workspace,
    models_file,
    update_model_settings,
    write_env_updates,
)

# Settings a developer's shell might carry that would leak into a test's HarnessConfig.
_CLEAR = (
    "HARNESS_WORKSPACE", "HARNESS_CHAT_PROVIDER", "HARNESS_CHAT_MODEL", "HARNESS_CHAT_FALLBACKS",
    "HARNESS_DIGEST_PROVIDER", "HARNESS_DIGEST_MODEL", "HARNESS_DIGEST_FALLBACKS",
    "HARNESS_RECALL_PROVIDER", "HARNESS_RECALL_MODEL", "HARNESS_RECALL_FALLBACKS",
    "HARNESS_ROLE_PROFILES_FILE", "HARNESS_MEMORY_WORKSPACE_ID", "HARNESS_TRACE_DB", "HARNESS_TRACE_ROOT",
    "HARNESS_API_TOKEN", "HARNESS_PEER_API_TOKEN", "HARNESS_PEER_MAX_CONCURRENT", "HARNESS_PEER_NAME", "HARNESS_PEER_DESCRIPTION", "HARNESS_API_PORT", "MEMTRACE_MCP_URL", "MEMTRACE_API_TOKEN",
)

SCOPE = "# Harness scope — TestProj\n\n- workspace_id: ws_test\n"


@contextmanager
def workspace(env: dict[str, str] | None = None, *, scope: str | None = SCOPE):
    """Yields (root, config): an initialised workspace, loaded into a private copy of the
    environment. `env` is given by environment-variable name; the preferences among it (models,
    memory workspace, behaviour) go into `.harness/models.toml`, the rest into `.harness.env`."""
    with TemporaryDirectory() as tmp:
        root = Path(tmp) / "ws"
        paths = init_workspace(root)
        if scope is not None:
            paths["scope"].write_text(scope, encoding="utf-8")
        if env:
            environment = {k: v for k, v in env.items() if k not in MODEL_ENV_VARS}
            preferences = {k: v for k, v in env.items() if k in MODEL_ENV_VARS}
            if environment:
                write_env_updates(paths["env"], environment)
            if preferences:
                update_model_settings(models_file(root), preferences)
        with patch.dict(os.environ, {}, clear=False):
            for key in _CLEAR:
                os.environ.pop(key, None)
            load_workspace(root)
            yield root, HarnessConfig.from_env()
