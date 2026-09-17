from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class HarnessConfig:
    memtrace_mcp_url: str | None
    memtrace_api_token: str | None
    trace_db_path: Path
    trace_root: Path
    claude_command: str
    codex_command: str
    antigravity_command: str
    antigravity_output_mode: str
    cli_timeout_seconds: int
    telegram_bot_token: str | None
    telegram_allowed_chat_ids: set[int]
    project_index_path: Path | None
    chat_provider: str
    chat_model: str
    unattended_write_requires_approval: bool
    harness_memory_workspace_id: str | None = None
    chat_fallbacks: tuple[tuple[str, str], ...] = ()
    operator_preference_workspace_id: str | None = None
    status_server_enabled: bool = True
    status_server_host: str = "127.0.0.1"
    status_server_port: int = 8787
    shutdown_grace_seconds: int = 120
    reply_language: str | None = "Traditional Chinese (繁體中文，台灣用語與正體字)"
    schedule_timezone: str = "Asia/Taipei"

    def command_for(self, provider: str) -> str:
        if provider == "claude":
            return self.claude_command
        if provider == "codex":
            return self.codex_command
        if provider == "antigravity":
            return self.antigravity_command
        return provider

    def telegram_bot_token_for(self, project_name: str) -> str | None:
        """Per-project bot token, read from the Harness's own .env — never from the
        target project's repo, so a project's harness-scope.md never has to carry a
        secret. Falls back to the shared HARNESS_TELEGRAM_BOT_TOKEN bot."""
        return os.getenv(project_bot_token_env_var(project_name)) or self.telegram_bot_token

    def chat_candidates_for(self, project_name: str) -> tuple[tuple[str, str], ...]:
        """Ordered (provider, model) list for that project's quick-chat-reply model:
        that project's own HARNESS_CHAT_PROVIDER_<PROJECT>/_MODEL_<PROJECT>/_FALLBACKS_<PROJECT>
        if set, else the shared HARNESS_CHAT_PROVIDER/MODEL/FALLBACKS. Chat is cheap and
        low-stakes (no repo writes), so unlike Agent Loop roles it's fine for a project
        to just pick whichever model is good enough and cheapest — no vendor lock."""
        provider = os.getenv(project_chat_provider_env_var(project_name)) or self.chat_provider
        model = os.getenv(project_chat_model_env_var(project_name), self.chat_model)
        fallbacks_override = os.getenv(project_chat_fallbacks_env_var(project_name))
        fallbacks = (
            _parse_chat_fallbacks(fallbacks_override)
            if fallbacks_override is not None
            else self.chat_fallbacks
        )
        if not provider:
            return ()
        return ((provider, model), *fallbacks)

    def role_profiles_file_for(self, project_name: str) -> Path | None:
        """Per-project role-profile override (e.g. a different fallback order), read
        from the Harness's own env — not from harness-scope.md, so the target project's
        repo never carries Harness-internal routing policy. None means: use the
        packaged default-role-profiles.toml."""
        override = os.getenv(project_role_profiles_env_var(project_name))
        return Path(override) if override else None

    @classmethod
    def from_env(cls) -> "HarnessConfig":
        trace_path = os.getenv("HARNESS_TRACE_DB", "data/harness.sqlite3")
        trace_root = os.getenv("HARNESS_TRACE_ROOT", "data/traces")
        allowed_chat_ids_str = os.getenv("HARNESS_TELEGRAM_ALLOWED_CHAT_IDS", "")
        allowed_chat_ids = {
            int(cid.strip())
            for cid in allowed_chat_ids_str.split(",")
            if cid.strip().isdigit() or (cid.strip().startswith("-") and cid.strip()[1:].isdigit())
        }
        project_index = os.getenv("HARNESS_PROJECT_INDEX")
        unattended_approval_str = os.getenv("HARNESS_UNATTENDED_WRITE_REQUIRES_APPROVAL", "true").lower()
        return cls(
            memtrace_mcp_url=os.getenv("MEMTRACE_MCP_URL"),
            memtrace_api_token=os.getenv("MEMTRACE_API_TOKEN"),
            trace_db_path=Path(trace_path),
            trace_root=Path(trace_root),
            claude_command=os.getenv("HARNESS_CLAUDE_COMMAND", "claude"),
            codex_command=os.getenv("HARNESS_CODEX_COMMAND", "codex"),
            antigravity_command=os.getenv("HARNESS_ANTIGRAVITY_COMMAND", "agy"),
            antigravity_output_mode=_antigravity_output_mode(
                os.getenv("HARNESS_ANTIGRAVITY_OUTPUT_MODE", "auto")
            ),
            cli_timeout_seconds=_positive_int(os.getenv("HARNESS_CLI_TIMEOUT_SECONDS"), 900),
            telegram_bot_token=os.getenv("HARNESS_TELEGRAM_BOT_TOKEN"),
            telegram_allowed_chat_ids=allowed_chat_ids,
            project_index_path=Path(project_index) if project_index else None,
            chat_provider=os.getenv("HARNESS_CHAT_PROVIDER", "claude"),
            chat_model=os.getenv("HARNESS_CHAT_MODEL", "haiku"),
            unattended_write_requires_approval=unattended_approval_str not in {"false", "0", "no"},
            harness_memory_workspace_id=os.getenv("HARNESS_MEMORY_WORKSPACE_ID"),
            chat_fallbacks=_parse_chat_fallbacks(os.getenv("HARNESS_CHAT_FALLBACKS", "")),
            operator_preference_workspace_id=os.getenv("HARNESS_OPERATOR_PREFERENCE_WORKSPACE_ID"),
            status_server_enabled=os.getenv("HARNESS_STATUS_SERVER_ENABLED", "true").lower()
            not in {"false", "0", "no"},
            status_server_host=os.getenv("HARNESS_STATUS_SERVER_HOST", "127.0.0.1"),
            status_server_port=_positive_int(os.getenv("HARNESS_STATUS_SERVER_PORT"), 8787),
            shutdown_grace_seconds=_positive_int(os.getenv("HARNESS_SHUTDOWN_GRACE_SECONDS"), 120),
            reply_language=(
                os.getenv("HARNESS_REPLY_LANGUAGE")
                if "HARNESS_REPLY_LANGUAGE" in os.environ
                else "Traditional Chinese (繁體中文，台灣用語與正體字)"
            )
            or None,
            schedule_timezone=os.getenv("HARNESS_SCHEDULE_TIMEZONE", "Asia/Taipei"),
        )

    def memory_workspace_id_for(self, project_name: str, project_workspace_id: str) -> str:
        """Cold-memory consolidation target: a dedicated memory workspace (runtime/
        conversation memory — what happened) is intentionally separate from a project's
        own spec/planning workspace (what should be built). Resolution order:
        1. HARNESS_MEMORY_WORKSPACE_ID_<PROJECT> — that project's own dedicated memory KB
        2. HARNESS_MEMORY_WORKSPACE_ID — one shared memory KB across every project
        3. the project's own spec workspace, if neither is set
        Same per-project-override-over-shared-default shape as telegram_bot_token_for()
        and role_profiles_file_for() — adding a project with its own memory KB is a
        one-line .env addition, never a code change."""
        return (
            os.getenv(project_memory_workspace_env_var(project_name))
            or self.harness_memory_workspace_id
            or project_workspace_id
        )


