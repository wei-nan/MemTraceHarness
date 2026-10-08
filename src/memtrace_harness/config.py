from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from memtrace_harness.taiwantrade_mcp import mcp_server_spec


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
    # Nightly memory digest model, independent of the chat model: the digest is a
    # once-a-day consolidation that decides what becomes long-term memory, so it can
    # afford a stronger model than the quick chat reply. Unset -> chat candidates.
    digest_provider: str | None = None
    digest_model: str | None = None
    digest_fallbacks: tuple[tuple[str, str], ...] = ()
    # Topic recall (the slow path behind chat: topic judgement + cold-memory exploration
    # -> short-lived briefs, topic_recall.py). Unset -> the digest chain, so it gets the
    # stronger model without any extra setting.
    recall_enabled: bool = True
    recall_provider: str | None = None
    recall_model: str | None = None
    recall_fallbacks: tuple[tuple[str, str], ...] = ()
    recall_ttl_hours: int = 72
    operator_preference_workspace_id: str | None = None
    status_server_enabled: bool = True
    status_server_host: str = "127.0.0.1"
    status_server_port: int = 8787
    shutdown_grace_seconds: int = 120
    reply_language: str | None = "Traditional Chinese (繁體中文，台灣用語與正體字)"
    schedule_timezone: str = "Asia/Taipei"
    # A pending approval nobody answered for this long is closed automatically so an
    # old message's buttons can't be tapped later and resume a task the human has since
    # forgotten (2026-09-30: a day-old backtest approval was tapped thinking it was
    # something else). 0 disables.
    approval_ttl_hours: int = 12

    def chat_command(
        self,
        provider: str,
        model: str | None,
        prompt: str,
        *,
        claude_allowed_tools: str | None = None,
        taiwantrade: bool = False,
        order_project: str | None = None,
        ops_server: dict | None = None,
    ) -> list[str]:
        """argv for one plain, non-interactive "answer this prompt" call — the quick
        chat reply, the chat classifiers, the nightly digest, JSON repair. Claude and
        Antigravity take `--print <prompt>`; Codex has no --print at all (it exits 2
        with a usage error), so until 2026-10-02 picking Codex as a chat model silently
        failed on every message and fell back. Codex answers through `codex exec`,
        read-only and ephemeral, printing only the final message on stdout."""
        command = [self.command_for(provider)]
        # The read-only TaiwanTrade proxy (taiwantrade_mcp.py) is an MCP server, which the
        # CLI spawns outside its tool sandbox — the only way a sandboxed chat model can
        # reach 127.0.0.1:8000. Opt-in, and only for the chat reply (taiwantrade=True):
        # digests, classifiers and JSON repair have no use for trading data.
        # order_project (the chat's project) also lets the model *propose* orders, if the
        # operator allowed that project; the human still has to confirm in Telegram.
        proxy = (
            mcp_server_spec(order_project=order_project, trace_db_path=self.trace_db_path)
            if taiwantrade
            else None
        )
        if provider == "codex":
            command += ["exec", "--skip-git-repo-check", "--ephemeral", "--sandbox", "read-only", "--color", "never"]
            if proxy:
                env_toml = ",".join(f"{k}={json.dumps(v)}" for k, v in proxy["env"].items())
                command += [
                    "--config", f"mcp_servers.taiwantrade.command={json.dumps(proxy['command'])}",
                    "--config", f"mcp_servers.taiwantrade.args={json.dumps(proxy['args'])}",
                    "--config", f"mcp_servers.taiwantrade.env={{{env_toml}}}",
                    # `codex exec` runs with approval=never, so an MCP tool call that needs
                    # approval simply fails ("approval required but unavailable"). Every
                    # tool of this proxy is a read-only GET, so approve them up front.
                    "--config", 'mcp_servers.taiwantrade.default_tools_approval_mode="approve"',
                ]
            if ops_server:
                env_toml = ",".join(f"{k}={json.dumps(v)}" for k, v in ops_server["env"].items())
                command += [
                    "--config", f"mcp_servers.ops.command={json.dumps(ops_server['command'])}",
                    "--config", f"mcp_servers.ops.args={json.dumps(ops_server['args'])}",
                    "--config", f"mcp_servers.ops.env={{{env_toml}}}",
                    # Safe to approve up front: run_job only records a request the human must
                    # confirm in Telegram; the other tools read.
                    "--config", 'mcp_servers.ops.default_tools_approval_mode="approve"',
                ]
            if model:
                command += ["--model", model]
            return command + [prompt]
        if provider == "claude" and proxy:
            command += ["--mcp-config", json.dumps({"mcpServers": {"taiwantrade": proxy}})]
            claude_allowed_tools = ",".join(
                filter(None, [claude_allowed_tools, "mcp__taiwantrade"])
            )
        if provider == "claude" and ops_server:
            # --mcp-config may be given more than once; the taiwantrade one above is separate.
            command += ["--mcp-config", json.dumps({"mcpServers": {"ops": ops_server}})]
            claude_allowed_tools = ",".join(filter(None, [claude_allowed_tools, "mcp__ops"]))
        if provider == "claude" and claude_allowed_tools:
            command += ["--allowedTools", claude_allowed_tools]
        if model:
            command += ["--model", model]
        return command + ["--print", prompt]

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

    def digest_candidates_for(self, project_name: str) -> tuple[tuple[str, str], ...]:
        """Ordered (provider, model) list for that project's nightly memory digest:
        HARNESS_DIGEST_PROVIDER_<PROJECT>/_MODEL_<PROJECT>/_FALLBACKS_<PROJECT> if set,
        else the shared HARNESS_DIGEST_PROVIDER/MODEL/FALLBACKS. When no digest provider
        is configured at either level it falls back to chat_candidates_for(), the
        behavior before the digest got its own model. A configured digest chain is used
        as-is — it never silently degrades into the (weaker) chat chain."""
        provider = os.getenv(project_digest_provider_env_var(project_name)) or self.digest_provider
        if not provider:
            return self.chat_candidates_for(project_name)
        model = os.getenv(project_digest_model_env_var(project_name)) or self.digest_model
        fallbacks_override = os.getenv(project_digest_fallbacks_env_var(project_name))
        fallbacks = (
            _parse_chat_fallbacks(fallbacks_override)
            if fallbacks_override is not None
            else self.digest_fallbacks
        )
        return ((provider, model or ""), *fallbacks)

    def recall_candidates_for(self, project_name: str) -> tuple[tuple[str, str], ...]:
        """Ordered (provider, model) list for that project's topic recall:
        HARNESS_RECALL_PROVIDER_<PROJECT>/_MODEL_<PROJECT>/_FALLBACKS_<PROJECT> if set,
        else the shared HARNESS_RECALL_*; with none configured at all it uses the digest
        chain (itself falling back to chat). Same rule as digest_candidates_for(): a
        configured chain is used as-is and never degrades into a weaker one."""
        provider = os.getenv(project_recall_provider_env_var(project_name)) or self.recall_provider
        if not provider:
            return self.digest_candidates_for(project_name)
        model = os.getenv(project_recall_model_env_var(project_name)) or self.recall_model
        fallbacks_override = os.getenv(project_recall_fallbacks_env_var(project_name))
        fallbacks = (
            _parse_chat_fallbacks(fallbacks_override)
            if fallbacks_override is not None
            else self.recall_fallbacks
        )
        return ((provider, model or ""), *fallbacks)

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
            digest_provider=os.getenv("HARNESS_DIGEST_PROVIDER") or None,
            digest_model=os.getenv("HARNESS_DIGEST_MODEL") or None,
            digest_fallbacks=_parse_chat_fallbacks(os.getenv("HARNESS_DIGEST_FALLBACKS", "")),
            recall_enabled=os.getenv("HARNESS_RECALL_ENABLED", "true").lower()
            not in {"false", "0", "no"},
            recall_provider=os.getenv("HARNESS_RECALL_PROVIDER") or None,
            recall_model=os.getenv("HARNESS_RECALL_MODEL") or None,
            recall_fallbacks=_parse_chat_fallbacks(os.getenv("HARNESS_RECALL_FALLBACKS", "")),
            recall_ttl_hours=_positive_int(os.getenv("HARNESS_RECALL_TTL_HOURS"), 72),
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
            approval_ttl_hours=_non_negative_int(os.getenv("HARNESS_APPROVAL_TTL_HOURS"), 12),
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


def _non_negative_int(value: str | None, default: int) -> int:
    try:
        parsed = int(value) if value is not None and value.strip() else default
    except ValueError:
        return default
    return parsed if parsed >= 0 else default


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


def project_digest_provider_env_var(project_name: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "_", project_name).strip("_").upper()
    return f"HARNESS_DIGEST_PROVIDER_{slug}"


def project_digest_model_env_var(project_name: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "_", project_name).strip("_").upper()
    return f"HARNESS_DIGEST_MODEL_{slug}"


def project_digest_fallbacks_env_var(project_name: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "_", project_name).strip("_").upper()
    return f"HARNESS_DIGEST_FALLBACKS_{slug}"


def project_recall_provider_env_var(project_name: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "_", project_name).strip("_").upper()
    return f"HARNESS_RECALL_PROVIDER_{slug}"


def project_recall_model_env_var(project_name: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "_", project_name).strip("_").upper()
    return f"HARNESS_RECALL_MODEL_{slug}"


def project_recall_fallbacks_env_var(project_name: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "_", project_name).strip("_").upper()
    return f"HARNESS_RECALL_FALLBACKS_{slug}"


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
