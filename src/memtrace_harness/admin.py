"""Reading and changing a running Harness's own state, for whoever administers it.

`collect_status_data()` is the one JSON-safe description of this Harness (one workspace, one
project) that the CLI's `status` command, the Harness API (api.py) and, through it, the status web
service all read, so they never drift apart. The `update_*` functions change this workspace's model
settings: they write `.harness/models.toml` (so a restart keeps them) and the process environment (so
the next call already uses them).
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from memtrace_harness.memory_audit import audit_status
from memtrace_harness.approval import INFO_NEEDED_REASONS
from memtrace_harness.config import HarnessConfig, _parse_chat_fallbacks
from memtrace_harness.inflight import default_tracker
from memtrace_harness.memory_digest import PREFERENCE_CATEGORY_LABELS
from memtrace_harness.role_profiles import load_role_profiles
from memtrace_harness.scope import ProjectScope
from memtrace_harness.trace_store import TraceStore
from memtrace_harness.workspace import STATE_DIR_NAME, models_file, update_model_settings

logger = logging.getLogger(__name__)

PROVIDERS = ("claude", "codex", "antigravity", "cursor")


def merge_model_catalog(config: HarnessConfig, configured: dict[str, set[str]]) -> dict[str, list[str]]:
    """What each provider CLI currently offers (newest first, see model_catalog.py),
    followed by anything configured here that the catalog doesn't list. A catalog
    failure only means the menu falls back to the configured models."""
    from memtrace_harness.model_catalog import extra_models, provider_models

    try:
        catalog = provider_models(config)
    except Exception:
        logger.exception("model catalog lookup failed; using configured models only")
        catalog = {}
    extras = extra_models(config.workspace_root)
    merged: dict[str, list[str]] = {}
    for provider, models in configured.items():
        offered = list(catalog.get(provider) or [])
        offered += [m for m in extras.get(provider, []) if m not in offered]
        merged[provider] = offered + sorted(m for m in models if m not in offered)
    return merged


# How many recent nightly digests per project the status page shows.
STATUS_DIGEST_DAYS = 7
# How many of the most recently retired preferences the status page lists (with a way back).
STATUS_RETIRED_PREFERENCES = 10


CONFIRMATION_LABELS = {
    "job": "待確認重跑工作",
    "scope_change": "待確認新增工具設定",
    "external_action": "待確認 GitHub 操作",
    "model_change": "待確認模型變更",
    "order": "待確認委託",
}
INTERRUPTED_LOOKBACK = timedelta(days=7)
INTERRUPTED_AFTER = timedelta(minutes=10)


def collect_waiting(trace_store: TraceStore, scope: ProjectScope, pending_approvals: list[dict], locks: list[dict]) -> dict:
    """Everything of this project that waits on a person, so the status page can say how many and what.
    Read-only; no network (the GitHub backlog comes from the last scan pass's snapshot)."""
    now = datetime.now(timezone.utc)
    items: list[dict] = []
    for req in pending_approvals:
        needs_answer = req["reason"] in INFO_NEEDED_REASONS
        card = trace_store.get_approval_decision_card(req["id"]) if needs_answer else None
        items.append(
            {
                "kind": "approval",
                "id": req["id"],
                "label": "待回答" if needs_answer else "待核准",
                "summary": (req["proposed_action"] or "").strip().splitlines()[0][:140] if req["proposed_action"] else "",
                "since": req["created_at"],
                "needs_answer": needs_answer,
                "options": [{"label": o["label"], "action": o["action"], "tradeoff": o.get("tradeoff", "")} for o in card["options"]] if card else [],
                "recommended": card.get("recommended") if card else None,
                "situation": card.get("situation") if card else None,
            }
        )
    for conf in trace_store.list_open_confirmations(scope.name):
        items.append(
            {
                "kind": "confirm", "id": conf["id"], "label": CONFIRMATION_LABELS.get(conf["kind"], "待確認"),
                "summary": conf["summary"], "since": conf["since"], "expires_at": conf["expires_at"],
            }
        )
    for question in trace_store.list_open_chat_questions(scope.name):
        items.append(
            {
                "kind": "question", "id": question["question_id"], "label": "待你選擇",
                "summary": question["question"], "since": question["created_at"],
                "options": [{"label": o["label"], "action": o["text"]} for o in question["options"]],
            }
        )
    live = {entry["conversation_id"] for entry in default_tracker.snapshot()}
    lock_ids = {lock["conversation_id"] for lock in locks}
    for conv in trace_store.list_unfinished_conversations(scope.workspace_id, (now - INTERRUPTED_LOOKBACK).isoformat()):
        if conv["id"] in live:
            continue
        try:
            idle = now - datetime.fromisoformat(conv["updated_at"])
        except ValueError:
            continue
        if idle < INTERRUPTED_AFTER:
            continue
        holds = conv["id"] in lock_ids
        items.append(
            {
                "kind": "interrupted", "id": conv["id"],
                "label": "疑似中斷的任務" + ("（鎖還被它佔著，會擋住新任務）" if holds else ""),
                "summary": (conv["goal"] or "").strip().splitlines()[0][:140] if conv["goal"] else conv["id"],
                "since": conv["updated_at"], "holds_lock": holds,
            }
        )
    # Who holds the workspace right now, read live: a scan pass is skipped while anyone does, and the page
    # must say so (and who) only for as long as it is true.
    holders = [
        {"conversation_id": lock["conversation_id"], "locked_at": lock["locked_at"], "alive": lock["conversation_id"] in live}
        for lock in locks
    ]
    return {"total": len(items), "items": items, "backlog": trace_store.get_scan_snapshot(scope.name), "lock_holders": holders}


def collect_status_data(config: HarnessConfig, scope: ProjectScope, started_at: str | None = None) -> dict:
    """Gather the harness's current diagnostic state as plain structured data (JSON-
    safe: only str/int/bool/list/dict). Read-only — never modifies process state,
    files, or the trace store. This is the single source of truth for both the CLI
    `status` command (rendered as text by _collect_status_text) and the web status
    dashboard (rendered as cards, pushed live over SSE as JSON) — they read the same
    shape so they never drift apart."""
    trace_store = TraceStore(config.trace_db_path)

    harness_info = {
        "pid": os.getpid(),
        "started_at": started_at,
        "workspace_dir": str(config.workspace_root) if config.workspace_root else None,
        "api_port": config.api_port,
        "outbox_backlog": trace_store.outbox_backlog(),
    }

    project_entries = []
    # Built up across every project below: every (provider, model) pair actually
    # configured anywhere in this harness (role profiles, their fallbacks, chat
    # candidates) — the dashboard's model dropdowns are populated from this rather
    # than a hardcoded catalog, since model names change too often to keep a static
    # list current (see profiles/beri.toml's own comments verifying names against
    # `agy models` at the time). A "自訂…" option still covers anything not seen yet.
    known_models: dict[str, set[str]] = {p: set() for p in PROVIDERS}
    for scope in [scope]:

        session_id = f"psess_{scope.name}"
        turn_count = len(trace_store.get_primary_session_turns(session_id))
        pending_goal = len(trace_store.get_unconsolidated_turns(session_id))
        digests = trace_store.list_memory_digests(scope.name, limit=STATUS_DIGEST_DAYS)
        locks = trace_store.list_workspace_locks(scope.workspace_id)
        lock = locks[0] if locks else None
        queued_tasks = trace_store.list_queued_tasks(scope.workspace_id)
        pending_approvals = trace_store.list_pending_approvals_for_workspace(scope.workspace_id)
        recent_turns = trace_store.get_recent_primary_session_turns(session_id, limit=5)
        schedules = trace_store.list_schedules(scope.name)

        # A needs_human stop releases the workspace lock (approving doesn't need to
        # hold it — see agent_loop_background_execution memory note) even though the
        # conversation is very much unfinished: there's a pending approval sitting on
        # it. Without this fallback, the pipeline stepper would disappear the moment a
        # gate stops, which is exactly when a human most wants to see it. Prefer the
        # lock's conversation when actually locked (mid-run); otherwise fall back to
        # the oldest pending approval's conversation.
        pipeline_conversation_id = (
            lock["conversation_id"]
            if lock
            else (pending_approvals[0]["conversation_id"] if pending_approvals else None)
        )
        pipeline = (
            trace_store.get_conversation_pipeline(pipeline_conversation_id)
            if pipeline_conversation_id
            else []
        )

        chat_candidates = config.chat_candidates_for(scope.name)
        digest_candidates = config.digest_candidates_for(scope.name)
        digest_is_own = bool(os.getenv("HARNESS_DIGEST_PROVIDER") or config.digest_provider)
        archive_candidates = config.archive_candidates_for(scope.name)
        archive_is_own = bool(os.getenv("HARNESS_ARCHIVE_PROVIDER") or config.archive_provider)
        audit_candidates = config.audit_candidates_for(scope.name)
        audit_is_own = bool(os.getenv("HARNESS_AUDIT_PROVIDER") or config.audit_provider)
        recall_candidates = config.recall_candidates_for(scope.name)
        recall_is_own = bool(os.getenv("HARNESS_RECALL_PROVIDER") or config.recall_provider)
        now_iso = datetime.now(timezone.utc).isoformat()
        topic_briefs = trace_store.list_active_topic_briefs(scope.name, now_iso)
        recall_state = trace_store.get_recall_state(scope.name)

        role_profiles_entries: list[dict] | None = None
        role_profiles_error: str | None = None
        try:
            role_profiles = load_role_profiles(config.role_profiles_file_for(scope.name))
            role_profiles_entries = [
                {
                    "id": profile_id,
                    "provider": profile.provider,
                    "model": profile.model,
                    "reasoning_effort": profile.reasoning_effort,
                    "fallbacks": [
                        {"provider": f.provider, "model": f.model, "reasoning_effort": f.reasoning_effort}
                        for f in profile.fallbacks
                    ],
                }
                for profile_id, profile in role_profiles.items()
            ]
        except Exception as exc:
            role_profiles_error = str(exc)

        for provider, model in (*chat_candidates, *digest_candidates, *archive_candidates, *recall_candidates):
            if provider in known_models and model:
                known_models[provider].add(model)
        for entry in role_profiles_entries or []:
            if entry["provider"] in known_models and entry["model"]:
                known_models[entry["provider"]].add(entry["model"])
            for fb in entry["fallbacks"]:
                if fb["provider"] in known_models and fb["model"]:
                    known_models[fb["provider"]].add(fb["model"])

        project_entries.append(
            {
                "name": scope.name,
                "workspace_id": scope.workspace_id,
                "working_directory": str(scope.working_directory),
                "repos": [
                    {"name": scope.name, "primary": True, "path": str(scope.working_directory),
                     "exists": scope.working_directory.is_dir(), "verify_command": scope.verify_command,
                     "github_repo": scope.github_repo},
                    *[
                        {"name": name, "primary": False, "path": str(repo.path), "exists": repo.path.is_dir(),
                         "verify_command": repo.verify_command, "github_repo": repo.github_repo}
                        for name, repo in scope.repos.items()
                    ],
                ],
                "dedicated": {
                    "chat_model": bool(os.getenv("HARNESS_CHAT_PROVIDER")),
                    "memory": bool(os.getenv("HARNESS_MEMORY_WORKSPACE_ID") or config.harness_memory_workspace_id),
                    "role_profiles": config.role_profiles_file_for(scope.name) is not None,
                },
                "chat_candidates": [
                    {"provider": p, "model": m or None} for p, m in chat_candidates
                ],
                "digest_candidates": [
                    {"provider": p, "model": m or None} for p, m in digest_candidates
                ],
                "digest_is_own": digest_is_own,
                "archive_candidates": [
                    {"provider": p, "model": m or None} for p, m in archive_candidates
                ],
                "archive_is_own": archive_is_own,
                "audit_candidates": [
                    {"provider": p, "model": m or None} for p, m in audit_candidates
                ],
                "audit_is_own": audit_is_own,
                "audit": audit_status(trace_store, scope.name),
                "recall_candidates": [
                    {"provider": p, "model": m or None} for p, m in recall_candidates
                ],
                "recall_is_own": recall_is_own,
                "recall_enabled": config.recall_enabled,
                "recall_state": recall_state,
                "topic_briefs": [
                    {
                        "id": b["id"],
                        "title": b["title"],
                        "summary": b["summary"],
                        "fresh": b["fresh"],
                        "related": b["related"],
                        "updated_at": b["updated_at"],
                        "expires_at": b["expires_at"],
                        "pushed": b["pushed_at"] is not None,
                    }
                    for b in topic_briefs
                ],
                "role_profiles": role_profiles_entries,
                "role_profiles_error": role_profiles_error,
                "turn_count": turn_count,
                "pending_goal": pending_goal,
                "memory_workspace_id": config.memory_workspace_id_for(scope.name, scope.workspace_id),
                "digests": [
                    {
                        "digest_date": d["digest_date"],
                        "turn_count": d["turn_count"],
                        "provider": d["provider"],
                        "model": d["model"],
                        "synced": d["memtrace_node_id"] is not None,
                        "digest": d["digest"],
                    }
                    for d in digests
                ],
                "recent_turns": [
                    {
                        "created_at": turn["created_at"],
                        "speaker": turn["speaker"],
                        "content": turn["content"],
                    }
                    for turn in recent_turns
                ],
                "lock": (
                    {
                        "conversation_id": lock["conversation_id"],
                        "locked_at": lock["locked_at"],
                        "latest_turn": trace_store.get_latest_turn(lock["conversation_id"]),
                    }
                    if lock
                    else None
                ),
                "max_workers": scope.max_workers,
                "locks": [
                    {
                        "conversation_id": item["conversation_id"],
                        "locked_at": item["locked_at"],
                        "schedule_id": item["schedule_id"],
                        "latest_turn": trace_store.get_latest_turn(item["conversation_id"]),
                    }
                    for item in locks
                ],
                "queue": [
                    {
                        "id": item["id"],
                        "kind": item["kind"],
                        "schedule_id": item["schedule_id"],
                        "goal": (item["payload"].get("goal") or item["conversation_id"] or "")[:120],
                        "created_at": item["created_at"],
                    }
                    for item in queued_tasks
                ],
                "pipeline_conversation_id": pipeline_conversation_id,
                "pipeline": pipeline,
                "pending_approvals": [
                    {
                        "id": req["id"],
                        "reason": req["reason"],
                        "proposed_action": req["proposed_action"],
                    }
                    for req in pending_approvals
                ],
                "waiting": collect_waiting(trace_store, scope, pending_approvals, locks),
                "schedules": [
                    {
                        "id": s["id"],
                        "kind": s["kind"],
                        "interval_seconds": s["interval_seconds"],
                        "time_of_day": s["time_of_day"],
                        "end_time_of_day": s["end_time_of_day"],
                        "next_run_at": s["next_run_at"],
                        "last_run_at": s["last_run_at"],
                        "goal": s["goal"],
                    }
                    for s in schedules
                ],
            }
        )

    preferences = trace_store.list_preference_rules(statuses=("pending", "adopted", "retired"))
    recently_retired = sorted(
        (r for r in preferences if r["status"] == "retired"),
        key=lambda r: r["resolved_at"] or "",
        reverse=True,
    )[:STATUS_RETIRED_PREFERENCES]
    return {
        "harness": harness_info,
        "projects": project_entries,
        "preferences": {
            "pending": [r for r in preferences if r["status"] == "pending"],
            "adopted": [r for r in preferences if r["status"] == "adopted"],
            "retired": recently_retired,
            "category_labels": PREFERENCE_CATEGORY_LABELS,
        },
        "known_models": merge_model_catalog(config, known_models),
    }




def _apply(config: HarnessConfig, updates: dict[str, str]) -> None:
    """Persist in this workspace's `models.toml` and take effect immediately: the model-candidate
    helpers on HarnessConfig read os.environ on every call. Until 2026-10-02 only the file was written,
    so the page re-rendered the still-running old model right after "saved" — it looked as if the save
    had bounced back."""
    if config.workspace_root is None:
        raise ValueError("this Harness was not started from a workspace; there is no models.toml to change")
    update_model_settings(models_file(config.workspace_root), updates)
    os.environ.update(updates)


def _check_model(provider: str, model: str) -> None:
    if provider not in PROVIDERS:
        raise ValueError(f"unknown provider {provider!r} (expected one of {PROVIDERS})")
    if not model.strip():
        raise ValueError("model must be a non-empty string")


def _check_fallbacks(fallbacks: str) -> str:
    pairs = _parse_chat_fallbacks(fallbacks)
    for provider, model in pairs:
        if provider not in PROVIDERS:
            raise ValueError(f"unknown provider {provider!r} (expected one of {PROVIDERS})")
        if not model:
            raise ValueError("every fallback needs a model, written provider/model")
    return ",".join(f"{p}/{m}" for p, m in pairs)


def update_chat_model(config: HarnessConfig, provider: str, model: str) -> None:
    """Set the quick-chat model (the cheap call behind ordinary conversation, not an Agent Loop
    role). Only the primary candidate is touched, never HARNESS_CHAT_FALLBACKS."""
    _check_model(provider, model)
    _apply(config, {"HARNESS_CHAT_PROVIDER": provider, "HARNESS_CHAT_MODEL": model})


def update_digest_model(config: HarnessConfig, provider: str, model: str) -> None:
    """Set the nightly-memory-digest model, independent of the chat model."""
    _check_model(provider, model)
    _apply(config, {"HARNESS_DIGEST_PROVIDER": provider, "HARNESS_DIGEST_MODEL": model})


def update_digest_fallbacks(config: HarnessConfig, fallbacks: str) -> None:
    """`fallbacks` is the "provider/model,provider/model" text HARNESS_DIGEST_FALLBACKS takes. An
    empty string is a real value: it clears the chain."""
    _apply(config, {"HARNESS_DIGEST_FALLBACKS": _check_fallbacks(fallbacks)})


def update_recall_model(config: HarnessConfig, provider: str, model: str) -> None:
    """Same as update_digest_model(), for the background topic-recall model."""
    _check_model(provider, model)
    _apply(config, {"HARNESS_RECALL_PROVIDER": provider, "HARNESS_RECALL_MODEL": model})


def update_recall_fallbacks(config: HarnessConfig, fallbacks: str) -> None:
    _apply(config, {"HARNESS_RECALL_FALLBACKS": _check_fallbacks(fallbacks)})


def _profile_table(text: str, profile_id: str) -> str:
    """The TOML table name (without brackets) that holds this profile in a role-profiles file, whichever
    way the file spells it: the skill name, a pre-skill role name, or — for `plan-escalation` — the plan
    skill's `[profiles.plan.escalation]` sub-table. Raises ValueError when the file has none of them."""
    from memtrace_harness.role_profiles import profile_id_variants

    candidates = [f"profiles.{variant}" for variant in profile_id_variants(profile_id)]
    if profile_id_variants(profile_id)[0] == "plan-escalation":
        candidates.append("profiles.plan.escalation")
    for table in candidates:
        if re.search(rf"(?m)^\[{re.escape(table)}\]", text):
            return table
    raise ValueError(f"profile {profile_id!r} not found in the role-profiles file")


def update_role_profile(config: HarnessConfig, profile_id: str, provider: str, model: str) -> None:
    """Surgical, comment-preserving edit of one role's primary provider/model line in this
    workspace's role-profiles TOML — a targeted text patch, not a full TOML parse+rewrite, since
    these files carry load-bearing decision-rationale comments a round-trip writer would silently
    discard. Only the role's own primary provider/model is touched, never its fallbacks.

    Only a workspace that already has its own roles file can be edited: the packaged default is
    shared by every Harness, and editing it in place would change all of them."""
    if provider not in PROVIDERS:
        raise ValueError(f"unknown provider {provider!r} (expected one of {PROVIDERS})")
    if not model.strip() or '"' in model or "\n" in model:
        raise ValueError("model must be a non-empty string with no quotes or newlines")

    path = config.role_profiles_file_for()
    if path is None:
        raise ValueError(
            "this workspace has no roles file of its own yet (it uses the packaged default, shared "
            "by every Harness) — give it one first with the 獨立出去 button or "
            "`memtrace-harness roles-independent`"
        )
    if not path.is_file():
        raise ValueError(f"the role-profiles file does not exist: {path}")

    text = path.read_text(encoding="utf-8")
    try:
        header = f"[{_profile_table(text, profile_id)}]"
    except ValueError:
        raise ValueError(f"profile {profile_id!r} not found in {path}") from None
    start = text.find(header)
    body_start = start + len(header)
    # Bound this profile's own primary block at the next section header of any kind
    # (a later [profiles.X] or this role's own [[profiles.X.fallbacks]]) — the
    # primary provider/model lines always appear once, before any such header.
    next_header = re.search(r"\n\[", text[body_start:])
    body_end = body_start + next_header.start() if next_header else len(text)
    block = text[body_start:body_end]

    new_block, replaced_provider = re.subn(
        r'(?m)^provider\s*=\s*"[^"]*"', f'provider = "{provider}"', block, count=1
    )
    if not replaced_provider:
        raise ValueError(f"profile {profile_id!r} in {path} has no provider line to update")
    new_block, replaced_model = re.subn(
        r'(?m)^model\s*=\s*"[^"]*"', f'model = "{model}"', new_block, count=1
    )
    if not replaced_model:
        raise ValueError(f"profile {profile_id!r} in {path} has no model line to update")

    path.write_text(text[:body_start] + new_block + text[body_end:], encoding="utf-8")


def _check_chain(chain: object) -> list[dict]:
    if not isinstance(chain, list) or not chain:
        raise ValueError("the chain needs at least a primary model")
    out = []
    for item in chain:
        if not isinstance(item, dict):
            raise ValueError("malformed chain entry")
        provider, model = str(item.get("provider", "")), str(item.get("model", "")).strip()
        _check_model(provider, model)
        if '"' in model or "\n" in model:
            raise ValueError("model must have no quotes or newlines")
        out.append({"provider": provider, "model": model, "reasoning_effort": str(item.get("reasoning_effort") or "")})
    return out


def _remember(config: HarnessConfig, chain: list[dict]) -> None:
    from memtrace_harness.model_catalog import remember_models

    remember_models(config.workspace_root, [(c["provider"], c["model"]) for c in chain])


def update_model_chain(config: HarnessConfig, target: str, chain: object) -> None:
    """Set the whole ordered chain (primary + any number of fallbacks) for `chat`, `digest`,
    `archive`, `audit` or `recall` in one go. Empty fallbacks are a real value: they clear the chain."""
    if target not in ("chat", "digest", "archive", "audit", "recall"):
        raise ValueError(f"unknown model target {target!r}")
    entries = _check_chain(chain)
    prefix = f"HARNESS_{target.upper()}"
    fallbacks = ",".join(f"{c['provider']}/{c['model']}" for c in entries[1:])
    _apply(config, {
        f"{prefix}_PROVIDER": entries[0]["provider"],
        f"{prefix}_MODEL": entries[0]["model"],
        f"{prefix}_FALLBACKS": fallbacks,
    })
    _remember(config, entries)


def update_role_profile_chain(config: HarnessConfig, profile_id: str, chain: object) -> None:
    """Rewrite one role's primary model and its whole fallback list (any number), keeping the
    rest of the file — comments, other roles, the role's other settings — as written. The result
    is validated as a role-profiles file before anything is written."""
    import tomllib

    from memtrace_harness.role_profiles import _validate_provider_effort, load_role_profiles

    entries = _check_chain(chain)
    path = config.role_profiles_file_for()
    if path is None:
        raise ValueError(
            "this workspace has no roles file of its own yet (it uses the packaged default, shared "
            "by every Harness) — give it one first with the 獨立出去 button or "
            "`memtrace-harness roles-independent`"
        )
    text = path.read_text(encoding="utf-8")
    try:
        table = _profile_table(text, profile_id)
    except ValueError:
        raise ValueError(f"profile {profile_id!r} not found in {path}") from None
    header = f"[{table}]"
    start = text.find(header)
    body_start = start + len(header)
    fb_header = f"[[{table}.fallbacks]]"
    # This role's section = from its header up to the next table that is not its own fallbacks.
    end = len(text)
    for m in re.finditer(r"(?m)^\[+[^\]]+\]+", text[body_start:]):
        if m.group(0) != fb_header:
            end = body_start + m.start()
            break
    section = text[body_start:end]
    fb_at = section.find(fb_header)
    primary = section if fb_at == -1 else section[:fb_at]
    old: dict = tomllib.loads(text)
    for part in table.split("."):
        old = old.get(part, {})
    old_fallbacks = old.get("fallbacks", [])
    old_effort = old

    first = entries[0]
    effort = first["reasoning_effort"] or str(old_effort.get("reasoning_effort") or "medium")
    _validate_provider_effort(profile_id, first["provider"], effort)
    for key, value in (("provider", first["provider"]), ("model", first["model"]), ("reasoning_effort", effort)):
        primary, n = re.subn(rf'(?m)^{key}\s*=\s*"[^"]*"', f'{key} = "{value}"', primary, count=1)
        if not n:
            raise ValueError(f"profile {profile_id!r} has no {key} line to update")
    # The primary's quota bucket follows its provider unless it names something unrelated.
    primary = re.sub(
        r'(?m)^quota_bucket\s*=\s*"[^"]*"',
        f'quota_bucket = "{first["provider"]}-account"',
        primary,
        count=1,
    )
    fallbacks = entries[1:]
    hops = len(fallbacks)
    if re.search(r"(?m)^max_fallback_hops\s*=", primary):
        primary = re.sub(r"(?m)^max_fallback_hops\s*=.*$", f"max_fallback_hops = {hops}", primary, count=1)
    elif hops:
        primary = primary.rstrip("\n") + f"\nmax_fallback_hops = {hops}\n"
    if hops and not re.search(r"(?m)^fallback_on\s*=", primary):
        primary = primary.rstrip("\n") + '\nfallback_on = ["quota_exhausted", "rate_limit", "provider_overloaded"]\n'
    blocks = ""
    for index, fb in enumerate(fallbacks):
        keep = next((o for o in old_fallbacks if o.get("provider") == fb["provider"]), {})
        fb_effort = fb["reasoning_effort"] or str(keep.get("reasoning_effort") or "medium")
        _validate_provider_effort(f"{profile_id}.fallbacks[{index}]", fb["provider"], fb_effort)
        blocks += (
            f'\n{fb_header}\nprovider = "{fb["provider"]}"\nmodel = "{fb["model"]}"\n'
            f'reasoning_effort = "{fb_effort}"\nquota_bucket = "{fb["provider"]}-account"\n'
        )
    primary = primary.rstrip("\n") + "\n"
    candidate = text[:body_start] + primary + blocks + (("\n" + text[end:]) if end < len(text) else "")
    # Validate the whole file as the loader would before replacing the live one.
    check = path.with_name(path.name + ".check")
    check.write_text(candidate, encoding="utf-8")
    try:
        load_role_profiles(check)
    finally:
        check.unlink(missing_ok=True)
    path.write_text(candidate, encoding="utf-8")
    _remember(config, entries)


def refresh_model_catalog() -> None:
    from memtrace_harness.model_catalog import clear_cache

    clear_cache()


def create_dedicated_role_profiles_file(config: HarnessConfig) -> Path:
    """Give this workspace its own editable `.harness/roles.toml`, copied from the packaged
    default — the status page's "獨立出去" button. Takes effect at once (the role profiles file is
    read each time a task starts)."""
    if config.role_profiles_file_for() is not None:
        raise ValueError("this workspace already has its own role-profiles file")
    root = config.workspace_root
    if root is None:
        raise ValueError("this Harness was not started from a workspace")
    dest = root / STATE_DIR_NAME / "roles.toml"
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not dest.is_file():
        default_toml = Path(__file__).with_name("default-role-profiles.toml")
        dest.write_text(default_toml.read_text(encoding="utf-8"), encoding="utf-8")
    os.environ["HARNESS_ROLE_PROFILES_FILE"] = str(dest)  # `.harness/roles.toml` is found again at every start
    return dest


def render_status_text(data: dict) -> str:
    """Render collect_status_data()'s structure as the plain-text report `status` prints."""
    lines: list[str] = []
    harness = data["harness"]
    lines.append("=== Harness ===")
    lines.append(f"  pid {harness['pid']}，工作區 {harness['workspace_dir']}，API 埠 {harness['api_port']}")
    if harness.get("started_at"):
        lines.append(f"  啟動時間 {harness['started_at']}")
    lines.append(f"  尚未被服務取走的訊息：{harness['outbox_backlog']} 則")

    for project in data["projects"]:
        lines.append(f"\n  【{project['name']}】 workspace_id={project['workspace_id']}")
        lines.append(f"    working_directory: {project['working_directory']}")
        if len(project.get("repos", [])) > 1:
            lines.append("    repos：")
            for repo in project["repos"]:
                mark = "（主要）" if repo["primary"] else ""
                missing = "" if repo["exists"] else "  ⚠️ 資料夾不存在"
                lines.append(f"      - {repo['name']}{mark}：{repo['path']}{missing}")
        dedicated = project["dedicated"]
        lines.append(
            "    設定："
            f"chat model={'已設定' if dedicated['chat_model'] else '用預設'}, "
            f"memory workspace={'已設定' if dedicated['memory'] else '同專案工作區'}, "
            f"role-profiles={'自己的檔案' if dedicated['role_profiles'] else '用預設'}"
        )

        if project["chat_candidates"]:
            chat_line = " -> ".join(
                f"{c['provider']}/{c['model'] or '(預設)'}" for c in project["chat_candidates"]
            )
            lines.append(f"    聊天模型：{chat_line}")
        else:
            lines.append("    聊天模型：（未設定 HARNESS_CHAT_PROVIDER）")

        if project["role_profiles_error"]:
            lines.append(f"    ⚠️  role-profiles 讀取失敗：{project['role_profiles_error']}")
        else:
            lines.append("    Agent Loop 角色模型：")
            for profile in project["role_profiles"] or []:
                entry = f"      {profile['id']}: {profile['provider']}/{profile['model']}"
                if profile["fallbacks"]:
                    fb = ", ".join(f"{f['provider']}/{f['model']}" for f in profile["fallbacks"])
                    entry += f"（fallback: {fb}）"
                lines.append(entry)

        lines.append(
            f"    對話記錄：{project['turn_count']} 則"
            f"（目標分類待處理 {project['pending_goal']}、每日摘要 {len(project['digests'])} 天）"
        )
        if project["recent_turns"]:
            lines.append("    最近對話：")
            for turn in project["recent_turns"]:
                content_preview = turn["content"].replace("\n", " ")[:80]
                lines.append(f"      [{turn['created_at']}] {turn['speaker']}: {content_preview}")
        if project["locks"]:
            lines.append(f"    ⚠️  執行中 {len(project['locks'])}/{project['max_workers']} 個任務位：")
        for lock in project["locks"]:
            lines.append(f"    ⚠️  conversation_id={lock['conversation_id']}, locked_at={lock['locked_at']}")
            turn = lock["latest_turn"]
            if turn:
                lines.append(
                    f"       最新進度：{turn['stage']}/{turn['profile_id']} "
                    f"({turn['provider']}/{turn['model'] or '?'}) state={turn['state']} @ {turn['created_at']}"
                )
            else:
                lines.append("       最新進度：尚未有階段完成（可能剛啟動或卡在第一個階段）")
        if project["queue"]:
            lines.append(f"    ⏳ 佇列中 {len(project['queue'])} 個任務：")
            for queued in project["queue"]:
                lines.append(f"       - {queued['goal']}")
        if project["pending_approvals"]:
            lines.append(f"    ⚠️  待核准請求 {len(project['pending_approvals'])} 筆：")
            for req in project["pending_approvals"]:
                lines.append(f"       {req['id']} — {req['reason']} — {req['proposed_action'][:60]}")
        if project["pipeline"]:
            lines.append(f"    Agent Loop 進度（conversation_id={project['pipeline_conversation_id']}）：")
            for stage in project["pipeline"]:
                retry = f" (重試 #{stage['attempt_index']})" if stage["attempt_index"] else ""
                lines.append(
                    f"       {stage['stage']}/{stage['profile_id']}{retry} "
                    f"({stage['provider']}/{stage['model'] or '?'}) state={stage['state']} @ {stage['created_at']}"
                )

    return "\n".join(lines)

    return "\n".join(lines)


def collect_status_text(config: HarnessConfig, scope: ProjectScope) -> str:
    return render_status_text(collect_status_data(config, scope))


def collect_turn_detail(config: HarnessConfig, turn_id: int) -> dict | None:
    """One Agent Loop stage attempt's full detail (artifact + model's final text) for the status
    page's on-demand "click a stage" view — deliberately not part of collect_status_data(), since
    artifact/final_text bodies can be large and most stages in a pipeline are never clicked."""
    return TraceStore(config.trace_db_path).get_turn_detail(turn_id)
