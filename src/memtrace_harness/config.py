from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from memtrace_harness.taiwantrade_mcp import mcp_server_spec
from memtrace_harness.workspace import STATE_DIR_NAME


@dataclass(frozen=True)
class HarnessConfig:
    """Everything one Harness needs, read from the process environment that `workspace.load_workspace()`
    fills from the workspace's `.harness/models.toml` (preferences) and `.harness.env` (environment).

    A Harness is one workspace: one project, one set of models, one MemTrace workspace. There is
    nothing Telegram- or web-shaped in here — those are services (memtrace_services/) with their
    own environment files, and they only reach the Harness through its local HTTP API (api.py).
    """

    memtrace_mcp_url: str | None
    memtrace_api_token: str | None
    trace_db_path: Path
    trace_root: Path
    claude_command: str
    codex_command: str
    antigravity_command: str
    antigravity_output_mode: str
    cli_timeout_seconds: int
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
    # Memory archive (the nightly split of a day into matters, the weekly matter review, rebuilds):
    # its own model, since how well a day is split decides what can be found later. Unset -> the
    # digest chain.
    archive_provider: str | None = None
    archive_model: str | None = None
    archive_fallbacks: tuple[tuple[str, str], ...] = ()
    # Nightly audit (memory_audit.py): an independent model that judges how the day went. Unset ->
    # the digest chain; setting a different vendor makes the auditor more independent of what it audits.
    audit_provider: str | None = None
    audit_model: str | None = None
    audit_fallbacks: tuple[tuple[str, str], ...] = ()
    # Topic recall (the slow path behind chat: topic judgement + cold-memory exploration
    # -> short-lived briefs, topic_recall.py). Unset -> the digest chain, so it gets the
    # stronger model without any extra setting.
    recall_enabled: bool = True
    recall_provider: str | None = None
    recall_model: str | None = None
    recall_fallbacks: tuple[tuple[str, str], ...] = ()
    recall_ttl_hours: int = 72
    operator_preference_workspace_id: str | None = None
    # The Harness's local HTTP API, the only door services use (api.py). Loopback by default; the
    # token is what stops any other local process from posting as the operator.
    api_host: str = "127.0.0.1"
    api_port: int = 8800
    api_token: str | None = None
    # A second, narrower token for the A2A service (memtrace_services/a2a): it opens only /v1/peer/*, never
    # /v1/actions or /v1/admin/*, so a leaked peer path cannot approve or change anything (docs/a2a-peers.md).
    peer_api_token: str | None = None
    peer_max_concurrent: int = 2
    peer_description: str | None = None
    peer_name: str | None = None
    # The workspace this Harness is (None only for legacy/one-off CLI use without a workspace).
    workspace_root: Path | None = None
    role_profiles_file: Path | None = None
    shutdown_grace_seconds: int = 120
    reply_language: str | None = "Traditional Chinese (繁體中文，台灣用語與正體字)"
    schedule_timezone: str = "Asia/Taipei"
    # A pending approval nobody answered for this long is closed automatically so an
    # old message's buttons can't be tapped later and resume a task the human has since
    # forgotten (2026-09-30: a day-old backtest approval was tapped thinking it was
    # something else). 0 disables.
    approval_ttl_hours: int = 12
    # Command template that turns a voice / audio / video file into text (attachments.py); {input} and {outdir} are
    # filled in. None -> the whisper CLI, when a model is already downloaded.
    transcribe_command: str | None = None
    cursor_command: str = "cursor-agent"

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
        memtrace_read: bool = False,
        images: list[str] | None = None,
        read_dirs: list[str] | None = None,
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
        # operator allowed that project; the human still has to confirm in the chat.
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
            if memtrace_read and self.memtrace_mcp_url:
                # Until 2026-10-09 only the Agent Loop's Codex adapter got this; a Codex chat
                # model (own CODEX_HOME, no MCP servers) therefore had no MemTrace at all and
                # told the operator so. Read-only here; writes go through the Harness
                # (HARNESS_KB_NOTE::), never a model-held write tool.
                from memtrace_harness.adapters.codex import MEMTRACE_READ_TOOLS, MEMTRACE_TOKEN_ENV

                prefix = "mcp_servers.memtrace"
                command += [
                    "--config", f"{prefix}.url={json.dumps(self.memtrace_mcp_url)}",
                    "--config", f"{prefix}.enabled_tools={json.dumps(MEMTRACE_READ_TOOLS)}",
                    "--config", f'{prefix}.default_tools_approval_mode="approve"',
                ]
                if os.getenv(MEMTRACE_TOKEN_ENV):
                    command += ["--config", f'{prefix}.bearer_token_env_var="{MEMTRACE_TOKEN_ENV}"']
            if ops_server:
                env_toml = ",".join(f"{k}={json.dumps(v)}" for k, v in ops_server["env"].items())
                command += [
                    "--config", f"mcp_servers.ops.command={json.dumps(ops_server['command'])}",
                    "--config", f"mcp_servers.ops.args={json.dumps(ops_server['args'])}",
                    "--config", f"mcp_servers.ops.env={{{env_toml}}}",
                    # Safe to approve up front: run_job only records a request the human must
                    # confirm in the chat; the other tools read.
                    "--config", 'mcp_servers.ops.default_tools_approval_mode="approve"',
                ]
            if model:
                command += ["--model", model]
            if images:
                # `-i` takes any number of files, so `--` is what ends them and starts the prompt.
                command += ["-i", *images, "--"]
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
        if provider == "claude":
            # Installed skills (~/.claude/skills) are loaded by the CLI but, headless, nobody can approve the Skill tool.
            claude_allowed_tools = ",".join(filter(None, [claude_allowed_tools, "Skill"]))
        if provider == "claude" and read_dirs:
            # Files a person sent (attachments.py) live in these folders; Claude reads them with its own Read tool.
            claude_allowed_tools = ",".join(
                filter(None, [claude_allowed_tools, *(f"Read(/{Path(d).resolve()}/**)" for d in read_dirs)])
            )
        if provider == "claude" and claude_allowed_tools:
            command += ["--allowedTools", claude_allowed_tools]
        if provider == "cursor":
            # Plain answer call: read-only `ask` mode, workspace trusted so it never stops to ask.
            command += ["--print", "--mode", "ask", "--trust"]
            if model:
                command += ["--model", model]
            return command + [prompt]
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
        if provider == "cursor":
            return self.cursor_command
        return provider

    # The `project_name` parameters below are kept so callers written for the old
    # one-process-many-projects Harness read the same; a workspace has exactly one project, so
    # the name no longer selects anything. The model settings are read from the environment at
    # call time (falling back to what was loaded at startup) so that changing one — from the status
    # page, through the Harness API — takes effect on the next call without a restart.

    def peer_description_now(self) -> str:
        """What this Harness says about itself to its peers. Read at call time, like the model settings, so an edit on the
        status page shows up in the next Agent Card without a restart."""
        return os.getenv("HARNESS_PEER_DESCRIPTION", self.peer_description or "").strip()

    def chat_candidates_for(self, project_name: str | None = None) -> tuple[tuple[str, str], ...]:
        """Ordered (provider, model) list for the quick chat reply. Chat is cheap and
        low-stakes (no repo writes), so unlike Agent Loop roles a workspace just picks whichever
        model is good enough and cheapest — no vendor lock."""
        provider = os.getenv("HARNESS_CHAT_PROVIDER") or self.chat_provider
        model = os.getenv("HARNESS_CHAT_MODEL", self.chat_model)
        fallbacks_override = os.getenv("HARNESS_CHAT_FALLBACKS")
        fallbacks = (
            _parse_chat_fallbacks(fallbacks_override)
            if fallbacks_override is not None
            else self.chat_fallbacks
        )
        if not provider:
            return ()
        return ((provider, model), *fallbacks)

    def digest_candidates_for(self, project_name: str | None = None) -> tuple[tuple[str, str], ...]:
        """Ordered (provider, model) list for the nightly memory digest. When no digest provider
        is configured it falls back to chat_candidates_for(), the behavior before the digest got
        its own model. A configured digest chain is used as-is — it never silently degrades into
        the (weaker) chat chain."""
        provider = os.getenv("HARNESS_DIGEST_PROVIDER") or self.digest_provider
        if not provider:
            return self.chat_candidates_for()
        model = os.getenv("HARNESS_DIGEST_MODEL") or self.digest_model
        fallbacks_override = os.getenv("HARNESS_DIGEST_FALLBACKS")
        fallbacks = (
            _parse_chat_fallbacks(fallbacks_override)
            if fallbacks_override is not None
            else self.digest_fallbacks
        )
        return ((provider, model or ""), *fallbacks)

    def archive_candidates_for(self, project_name: str | None = None) -> tuple[tuple[str, str], ...]:
        """Ordered (provider, model) list for the memory archive work (matters, review, rebuild).
        With none configured it uses the digest chain (itself falling back to chat). A configured
        chain is used as-is and never degrades into a weaker one."""
        provider = os.getenv("HARNESS_ARCHIVE_PROVIDER") or self.archive_provider
        if not provider:
            return self.digest_candidates_for(project_name)
        model = os.getenv("HARNESS_ARCHIVE_MODEL") or self.archive_model
        fallbacks_override = os.getenv("HARNESS_ARCHIVE_FALLBACKS")
        fallbacks = (
            _parse_chat_fallbacks(fallbacks_override)
            if fallbacks_override is not None
            else self.archive_fallbacks
        )
        return ((provider, model or ""), *fallbacks)

    def audit_candidates_for(self, project_name: str | None = None) -> tuple[tuple[str, str], ...]:
        """Ordered (provider, model) list for the nightly audit; with none configured it uses the
        digest chain. A configured chain is used as-is and never degrades into a weaker one."""
        provider = os.getenv("HARNESS_AUDIT_PROVIDER") or self.audit_provider
        if not provider:
            return self.digest_candidates_for(project_name)
        model = os.getenv("HARNESS_AUDIT_MODEL") or self.audit_model
        fallbacks_override = os.getenv("HARNESS_AUDIT_FALLBACKS")
        fallbacks = (
            _parse_chat_fallbacks(fallbacks_override)
            if fallbacks_override is not None
            else self.audit_fallbacks
        )
        return ((provider, model or ""), *fallbacks)

    def recall_candidates_for(self, project_name: str | None = None) -> tuple[tuple[str, str], ...]:
        """Ordered (provider, model) list for topic recall; with none configured it uses the
        digest chain (itself falling back to chat). A configured chain is used as-is and never
        degrades into a weaker one."""
        provider = os.getenv("HARNESS_RECALL_PROVIDER") or self.recall_provider
        if not provider:
            return self.digest_candidates_for()
        model = os.getenv("HARNESS_RECALL_MODEL") or self.recall_model
        fallbacks_override = os.getenv("HARNESS_RECALL_FALLBACKS")
        fallbacks = (
            _parse_chat_fallbacks(fallbacks_override)
            if fallbacks_override is not None
            else self.recall_fallbacks
        )
        return ((provider, model or ""), *fallbacks)

    def role_profiles_file_for(self, project_name: str | None = None) -> Path | None:
        """This workspace's role-profile override (e.g. a different fallback order), kept in
        its own `.harness/roles.toml` — not in harness-scope.md, so the target project's repo never
        carries Harness-internal routing policy. None means: the packaged default-role-profiles.toml."""
        override = os.getenv("HARNESS_ROLE_PROFILES_FILE")
        return Path(override) if override else self.role_profiles_file

    def memory_workspace_id_for(self, project_name: str | None, project_workspace_id: str) -> str:
        """Cold-memory consolidation target: a dedicated memory workspace (runtime/conversation
        memory — what happened) is intentionally separate from the project's own spec/planning
        workspace (what should be built). HARNESS_MEMORY_WORKSPACE_ID if set, else the project's
        own spec workspace."""
        return (
            os.getenv("HARNESS_MEMORY_WORKSPACE_ID")
            or self.harness_memory_workspace_id
            or project_workspace_id
        )

    @classmethod
    def from_env(cls) -> "HarnessConfig":
        """Read the process environment, which `workspace.load_workspace()` has already filled
        from the workspace's `models.toml` and `.harness.env`. State paths default into the workspace."""
        root_text = os.getenv("HARNESS_WORKSPACE")
        root = Path(root_text).resolve() if root_text else None
        state = (root / STATE_DIR_NAME) if root else Path("data")
        trace_path = os.getenv("HARNESS_TRACE_DB") or str(state / "harness.sqlite3")
        trace_root = os.getenv("HARNESS_TRACE_ROOT") or str(state / "traces")
        unattended_approval_str = os.getenv("HARNESS_UNATTENDED_WRITE_REQUIRES_APPROVAL", "true").lower()
        roles_text = os.getenv("HARNESS_ROLE_PROFILES_FILE")
        if roles_text:
            roles_file: Path | None = Path(roles_text)
        elif root and (state / "roles.toml").is_file():
            roles_file = state / "roles.toml"
        else:
            roles_file = None
        return cls(
            memtrace_mcp_url=os.getenv("MEMTRACE_MCP_URL"),
            memtrace_api_token=os.getenv("MEMTRACE_API_TOKEN"),
            trace_db_path=Path(trace_path),
            trace_root=Path(trace_root),
            claude_command=os.getenv("HARNESS_CLAUDE_COMMAND", "claude"),
            codex_command=os.getenv("HARNESS_CODEX_COMMAND", "codex"),
            antigravity_command=os.getenv("HARNESS_ANTIGRAVITY_COMMAND", "agy"),
            cursor_command=os.getenv("HARNESS_CURSOR_COMMAND", "cursor-agent"),
            antigravity_output_mode=_antigravity_output_mode(
                os.getenv("HARNESS_ANTIGRAVITY_OUTPUT_MODE", "auto")
            ),
            cli_timeout_seconds=_positive_int(os.getenv("HARNESS_CLI_TIMEOUT_SECONDS"), 900),
            transcribe_command=os.getenv("HARNESS_TRANSCRIBE_COMMAND") or None,
            chat_provider=os.getenv("HARNESS_CHAT_PROVIDER", "claude"),
            chat_model=os.getenv("HARNESS_CHAT_MODEL", "haiku"),
            unattended_write_requires_approval=unattended_approval_str not in {"false", "0", "no"},
            harness_memory_workspace_id=os.getenv("HARNESS_MEMORY_WORKSPACE_ID"),
            chat_fallbacks=_parse_chat_fallbacks(os.getenv("HARNESS_CHAT_FALLBACKS", "")),
            digest_provider=os.getenv("HARNESS_DIGEST_PROVIDER") or None,
            digest_model=os.getenv("HARNESS_DIGEST_MODEL") or None,
            digest_fallbacks=_parse_chat_fallbacks(os.getenv("HARNESS_DIGEST_FALLBACKS", "")),
            archive_provider=os.getenv("HARNESS_ARCHIVE_PROVIDER") or None,
            archive_model=os.getenv("HARNESS_ARCHIVE_MODEL") or None,
            archive_fallbacks=_parse_chat_fallbacks(os.getenv("HARNESS_ARCHIVE_FALLBACKS", "")),
            audit_provider=os.getenv("HARNESS_AUDIT_PROVIDER") or None,
            audit_model=os.getenv("HARNESS_AUDIT_MODEL") or None,
            audit_fallbacks=_parse_chat_fallbacks(os.getenv("HARNESS_AUDIT_FALLBACKS", "")),
            recall_enabled=os.getenv("HARNESS_RECALL_ENABLED", "true").lower()
            not in {"false", "0", "no"},
            recall_provider=os.getenv("HARNESS_RECALL_PROVIDER") or None,
            recall_model=os.getenv("HARNESS_RECALL_MODEL") or None,
            recall_fallbacks=_parse_chat_fallbacks(os.getenv("HARNESS_RECALL_FALLBACKS", "")),
            recall_ttl_hours=_positive_int(os.getenv("HARNESS_RECALL_TTL_HOURS"), 72),
            operator_preference_workspace_id=os.getenv("HARNESS_OPERATOR_PREFERENCE_WORKSPACE_ID"),
            api_host=os.getenv("HARNESS_API_HOST", "127.0.0.1"),
            api_port=_positive_int(os.getenv("HARNESS_API_PORT"), 8800),
            api_token=os.getenv("HARNESS_API_TOKEN") or None,
            peer_api_token=os.getenv("HARNESS_PEER_API_TOKEN") or None,
            peer_max_concurrent=_positive_int(os.getenv("HARNESS_PEER_MAX_CONCURRENT"), 2),
            peer_description=os.getenv("HARNESS_PEER_DESCRIPTION") or None,
            peer_name=os.getenv("HARNESS_PEER_NAME") or None,
            workspace_root=root,
            role_profiles_file=roles_file,
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
