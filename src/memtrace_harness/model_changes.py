"""Permanent model changes the chat model may *propose* ("以後 developer 都用 opus 5.5").

Like github_actions.py: the chat model's ``propose_model_change`` tool (ops_mcp.py) only records a request; the
Harness shows the exact change and writes it after a confirm tap (service.py). Only a role's or a background
job's *primary* model changes — fallbacks, reasoning effort, permission and context policy stay as they are.
For a model used once, see model_override.py instead.
"""

from __future__ import annotations

import re
from typing import Any

from memtrace_harness.model_override import infer_provider
from memtrace_harness.role_profiles import canonical_profile_id

PROVIDERS = ("claude", "codex", "antigravity", "cursor")
ENV_TARGETS = {"chat": "對話", "digest": "夜間摘要", "archive": "記憶歸檔", "audit": "夜間稽核", "recall": "主題回想"}
ROLE_TARGETS = {
    "controller": "Controller",
    "plan": "Plan skill",
    "plan-escalation": "Plan skill (escalation)",
    "review": "Review skill",
    "develop": "Develop skill",
}
TARGETS = (*ENV_TARGETS, *ROLE_TARGETS)
_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.\-]{0,79}$")


def validate_model_change(arguments: dict[str, Any]) -> dict[str, str]:
    target = canonical_profile_id(str(arguments.get("target") or "").strip())
    if target not in TARGETS:
        raise ValueError(f"target must be one of: {', '.join(TARGETS)}")
    model = str(arguments.get("model") or "").strip()
    provider = str(arguments.get("provider") or "").strip()
    if "/" in model:
        provider, _, model = model.rpartition("/")
    if not _MODEL.match(model):
        raise ValueError("model must be a plain model id such as claude-opus-5-5")
    provider = provider or infer_provider(model) or ""
    if provider not in PROVIDERS:
        raise ValueError(f"cannot tell the provider of {model!r}; give provider as one of {', '.join(PROVIDERS)}")
    return {"target": target, "provider": provider, "model": model}


def summarize(payload: dict[str, Any]) -> str:
    label = {**ENV_TARGETS, **ROLE_TARGETS}[payload["target"]]
    return f"把「{label}」的主要模型永久改成 {payload['provider']}/{payload['model']}（備援與其他設定不變）"


def apply_model_change(config: Any, payload: dict[str, Any]) -> None:
    """Write the change (raises ValueError with text for the operator). Takes effect on the next call."""
    from memtrace_harness import admin
    from memtrace_harness.model_catalog import remember_models
    from memtrace_harness.role_profiles import load_role_profiles

    change = validate_model_change(payload)
    target, provider, model = change["target"], change["provider"], change["model"]
    if target in ENV_TARGETS:
        prefix = f"HARNESS_{target.upper()}"
        admin._check_model(provider, model)
        admin._apply(config, {f"{prefix}_PROVIDER": provider, f"{prefix}_MODEL": model})
    else:
        path = config.role_profiles_file_for()
        before = path.read_text(encoding="utf-8") if path is not None and path.is_file() else None
        admin.update_role_profile(config, target, provider, model)
        try:
            load_role_profiles(path)  # e.g. the role's reasoning effort may not exist for the new provider
        except ValueError as exc:
            if before is not None:
                path.write_text(before, encoding="utf-8")
            raise ValueError(f"{provider} does not accept this role's current settings, nothing changed: {exc}") from exc
    remember_models(config.workspace_root, [(provider, model)])