def _parse_chat_fallbacks(value: str) -> tuple[tuple[str, str], ...]:
    """HARNESS_CHAT_FALLBACKS=provider/model,provider/model — an ordered fallback list
    for the quick-chat-reply model, same shape as a role profile's fallback chain
    (docs/remote-ops-plan.md §5: "the chat model gets its own ordered fallback list")."""
    pairs = []
    for item in value.split(","):
        item = item.strip()
        if not item:
            continue
        if "/" not in item:
            raise ValueError(f"HARNESS_CHAT_FALLBACKS entry must be provider/model, got: {item!r}")
        provider, model = item.split("/", 1)
        pairs.append((provider.strip(), model.strip()))
    return tuple(pairs)


def project_bot_token_env_var(project_name: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "_", project_name).strip("_").upper()
    return f"HARNESS_TELEGRAM_BOT_TOKEN_{slug}"


def project_role_profiles_env_var(project_name: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "_", project_name).strip("_").upper()
    return f"HARNESS_ROLE_PROFILES_FILE_{slug}"


def project_memory_workspace_env_var(project_name: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "_", project_name).strip("_").upper()
    return f"HARNESS_MEMORY_WORKSPACE_ID_{slug}"


def project_chat_provider_env_var(project_name: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "_", project_name).strip("_").upper()
    return f"HARNESS_CHAT_PROVIDER_{slug}"


def project_chat_model_env_var(project_name: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "_", project_name).strip("_").upper()
    return f"HARNESS_CHAT_MODEL_{slug}"


def project_chat_fallbacks_env_var(project_name: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "_", project_name).strip("_").upper()
    return f"HARNESS_CHAT_FALLBACKS_{slug}"


def _positive_int(value: str | None, default: int) -> int:
    if value is None:
        return default
    parsed = int(value)
    if parsed <= 0:
        raise ValueError("expected a positive integer, got: " + value)
    return parsed


def _antigravity_output_mode(value: str) -> str:
    normalized = value.strip().lower()
    if normalized not in {"auto", "text", "stream-json"}:
        raise ValueError(
            "HARNESS_ANTIGRAVITY_OUTPUT_MODE must be auto, text, or stream-json"
        )
    return normalized
