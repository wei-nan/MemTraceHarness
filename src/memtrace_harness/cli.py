from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
import sys
from uuid import uuid4
from zoneinfo import ZoneInfo

from memtrace_harness.adapter_factory import (
    build_provider_adapter,
    build_role_adapter_candidates,
    provider_commands,
)
from memtrace_harness.adapters import ModelAdapter
from memtrace_harness.cli_process import CliProcessRunner
from memtrace_harness.config import HarnessConfig
from memtrace_harness.approval import ApprovalManager
from memtrace_harness.chat_triage import ChatTriage
from memtrace_harness.loop import AgentLoopRunner
from memtrace_harness.memtrace_client import MemTraceClient
from memtrace_harness.primary_session import PrimarySessionManager
from memtrace_harness.role_profiles import load_role_profiles
from memtrace_harness.runner import HarnessRunner
from memtrace_harness.scanner import UnattendedScanner
from memtrace_harness.schedule import ScheduleSpec, compute_next_run
from memtrace_harness.schemas import TaskEnvelope
from memtrace_harness.scope import load_project_index
from memtrace_harness.telegram_gateway import TelegramGateway
from memtrace_harness.trace_store import TraceStore


logger = logging.getLogger(__name__)

PROVIDERS = ("claude", "codex", "antigravity")

DEFAULT_CONSTRAINTS = [
    "Do not automatically resolve MemTrace inquiries.",
    "Do not promote model consensus into formal knowledge without human review.",
    "Keep raw provider transcripts in the Harness trace store, not MemTrace.",
    "Do not call model provider HTTP APIs; use the configured local CLI only.",
]

DEFAULT_DONE_WHEN = [
    "Selected CLI executions have a terminal status.",
    "Provider usage is normalized when the CLI exposes it.",
    "Raw stdout and stderr references are persisted for audit.",
]


class SingleUseAction(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None) -> None:
        if getattr(namespace, self.dest, None) is not None:
            parser.error(f"{option_string} may be provided only once")
        setattr(namespace, self.dest, values)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "run":
            return run_command(args)
        if args.command == "probe":
            return probe_command(args)
        if args.command == "loop":
            return loop_command(args)
        if args.command == "gateway":
            return gateway_command(args)
        if args.command == "scan":
            return scan_command(args)
        if args.command == "init-project":
            return init_project_command(args)
        if args.command == "status":
            return status_command(args)
        if args.command == "remove-project":
            return remove_project_command(args)
    except (OSError, RuntimeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    parser.print_help()
    return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="memtrace-harness",
        description="Run Claude, Codex, and Antigravity through local CLIs only.",
    )
    subparsers = parser.add_subparsers(dest="command")

    probe_parser = subparsers.add_parser(
        "probe", help="Check CLI availability without a model call"
    )
    probe_parser.add_argument(
        "--agent", action="append", choices=PROVIDERS, help="Provider to probe; defaults to all."
    )
    probe_parser.add_argument("--working-directory", type=Path, default=Path.cwd())
    probe_parser.add_argument("--json", action="store_true")

    run_parser = subparsers.add_parser("run", help="Run one local model CLI")
    run_parser.add_argument("--workspace", required=True, help="MemTrace workspace id")
    run_parser.add_argument("--goal", required=True, help="Task goal")
    run_parser.add_argument(
        "--agent",
        action=SingleUseAction,
        choices=PROVIDERS,
        required=True,
        help="Provider CLI to run. One provider is allowed per run.",
    )
    run_parser.add_argument("--working-directory", type=Path, default=Path.cwd())
    run_parser.add_argument("--timeout-seconds", type=int)
    run_parser.add_argument(
        "--context-ref",
        action="append",
        default=[],
        help="MemTrace node id or external context reference. Can be repeated.",
    )
    run_parser.add_argument(
        "--risk-level", choices=["low", "medium", "high"], default="medium"
    )
    run_parser.add_argument(
        "--hydrate-context",
        action="store_true",
        help="Read mem_* context refs over MemTrace MCP before launching CLIs.",
    )
    run_parser.add_argument("--context-max-tokens", type=int, default=3000)
    run_parser.add_argument(
        "--writeback",
        action="store_true",
        help="Create a draft-only summary through MemTrace MCP after the CLI run.",
    )
    run_parser.add_argument("--json", action="store_true")

    loop_parser = subparsers.add_parser(
        "loop", help="Run the bounded Luna/Sonnet/Sol/Gemini role-profile loop"
    )
    loop_parser.add_argument("--workspace", required=True, help="MemTrace workspace id")
    loop_parser.add_argument("--goal", required=True, help="Task goal")
    loop_parser.add_argument("--working-directory", type=Path, default=Path.cwd())
    loop_parser.add_argument("--timeout-seconds", type=int)
    loop_parser.add_argument(
        "--context-ref",
        action="append",
        default=[],
        help="MemTrace node id or external context reference. Can be repeated.",
    )
    loop_parser.add_argument(
        "--conversation-id",
        help=(
            "Continue a Harness conversation from its latest durable checkpoint; "
            "omit to create a new conversation."
        ),
    )
    loop_parser.add_argument(
        "--risk-level", choices=["low", "medium", "high"], default="medium"
    )
    loop_parser.add_argument(
        "--hydrate-context",
        action="store_true",
        help="Read mem_* context refs over MemTrace MCP before launching the loop.",
    )
    loop_parser.add_argument("--context-max-tokens", type=int, default=3000)
    loop_parser.add_argument(
        "--profiles-file",
        type=Path,
        help="Optional TOML role-profile file; defaults to the packaged policy.",
    )
    loop_parser.add_argument(
        "--max-total-tokens",
        type=int,
        help="Stop closed at a stage boundary when known input+output usage reaches this value.",
    )
    loop_parser.add_argument(
        "--writeback",
        action="store_true",
        help="Create a draft-only loop evidence node through MemTrace MCP.",
    )
    loop_parser.add_argument("--json", action="store_true")

    gateway_parser = subparsers.add_parser(
        "gateway", help="Start the Telegram gateway and unattended scanner"
    )
    gateway_parser.add_argument(
        "--serve",
        action="store_true",
        help=(
            "Run continuously using Telegram long-polling instead of one "
            "poll-and-exit pass. No public endpoint required."
        ),
    )
    gateway_parser.add_argument(
        "--poll-timeout-seconds",
        type=int,
        default=30,
        help="Telegram getUpdates long-poll timeout per cycle in --serve mode (default 30).",
    )
    gateway_parser.add_argument(
        "--scan-interval-seconds",
        type=int,
        default=1800,
        help="Minimum seconds between unattended backlog scan passes in --serve mode (default 1800). "
        "Each pass costs one search_nodes call per project even when the backlog is empty — keep this "
        "well above a few minutes or idle scanning dominates MemTrace token spend (was 60s, see "
        "ws_spec_plan/mem_c9c4affd).",
    )
    gateway_parser.add_argument(
        "--consolidation-interval-seconds",
        type=int,
        default=3600,
        help=(
            "Minimum seconds between cold-memory consolidation passes (hot primary-session "
            "turns -> draft MemTrace nodes) per project in --serve mode (default 3600)."
        ),
    )
    gateway_parser.add_argument(
        "--schedule-check-interval-seconds",
        type=int,
        default=30,
        help=(
            "Minimum seconds between checks for due chat-created schedules "
            "(see HARNESS_SCHEDULE_START in the chat) in --serve mode (default 30). "
            "Cheap (a single indexed SQLite query), safe to keep short."
        ),
    )

    subparsers.add_parser(
        "scan", help="Run a single pass of the unattended backlog scanner"
    )

    subparsers.add_parser(
        "init-project",
        help="Interactive wizard to register a new project (harness-scope.md, .env entries)",
    )

    subparsers.add_parser(
        "status",
        help="Show running gateway process(es), registered projects, locks, and pending approvals",
    )

    remove_project_parser = subparsers.add_parser(
        "remove-project", help="Unregister a project (does not delete its harness-scope.md)"
    )
    remove_project_parser.add_argument(
        "name", nargs="?", help="Project name to remove; omit to be prompted interactively"
    )
    remove_project_parser.add_argument(
        "--yes", action="store_true", help="Skip the confirmation prompt"
    )
    return parser


def run_command(args: argparse.Namespace) -> int:
    config = HarnessConfig.from_env()
    working_directory = args.working_directory.resolve()
    if not working_directory.is_dir():
        raise ValueError(f"Working directory does not exist: {working_directory}")
    timeout_seconds = args.timeout_seconds or config.cli_timeout_seconds
    if timeout_seconds <= 0:
        raise ValueError("--timeout-seconds must be greater than zero")

    memtrace_client = _memtrace_client(args, config)
    task_id = f"task_{uuid4().hex[:12]}"
    context_refs = list(args.context_ref)
    context_items = []
    if args.hydrate_context:
        assert memtrace_client is not None
        context_items = memtrace_client.hydrate_context_refs(
            workspace_id=args.workspace,
            refs=context_refs,
            max_response_tokens=args.context_max_tokens,
            task_id=task_id,
        )

    task = TaskEnvelope(
        task_id=task_id,
        workspace_id=args.workspace,
        goal=args.goal,
        context_refs=context_refs,
        context_items=context_items,
        constraints=DEFAULT_CONSTRAINTS,
        done_when=DEFAULT_DONE_WHEN,
        risk_level=args.risk_level,
        source="harness-cli",
    )
    adapters = build_adapters(
        providers=[args.agent],
        config=config,
        working_directory=working_directory,
        timeout_seconds=timeout_seconds,
    )
    summary = HarnessRunner(
        adapters=adapters,
        trace_store=TraceStore(config.trace_db_path),
        memtrace_client=memtrace_client,
    ).run(task, writeback=args.writeback)

    if args.json:
        print(json.dumps(summary.to_dict(), ensure_ascii=False, indent=2))
    else:
        _print_summary(summary, config)
    return 0 if all(item.execution.status == "succeeded" for item in summary.responses) else 1


def probe_command(args: argparse.Namespace) -> int:
    config = HarnessConfig.from_env()
    working_directory = args.working_directory.resolve()
    if not working_directory.is_dir():
        raise ValueError(f"Working directory does not exist: {working_directory}")
    commands = provider_commands(config)
    runner = CliProcessRunner()
    providers = _deduplicate(args.agent or list(PROVIDERS))
    results = {}
    for provider in providers:
        result = runner.probe(commands[provider], cwd=working_directory)
        capabilities = {}
        if provider == "antigravity" and not result.unavailable:
            help_result = runner.run(
                [commands[provider], "--help"], cwd=working_directory, timeout_seconds=10
            )
            help_text = f"{help_result.stdout}\n{help_result.stderr}"
            capabilities["stream_json"] = "--output-format" in help_text
        results[provider] = {
            "available": not result.unavailable and result.return_code == 0,
            "command": result.command,
            "exit_code": result.return_code,
            "version": (result.stdout or result.stderr).strip().splitlines()[:1],
            "error": result.error,
            "capabilities": capabilities,
        }
    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        for provider, result in results.items():
            state = "available" if result["available"] else "unavailable"
            detail = result["error"] or "; ".join(result["version"]) or "no version output"
            print(f"{provider}: {state} ({detail})")
    return 0 if all(item["available"] for item in results.values()) else 1


def loop_command(args: argparse.Namespace) -> int:
    config = HarnessConfig.from_env()
    working_directory = args.working_directory.resolve()
    if not working_directory.is_dir():
        raise ValueError(f"Working directory does not exist: {working_directory}")
    timeout_seconds = args.timeout_seconds or config.cli_timeout_seconds
    if timeout_seconds <= 0:
        raise ValueError("--timeout-seconds must be greater than zero")
    if args.max_total_tokens is not None and args.max_total_tokens <= 0:
        raise ValueError("--max-total-tokens must be greater than zero")

    memtrace_client = _memtrace_client(args, config)
    task = _task_from_args(args, memtrace_client)
    profiles = load_role_profiles(args.profiles_file)
    candidate_adapters = build_role_adapter_candidates(
        profiles=profiles,
        config=config,
        working_directory=working_directory,
        timeout_seconds=timeout_seconds,
    )
    summary = AgentLoopRunner(
        adapters={profile_id: items[0] for profile_id, items in candidate_adapters.items()},
        fallback_adapters={
            profile_id: items[1:] for profile_id, items in candidate_adapters.items()
        },
        role_profiles=profiles,
        trace_store=TraceStore(config.trace_db_path),
        memtrace_client=memtrace_client,
        max_total_tokens=args.max_total_tokens,
        working_directory=working_directory,
        config=config,
    ).run(
        task,
        writeback=args.writeback,
        conversation_id=args.conversation_id,
    )

    if args.json:
        print(json.dumps(summary.to_dict(), ensure_ascii=False, indent=2))
    else:
        _print_loop_summary(summary, config)
    return 0 if summary.status == "succeeded" else 1


def build_adapters(
    *,
    providers: list[str],
    config: HarnessConfig,
    working_directory: Path,
    timeout_seconds: int,
) -> list[ModelAdapter]:
    return [
        build_provider_adapter(
            provider=provider,
            config=config,
            working_directory=working_directory,
            timeout_seconds=timeout_seconds,
        )
        for provider in providers
    ]


def _memtrace_client(args: argparse.Namespace, config: HarnessConfig) -> MemTraceClient | None:
    if not (args.writeback or args.hydrate_context):
        return None
    if not config.memtrace_mcp_url:
        raise RuntimeError(
            "MEMTRACE_MCP_URL is required when --writeback or --hydrate-context is set"
        )
    return MemTraceClient(config.memtrace_mcp_url, config.memtrace_api_token)


def _task_from_args(
    args: argparse.Namespace, memtrace_client: MemTraceClient | None
) -> TaskEnvelope:
    task_id = f"task_{uuid4().hex[:12]}"
    context_refs = list(args.context_ref)
    context_items = []
    if args.hydrate_context:
        assert memtrace_client is not None
        context_items = memtrace_client.hydrate_context_refs(
            workspace_id=args.workspace,
            refs=context_refs,
            max_response_tokens=args.context_max_tokens,
            task_id=task_id,
        )
    return TaskEnvelope(
        task_id=task_id,
        workspace_id=args.workspace,
        goal=args.goal,
        context_refs=context_refs,
        context_items=context_items,
        constraints=DEFAULT_CONSTRAINTS,
        done_when=DEFAULT_DONE_WHEN,
        risk_level=args.risk_level,
        source="harness-agent-loop",
    )


def _print_summary(summary, config: HarnessConfig) -> None:
    print(f"trace_id: {summary.trace_id}")
    print(f"run_mode: {summary.run_mode}")
    for response in summary.responses:
        usage = response.execution.usage
        print(
            f"{response.adapter_id}: {response.execution.status}; "
            f"input={usage.input_tokens}; cached={usage.cached_input_tokens}; "
            f"output={usage.output_tokens}; reasoning={usage.reasoning_output_tokens}; "
            f"usage={usage.completeness}"
        )
        print(f"  raw_trace: {response.execution.raw_trace_ref}")
    print(f"recommendation: {summary.recommendation}")
    if summary.writeback_node_id:
        print(f"writeback_node_id: {summary.writeback_node_id}")
    print(f"trace_db: {config.trace_db_path}")


def _print_loop_summary(summary, config: HarnessConfig) -> None:
    print(f"trace_id: {summary.trace_id}")
    print(f"conversation_id: {summary.conversation_id}")
    print(f"active_checkpoint_id: {summary.active_checkpoint_id}")
    print(f"run_mode: {summary.run_mode}")
    print(f"status: {summary.status}")
    for stage in summary.stages:
        usage = stage.response.execution.usage
        print(
            f"{stage.sequence}. {stage.stage}: {stage.profile_id}; state={stage.state}; "
            f"model={stage.response.execution.requested_model}; "
            f"fallback_index={stage.attempt_index}; "
            f"failure={stage.response.execution.failure_category}; "
            f"input={usage.input_tokens}; output={usage.output_tokens}; "
            f"usage={usage.completeness}"
        )
    print(
        f"total_usage: input={summary.total_usage.input_tokens}; "
        f"output={summary.total_usage.output_tokens}; "
        f"completeness={summary.total_usage.completeness}"
    )
    print(f"recommendation: {summary.recommendation}")
    if summary.writeback_node_id:
        print(f"writeback_node_id: {summary.writeback_node_id}")
    print(f"trace_db: {config.trace_db_path}")


def gateway_command(args: argparse.Namespace) -> int:
    config = HarnessConfig.from_env()
    trace_store = TraceStore(config.trace_db_path)
    approval_mgr = ApprovalManager(trace_store, config.telegram_allowed_chat_ids)
    projects = load_project_index(config.project_index_path)
    memtrace_client = MemTraceClient(config.memtrace_mcp_url, config.memtrace_api_token) if config.memtrace_mcp_url else None
    primary_session_mgr = PrimarySessionManager(trace_store, memtrace_client)

    # Each project's harness-scope.md may carry its own telegram_bot_token, giving it a
    # dedicated bot (its own identity, KB, Agent Loop and Improvement Loop context — all
    # already scoped per-project via ProjectScope.workspace_id/working_directory). Projects
    # without one share the fallback HARNESS_TELEGRAM_BOT_TOKEN bot and must name themselves
    # in chat text to route correctly; a project with its own bot needs no naming at all,
    # since ChatTriage defaults to the sole project registered to that bot.
    groups: dict[str, list] = {}
    for scope in projects:
        # harness-scope.md's telegram_bot_token stays supported for anyone who already
        # set it, but config.telegram_bot_token_for() (a HARNESS_TELEGRAM_BOT_TOKEN_<NAME>
        # var in this Harness's own .env) is how a project gets a dedicated bot without
        # writing a secret into that project's own repo.
        token = scope.telegram_bot_token or config.telegram_bot_token_for(scope.name)
        if token:
            groups.setdefault(token, []).append(scope)
    if not groups and config.telegram_bot_token:
        groups[config.telegram_bot_token] = []
    if not groups:
        raise RuntimeError(
            "No Telegram bot token configured: set HARNESS_TELEGRAM_BOT_TOKEN, or a "
            "per-project telegram_bot_token in that project's harness-scope.md"
        )

    gateways = []
    gateway_for_project: dict[str, TelegramGateway] = {}
    for token, group_projects in groups.items():
        triage = ChatTriage(group_projects)
        gw = TelegramGateway(config, approval_mgr, triage, primary_session_mgr, group_projects, memtrace_client, bot_token=token)
        gateways.append(gw)
        for scope in group_projects:
            gateway_for_project[scope.name] = gw

    scanner = UnattendedScanner(config, trace_store, projects, memtrace_client, gateway_for_project, approval_mgr)

    if args.serve:
        return _serve_gateway_loop(
            gateways,
            scanner,
            primary_session_mgr,
            projects,
            config,
            trace_store,
            gateway_for_project,
            poll_timeout=max(1, args.poll_timeout_seconds),
            scan_interval=max(1, args.scan_interval_seconds),
            consolidation_interval=max(1, args.consolidation_interval_seconds),
            schedule_check_interval=max(1, args.schedule_check_interval_seconds),
        )

    print(f"Starting Telegram gateway ({len(gateways)} bot(s)) and unattended scanner...")
    processed = sum(gw.poll_once() for gw in gateways)
    scan_results = scanner.run_scan_pass()
    print(f"Processed {processed} Telegram updates; scan pass results: {scan_results}")
    return 0


def _classify_via_model(
    candidates: tuple[tuple[str, str], ...],
    config: HarnessConfig,
    working_directory: Path,
    prompt: str,
    contents: list[str],
    positive_word: str,
) -> list[bool]:
    """Shared shape for both classifiers below: try each (provider, model) candidate in
    order, ask for exactly one verdict line per input, and parse it. A provider outage
    (not logged in, quota exhausted) falls through to the next candidate rather than
    failing the whole classification pass."""
    if not contents or not candidates:
        return [False] * len(contents)
    for provider, model in candidates:
        cmd = [config.command_for(provider)]
        if model:
            cmd.extend(["--model", model])
        cmd.extend(["--print", prompt])
        result = CliProcessRunner().run(cmd, cwd=working_directory, timeout_seconds=60)
        if result.return_code != 0 or not result.stdout.strip():
            continue
        lines = [ln.strip().upper() for ln in result.stdout.strip().splitlines() if ln.strip()]
        return [ln.startswith(positive_word) for ln in lines]
    return [False] * len(contents)


def _make_chat_classifier(config: HarnessConfig, project_name: str, working_directory: Path):
    """Build the classify_chat_fn consolidate_to_memtrace() uses to judge plain chat
    turns. Deliberately a cheap, low-stakes model call — that project's own chat model
    (chat_candidates_for(), same resolution as the quick-chat-reply path) — since this
    only decides whether to write a draft note, never whether to run real work; a model
    verdict is an acceptable amount of risk here in a way it would not be for the
    Agent Loop itself."""
    candidates = config.chat_candidates_for(project_name)

    def classify(contents: list[str]) -> list[bool]:
        prompt = (
            "Below are chat messages from a project's conversation log, numbered in "
            "order. For each one, decide if it is substantive — a decision, "
            "requirement, agreed plan, important fact, or something that should be "
            "remembered later — versus just small talk, a greeting, or a status check "
            "with no lasting value.\n\n"
            f"Reply with EXACTLY {len(contents)} lines, one per message in the same "
            "order, each line either the single word SUBSTANTIVE or the single word "
            "CHAT and nothing else.\n\n"
            + "\n".join(f"{i + 1}. {c}" for i, c in enumerate(contents))
        )
        return _classify_via_model(candidates, config, working_directory, prompt, contents, "SUBSTANTIVE")

    return classify


def _make_preference_classifier(config: HarnessConfig, working_directory: Path):
    """Build the classify_fn consolidate_preferences() uses. Deliberately global, not
    per-project (config.chat_provider/model/fallbacks directly, not chat_candidates_for)
    — an operator's preferences aren't scoped to whichever project they happened to
    mention them in. A different question from _make_chat_classifier(): not "does this
    matter for the project" but "does this reveal how the human wants to be worked
    with"."""
    candidates = ((config.chat_provider, config.chat_model), *config.chat_fallbacks) if config.chat_provider else ()

    def classify(contents: list[str]) -> list[bool]:
        prompt = (
            "Below are chat messages from a conversation log, numbered in order. For "
            "each one, decide if it reveals something about how this human wants to be "
            "worked with — a stated preference, an instruction about interaction style, "
            "tone, formatting, or approval habits, or a standing rule for how the "
            "assistant should behave — versus messages that are only about the project's "
            "own work, or ordinary small talk with no such signal.\n\n"
            f"Reply with EXACTLY {len(contents)} lines, one per message in the same "
            "order, each line either the single word PREFERENCE or the single word "
            "OTHER and nothing else.\n\n"
            + "\n".join(f"{i + 1}. {c}" for i, c in enumerate(contents))
        )
        return _classify_via_model(candidates, config, working_directory, prompt, contents, "PREFERENCE")

    return classify


def _serve_gateway_loop(
    gateways: list[TelegramGateway],
    scanner: UnattendedScanner,
    primary_session_mgr: PrimarySessionManager,
    projects: list,
    config: HarnessConfig,
    trace_store: TraceStore,
    gateway_for_project: dict[str, TelegramGateway],
    *,
    poll_timeout: int,
    scan_interval: int,
    consolidation_interval: int,
    schedule_check_interval: int,
) -> int:
    """Long-poll continuously instead of the one-shot poll-and-exit gateway command.
    Telegram's getUpdates blocks server-side up to `poll_timeout` seconds when there is
    nothing new, so this stays near-real-time without a public webhook endpoint — no
    inbound port, no TLS certificate, nothing exposed to the internet. A crash in one
    poll/scan/consolidation cycle is logged and the loop continues rather than taking
    the whole process down; only SIGINT/SIGTERM stop it cleanly."""
    print(
        f"Serving Telegram gateway ({len(gateways)} bot(s)); "
        f"poll_timeout={poll_timeout}s scan_interval={scan_interval}s "
        f"consolidation_interval={consolidation_interval}s "
        f"schedule_check_interval={schedule_check_interval}s. Ctrl-C to stop."
    )

    from memtrace_harness.status_server import start_status_server

    status_server, status_bus = start_status_server(config)
    if status_server is not None:
        print(f"Status dashboard: http://{config.status_server_host}:{config.status_server_port}")

    stop_requested = False

    def _handle_stop(signum: int, frame: object) -> None:
        nonlocal stop_requested
        stop_requested = True

    signal.signal(signal.SIGINT, _handle_stop)
    signal.signal(signal.SIGTERM, _handle_stop)

    for gw in gateways:
        label = ", ".join(p.name for p in gw.projects) or "unassigned"
        try:
            gw.register_bot_commands()
        except Exception:
            logger.exception(f"[{label}] registering bot commands failed; continuing")
        try:
            gw.notify_all_allowlisted(f"✅ Harness gateway 已啟動，開始監聽（{label}）。")
        except Exception:
            logger.exception(f"[{label}] startup notification failed; continuing")

    last_scan = 0.0
    last_consolidation = 0.0
    last_schedule_check = 0.0
    schedule_tz = ZoneInfo(config.schedule_timezone)
    while not stop_requested:
        for gw in gateways:
            if stop_requested:
                break
            label = ", ".join(p.name for p in gw.projects) or "unassigned"
            try:
                processed = gw.poll_once(timeout=poll_timeout)
                if processed:
                    print(f"[{label}] processed {processed} update(s)")
                    if status_bus is not None:
                        status_bus.publish()
            except Exception:
                logger.exception(f"[{label}] gateway poll cycle failed; continuing")

        if stop_requested:
            break

        now = time.monotonic()
        if now - last_scan >= scan_interval:
            try:
                scan_results = scanner.run_scan_pass()
                noteworthy = {
                    k: v for k, v in scan_results.items() if v not in ("no_backlog", "skipped_locked")
                }
                if noteworthy:
                    print(f"scan pass: {noteworthy}")
                    if status_bus is not None:
                        status_bus.publish()
            except Exception:
                logger.exception("scan pass failed; continuing")
            last_scan = now

        if now - last_schedule_check >= schedule_check_interval:
            try:
                due = trace_store.list_due_schedules(datetime.now(timezone.utc))
                for row in due:
                    gw = gateway_for_project.get(row["project"])
                    if gw is None:
                        logger.error(
                            f"schedule {row['id']} targets project '{row['project']}' with "
                            "no active gateway; skipping this run"
                        )
                        continue
                    # Advance next_run_at before triggering: run_due_schedule() only
                    # starts a background thread (non-blocking), and if the workspace
                    # turns out to be locked it just skips this occurrence — either
                    # way the schedule must not re-fire every tick until it succeeds.
                    spec = ScheduleSpec(
                        kind=row["kind"],
                        interval_seconds=row["interval_seconds"],
                        time_of_day=row["time_of_day"],
                    )
                    ran_at = datetime.now(timezone.utc)
                    next_run_at = compute_next_run(spec, after=ran_at, tz=schedule_tz)
                    trace_store.mark_schedule_ran(
                        row["id"], ran_at=ran_at, next_run_at=next_run_at, status="triggered"
                    )
                    try:
                        gw.run_due_schedule(row)
                    except Exception:
                        logger.exception(f"scheduled run for {row['id']} failed to start; continuing")
                if due:
                    print(f"triggered {len(due)} due schedule(s)")
                    if status_bus is not None:
                        status_bus.publish()
            except Exception:
                logger.exception("schedule check pass failed; continuing")
            last_schedule_check = now

        if now - last_consolidation >= consolidation_interval:
            for scope in projects:
                try:
                    memory_workspace_id = config.memory_workspace_id_for(scope.name, scope.workspace_id)
                    classify_fn = _make_chat_classifier(config, scope.name, scope.working_directory)
                    written = primary_session_mgr.consolidate_to_memtrace(
                        scope.name, memory_workspace_id, classify_fn
                    )
                    if written:
                        print(f"[{scope.name}] consolidated {len(written)} turn(s) to MemTrace as draft evidence")
                        if status_bus is not None:
                            status_bus.publish()
                except Exception:
                    logger.exception(f"[{scope.name}] cold-memory consolidation failed; will retry next cycle")

                # Independent second axis: not "does this matter for the project" but
                # "does this reveal how the human wants to be worked with" — cross-project,
                # written into a single evolving operator-profile node, not per-project.
                if config.operator_preference_workspace_id:
                    try:
                        pref_classify_fn = _make_preference_classifier(config, scope.working_directory)
                        pref_written = primary_session_mgr.consolidate_preferences(
                            scope.name, config.operator_preference_workspace_id, pref_classify_fn
                        )
                        if pref_written:
                            print(f"[{scope.name}] merged {len(pref_written)} preference note(s) into operator profile")
                    except Exception:
                        logger.exception(f"[{scope.name}] preference consolidation failed; will retry next cycle")
            last_consolidation = now

    _drain_inflight_agent_loops(config.shutdown_grace_seconds)

    if status_server is not None:
        status_server.shutdown()

    print("Gateway serve loop stopped.")
    return 0


def _drain_inflight_agent_loops(grace_seconds: int) -> None:
    """Background Agent Loop threads (Telegram tasks, approval resumes, unattended
    scan backlog runs) are daemon threads — the interpreter doesn't wait for them or
    guarantee their `finally` blocks run on exit, so without this, killing the process
    mid-run leaves that workspace's lock stuck forever. Wait up to grace_seconds for
    any in-flight runs to finish normally; if they don't, force-kill the underlying CLI
    subprocess that thread is currently blocked on (via cli_process.py's thread-keyed
    process registry — closes the gap noted in the shutdown-safety work: killing the
    Harness process alone left the CLI subprocess running as an orphan, bounded only
    by its own cli_timeout_seconds, not by this grace period) and force-release the
    workspace lock so the next start isn't blocked. The interrupted run's outcome is
    still unknown either way — this makes shutdown bounded and clean, not silent."""
    from memtrace_harness.cli_process import kill_process_for_thread
    from memtrace_harness.inflight import default_tracker

    remaining = default_tracker.snapshot()
    if not remaining:
        return
    print(
        f"Waiting up to {grace_seconds}s for {len(remaining)} in-flight Agent Loop "
        "run(s) to finish before exiting..."
    )
    if default_tracker.wait_for_drain(grace_seconds):
        return
    stuck = default_tracker.snapshot()
    for entry in stuck:
        thread_ident = entry["thread"].ident
        killed = thread_ident is not None and kill_process_for_thread(thread_ident)
        logger.warning(
            f"Shutdown grace period exceeded; {'killed the CLI subprocess and ' if killed else ''}"
            f"force-releasing workspace lock for {entry['workspace_id']} "
            f"(conversation {entry['conversation_id']}) — its Agent Loop run was "
            "interrupted mid-flight and its outcome is unknown. Check the trace store "
            "/ raw trace logs once the new process is up."
        )
        try:
            entry["trace_store"].release_workspace_lock(entry["workspace_id"])
        except Exception:
            logger.exception("failed to force-release workspace lock during shutdown")


def scan_command(args: argparse.Namespace) -> int:
    config = HarnessConfig.from_env()
    trace_store = TraceStore(config.trace_db_path)
    approval_mgr = ApprovalManager(trace_store, config.telegram_allowed_chat_ids)
    projects = load_project_index(config.project_index_path)
    memtrace_client = MemTraceClient(config.memtrace_mcp_url, config.memtrace_api_token) if config.memtrace_mcp_url else None
    gateway = TelegramGateway(config, approval_mgr, ChatTriage(projects), PrimarySessionManager(trace_store, memtrace_client), projects) if config.telegram_bot_token else None
    scanner = UnattendedScanner(config, trace_store, projects, memtrace_client, gateway, approval_mgr)

    results = scanner.run_scan_pass()
    print(json.dumps(results, indent=2))
    return 0


def _deduplicate(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def init_project_command(args: argparse.Namespace) -> int:
    """Interactive wizard for registering a new project: writes harness-scope.md,
    adds it to projects.index.txt, and writes any per-project .env overrides the
    operator chooses (bot token, chat model, memory workspace, role-profiles file).
    Nothing here is hot-reloaded — see the reminder printed at the end."""
    from memtrace_harness.config import (
        project_bot_token_env_var,
        project_chat_fallbacks_env_var,
        project_chat_model_env_var,
        project_chat_provider_env_var,
        project_memory_workspace_env_var,
        project_role_profiles_env_var,
    )

    config = HarnessConfig.from_env()

    print("=== Harness 專案設定精靈 ===")
    print("走過一輪問答，幫你把新專案要碰的檔案跟 .env 設定都準備好。\n")

    working_dir = _wizard_prompt_path("這個專案的工作目錄（絕對路徑)")
    default_name = working_dir.name
    name = _wizard_prompt("專案名稱", default=default_name)

    workspace_id = _wizard_prompt_workspace_id(
        config, "這個專案的 MemTrace 規格/待辦 workspace_id（產品規劃、決策用)"
    )
    off_limits = _wizard_prompt("禁區關鍵字（逗號分隔，沒有就留空)", default="")

    scope_path = working_dir / "harness-scope.md"
    if scope_path.exists():
        overwrite = _wizard_prompt_yes_no(
            f"{scope_path} 已經存在，要覆蓋嗎？", default=False
        )
        if not overwrite:
            print("保留現有的 harness-scope.md，跳過這一步。")
        else:
            _write_harness_scope(scope_path, name, workspace_id, off_limits)
            print(f"已寫入 {scope_path}")
    else:
        _write_harness_scope(scope_path, name, workspace_id, off_limits)
        print(f"已寫入 {scope_path}")

    index_path = config.project_index_path or Path("projects.index.txt")
    _ensure_in_project_index(index_path, scope_path)
    print(f"已確認 {index_path} 有列到這個專案。\n")

    env_updates: dict[str, str] = {}

    dedicated_bot = _wizard_prompt_yes_no(
        f"要幫「{name}」設定專屬 Telegram bot 嗎？（不然會共用全域 bot,訊息要指名專案名才能路由對)",
        default=True,
    )
    if dedicated_bot:
        print("去 Telegram 找 @BotFather，傳 /newbot 建立一個新 bot，完成後把它給你的 token 貼在這裡。")
        token = input("Bot token: ").strip()
        if token:
            env_updates[project_bot_token_env_var(name)] = token
    elif not config.telegram_bot_token:
        print("⚠️  目前沒有全域 HARNESS_TELEGRAM_BOT_TOKEN，這個專案在你設定其中一個之前收不到任何 Telegram 訊息。")

    print("\n聊天模型（快速對話用，走的不是完整 Agent Loop):")
    print("  1) Gemini（便宜、預設建議）  2) Claude  3) Codex  4) 沿用全域預設，不覆蓋")
    chat_choice = _wizard_prompt_choice("選擇", ["1", "2", "3", "4"], default="1")
    if chat_choice == "1":
        env_updates[project_chat_provider_env_var(name)] = "antigravity"
        env_updates[project_chat_model_env_var(name)] = "gemini-3.6-flash-high"
        env_updates[project_chat_fallbacks_env_var(name)] = "claude/haiku"
    elif chat_choice == "2":
        env_updates[project_chat_provider_env_var(name)] = "claude"
        env_updates[project_chat_model_env_var(name)] = "haiku"
        env_updates[project_chat_fallbacks_env_var(name)] = "antigravity/gemini-3.6-flash-high"
    elif chat_choice == "3":
        env_updates[project_chat_provider_env_var(name)] = "codex"
        env_updates[project_chat_model_env_var(name)] = ""

    dedicated_memory = _wizard_prompt_yes_no(
        "這個專案要獨立的冷記憶 workspace 嗎？（不要的話沿用全域共用的 Harness Memory)",
        default=False,
    )
    if dedicated_memory:
        mem_ws = _wizard_prompt_workspace_id(config, "冷記憶（專案決策)要寫進哪個 workspace_id")
        env_updates[project_memory_workspace_env_var(name)] = mem_ws

    dedicated_profiles = _wizard_prompt_yes_no(
        "要複製一份可自訂的 role-profiles.toml 給這個專案嗎？（Agent Loop 各角色用的模型)",
        default=False,
    )
    if dedicated_profiles:
        dest = Path("profiles") / f"{_slugify(name)}.toml"
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not dest.exists():
            default_toml = Path(__file__).with_name("default-role-profiles.toml")
            dest.write_text(default_toml.read_text(encoding="utf-8"), encoding="utf-8")
            print(f"已複製一份到 {dest}。")
        else:
            print(f"沿用既有的 {dest}。")

        advanced = _wizard_prompt_yes_no(
            "── 進階設定 ── 要逐一調整 Controller/Planner/Planner-escalation/"
            "Red Team/Developer 實際用的模型嗎？（不調整就維持該檔案目前內容)",
            default=False,
        )
        if advanced:
            text = dest.read_text(encoding="utf-8")
            text = _wizard_configure_roles(text)
            dest.write_text(text, encoding="utf-8")

        env_updates[project_role_profiles_env_var(name)] = str(dest)

    if env_updates:
        env_path = Path(".env")
        _write_env_updates(env_path, env_updates)
        print(f"\n已把以下設定寫進 {env_path}：")
        for key, value in env_updates.items():
            display = f"{value[:6]}..." if "TOKEN" in key and value else value
            print(f"  {key}={display}")
    else:
        print("\n沒有新增任何 .env 設定（都沿用全域預設)。")

    print("\n=== 完成 ===")
    print("檢查一下上面寫進 .env / harness-scope.md / role-profiles.toml 的內容是否正確。")
    if env_updates or dedicated_profiles:
        _wizard_offer_restart()
    print("設定生效後，傳一句訊息到對應的 bot 測試。")
    return 0


def _wizard_prompt(question: str, *, default: str = "") -> str:
    suffix = f"（預設：{default}）" if default else ""
    answer = input(f"{question}{suffix}: ").strip()
    return answer or default


def _wizard_prompt_path(question: str) -> Path:
    while True:
        answer = input(f"{question}: ").strip()
        if not answer:
            print("這個一定要填。")
            continue
        path = Path(answer).expanduser().resolve()
        if not path.is_dir():
            create = _wizard_prompt_yes_no(f"{path} 不存在，要建立嗎？", default=False)
            if create:
                path.mkdir(parents=True, exist_ok=True)
            else:
                continue
        return path


def _wizard_prompt_choice(question: str, choices: list[str], *, default: str) -> str:
    while True:
        answer = input(f"{question} [{'/'.join(choices)}]（預設：{default}）: ").strip()
        if not answer:
            return default
        if answer in choices:
            return answer
        print(f"請輸入其中一個：{', '.join(choices)}")


def _wizard_prompt_yes_no(question: str, *, default: bool) -> bool:
    hint = "Y/n" if default else "y/N"
    answer = input(f"{question} [{hint}]: ").strip().lower()
    if not answer:
        return default
    return answer in {"y", "yes"}


def _wizard_prompt_workspace_id(config: HarnessConfig, question: str) -> str:
    if config.memtrace_mcp_url:
        try:
            client = MemTraceClient(config.memtrace_mcp_url, config.memtrace_api_token)
            result = client.call_tool("list_workspaces", {})
            text = result.get("content", [{}])[0].get("text", "[]")
            workspaces = json.loads(text)
            if isinstance(workspaces, list) and workspaces:
                print("\n目前 MemTrace 裡看得到的 workspace：")
                for item in workspaces[:20]:
                    print(f"  {item.get('id')}  {item.get('name')}")
                print()
        except Exception:
            pass  # listing is a convenience, never block the wizard on it
    while True:
        answer = input(f"{question}（ws_ 開頭): ").strip()
        if answer.startswith("ws_"):
            return answer
        print("workspace_id 應該以 ws_ 開頭，再試一次。")


_ADVANCED_ROLE_ORDER = [
    ("controller", "Controller（調度)"),
    ("planner", "Planner（規劃)"),
    ("planner-escalation", "Planner-escalation（G1 推理缺口才啟用)"),
    ("red-team", "Red Team（G1/G2 審查)"),
    ("developer", "Developer（實際寫程式，唯一能寫入的角色)"),
]


def _wizard_configure_roles(text: str) -> str:
    """Advanced section: per-role provider/model, one role at a time. Only touches the
    primary provider/model line for each role — fallback chains and reasoning_effort
    stay whatever the file already has; edit the TOML directly for those."""
    for profile_id, label in _ADVANCED_ROLE_ORDER:
        change = _wizard_prompt_yes_no(f"要調整 {label} 的模型嗎？", default=False)
        if not change:
            continue
        provider_choice = _wizard_prompt_choice(
            "  廠商 1) codex  2) claude  3) antigravity", ["1", "2", "3"], default="1"
        )
        provider = {"1": "codex", "2": "claude", "3": "antigravity"}[provider_choice]
        model = _wizard_prompt(
            "  模型名稱（例如 gpt-5.6-sol / sonnet / gemini-3.6-flash-high)", default=""
        )
        if not model:
            print("  沒填模型名稱，跳過。")
            continue
        updated = _set_role_model_in_toml(text, profile_id, provider, model)
        if updated == text:
            print(f"  ⚠️  在檔案裡找不到 [profiles.{profile_id}] 區塊，跳過。")
        else:
            text = updated
            print(f"  已設定 {profile_id} = {provider}/{model}")
    return text


def _set_role_model_in_toml(text: str, profile_id: str, provider: str, model: str) -> str:
    """Replace only the primary provider/model lines inside one [profiles.<id>] block —
    stops at the next top-level [profiles.X] header, but a role's own
    [[profiles.<id>.fallbacks]] sub-blocks stay part of the same section since they
    don't match that boundary."""
    import re

    pattern = re.compile(
        rf'(\[profiles\.{re.escape(profile_id)}\]\n(?:(?!\[profiles\.).)*)', re.DOTALL
    )
    match = pattern.search(text)
    if not match:
        return text
    section = match.group(1)
    updated_section = re.sub(r'provider = "[^"]*"', f'provider = "{provider}"', section, count=1)
    updated_section = re.sub(r'model = "[^"]*"', f'model = "{model}"', updated_section, count=1)
    # Keep quota_bucket consistent with the new provider — the fallback-cooldown ledger
    # groups by this label, and leaving it pointing at the old vendor's bucket would
    # make cooldown accounting silently wrong, not just cosmetically stale.
    updated_section = re.sub(
        r'quota_bucket = "[^"]*"', f'quota_bucket = "{provider}-account"', updated_section, count=1
    )
    return text[: match.start()] + updated_section + text[match.end() :]


def _wizard_offer_restart() -> None:
    label = f"gui/{os.getuid()}/com.memtraceharness.gateway"
    restart = _wizard_prompt_yes_no("要現在重啟常駐服務讓設定生效嗎？", default=True)
    if not restart:
        print(f"記得之後手動重啟：launchctl kickstart -k {label}")
        return
    try:
        result = subprocess.run(
            ["launchctl", "kickstart", "-k", label], capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            print("已重啟常駐服務。")
        else:
            detail = result.stderr.strip() or f"exit code {result.returncode}"
            print(f"重啟失敗（{detail}）。可以自己手動跑：launchctl kickstart -k {label}")
    except Exception as exc:
        print(f"重啟失敗：{exc}。可以自己手動跑：launchctl kickstart -k {label}")


def _write_harness_scope(path: Path, name: str, workspace_id: str, off_limits: str) -> None:
    # default_risk_level intentionally not asked/written here: risk-level-driven
    # behavior belongs to the Agent Loop policy/KB layer, not something the wizard
    # should ask the operator to configure — see ProjectScope's own "medium" fallback.
    lines = [
        f"# Harness scope — {name}",
        "",
        f"- workspace_id: {workspace_id}",
        f"- working_directory: {path.parent}",
    ]
    if off_limits.strip():
        lines.append(f"- off_limits: {off_limits.strip()}")
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def _ensure_in_project_index(index_path: Path, scope_path: Path) -> None:
    existing_lines = (
        index_path.read_text(encoding="utf-8").splitlines() if index_path.is_file() else []
    )
    target = scope_path.resolve()
    already_present = any(
        line.strip() and not line.strip().startswith("#") and Path(line.strip()).resolve() == target
        for line in existing_lines
    )
    if already_present:
        return
    existing_lines.append(str(target))
    index_path.write_text("\n".join(existing_lines) + "\n", encoding="utf-8")


def _write_env_updates(env_path: Path, updates: dict[str, str]) -> None:
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.is_file() else []
    remaining = dict(updates)
    for i, line in enumerate(lines):
        if "=" not in line or line.strip().startswith("#"):
            continue
        key = line.split("=", 1)[0]
        if key in remaining:
            lines[i] = f"{key}={remaining.pop(key)}"
    for key, value in remaining.items():
        lines.append(f"{key}={value}")
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _slugify(name: str) -> str:
    import re

    return re.sub(r"[^A-Za-z0-9]+", "-", name).strip("-").lower() or "project"


def _collect_status_data(config: "HarnessConfig") -> dict:
    """Gather the harness's current diagnostic state as plain structured data (JSON-
    safe: only str/int/bool/list/dict). Read-only — never modifies process state,
    files, or the trace store. This is the single source of truth for both the CLI
    `status` command (rendered as text by _collect_status_text) and the web status
    dashboard (rendered as cards, pushed live over SSE as JSON) — they read the same
    shape so they never drift apart."""
    from memtrace_harness.config import (
        project_bot_token_env_var,
        project_chat_provider_env_var,
        project_memory_workspace_env_var,
        project_role_profiles_env_var,
    )

    trace_store = TraceStore(config.trace_db_path)
    projects = load_project_index(config.project_index_path)

    daemon: dict = {"pids": [], "multiple_warning": False, "pgrep_error": None, "launchd": None, "launchd_error": None}
    try:
        result = subprocess.run(
            ["pgrep", "-fl", "memtrace_harness gateway --serve"],
            capture_output=True, text=True, timeout=5,
        )
        daemon["pids"] = result.stdout.strip().splitlines()
        daemon["multiple_warning"] = len(daemon["pids"]) > 1
    except Exception as exc:
        daemon["pgrep_error"] = str(exc)

    try:
        result = subprocess.run(["launchctl", "list"], capture_output=True, text=True, timeout=5)
        matched = [line for line in result.stdout.splitlines() if "memtraceharness" in line]
        daemon["launchd"] = matched[0] if matched else None
    except Exception as exc:
        daemon["launchd_error"] = str(exc)

    project_entries = []
    for scope in projects:
        dedicated_bot = bool(os.getenv(project_bot_token_env_var(scope.name)))
        dedicated_chat = bool(os.getenv(project_chat_provider_env_var(scope.name)))
        dedicated_memory = bool(os.getenv(project_memory_workspace_env_var(scope.name)))
        dedicated_profiles = bool(os.getenv(project_role_profiles_env_var(scope.name)))

        session_id = f"psess_{scope.name}"
        turn_count = len(trace_store.get_primary_session_turns(session_id))
        pending_goal = len(trace_store.get_unconsolidated_turns(session_id))
        pending_pref = len(trace_store.get_unconsolidated_turns_for_preference(session_id))
        lock = trace_store.get_workspace_lock(scope.workspace_id)
        pending_approvals = trace_store.list_pending_approvals_for_workspace(scope.workspace_id)
        recent_turns = trace_store.get_recent_primary_session_turns(session_id, limit=5)

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

        role_profiles_entries: list[dict] | None = None
        role_profiles_error: str | None = None
        try:
            role_profiles = load_role_profiles(config.role_profiles_file_for(scope.name))
            role_profiles_entries = [
                {
                    "id": profile_id,
                    "provider": profile.provider,
                    "model": profile.model,
                    "fallbacks": [
                        {"provider": f.provider, "model": f.model} for f in profile.fallbacks
                    ],
                }
                for profile_id, profile in role_profiles.items()
            ]
        except Exception as exc:
            role_profiles_error = str(exc)

        project_entries.append(
            {
                "name": scope.name,
                "workspace_id": scope.workspace_id,
                "working_directory": str(scope.working_directory),
                "dedicated": {
                    "bot": dedicated_bot,
                    "chat_model": dedicated_chat,
                    "memory": dedicated_memory,
                    "role_profiles": dedicated_profiles,
                },
                "chat_candidates": [
                    {"provider": p, "model": m or None} for p, m in chat_candidates
                ],
                "role_profiles": role_profiles_entries,
                "role_profiles_error": role_profiles_error,
                "turn_count": turn_count,
                "pending_goal": pending_goal,
                "pending_pref": pending_pref,
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
            }
        )

    return {
        "daemon": daemon,
        "project_index_path": str(config.project_index_path) if config.project_index_path else None,
        "projects": project_entries,
    }


def _render_status_text(data: dict) -> str:
    """Render _collect_status_data()'s structure as the plain-text report the CLI
    `status` command prints."""
    lines: list[str] = []
    daemon = data["daemon"]

    lines.append("=== 常駐行程 ===")
    if daemon["pgrep_error"]:
        lines.append(f"  無法查詢行程：{daemon['pgrep_error']}")
    elif daemon["pids"]:
        for line in daemon["pids"]:
            lines.append(f"  {line}")
        if daemon["multiple_warning"]:
            lines.append("  ⚠️  找到不只一個 --serve 行程，同一個 bot 會搶 getUpdates 連線（409 Conflict）。")
    else:
        lines.append("  沒有正在跑的 gateway --serve 行程")

    if daemon["launchd_error"]:
        lines.append(f"  無法查詢 launchd：{daemon['launchd_error']}")
    else:
        lines.append("  launchd 註冊：" + (daemon["launchd"] or "沒有註冊 com.memtraceharness.gateway"))

    lines.append(f"\n=== 已註冊專案（{len(data['projects'])} 個，來自 {data['project_index_path']}）===")
    if not data["projects"]:
        lines.append("  （空的，或 HARNESS_PROJECT_INDEX 沒設定/找不到)")

    for project in data["projects"]:
        lines.append(f"\n  【{project['name']}】 workspace_id={project['workspace_id']}")
        lines.append(f"    working_directory: {project['working_directory']}")
        dedicated = project["dedicated"]
        lines.append(
            "    專屬設定："
            f"bot={'有' if dedicated['bot'] else '共用全域'}, "
            f"chat model={'有' if dedicated['chat_model'] else '共用全域'}, "
            f"memory workspace={'有' if dedicated['memory'] else '共用全域'}, "
            f"role-profiles={'有' if dedicated['role_profiles'] else '用預設'}"
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
            f"（目標分類待處理 {project['pending_goal']}、偏好分類待處理 {project['pending_pref']}）"
        )
        if project["recent_turns"]:
            lines.append("    最近對話：")
            for turn in project["recent_turns"]:
                content_preview = turn["content"].replace("\n", " ")[:80]
                lines.append(f"      [{turn['created_at']}] {turn['speaker']}: {content_preview}")
        if project["lock"]:
            lock = project["lock"]
            lines.append(f"    ⚠️  workspace 鎖定中：conversation_id={lock['conversation_id']}, locked_at={lock['locked_at']}")
            turn = lock["latest_turn"]
            if turn:
                lines.append(
                    f"       最新進度：{turn['stage']}/{turn['profile_id']} "
                    f"({turn['provider']}/{turn['model'] or '?'}) state={turn['state']} @ {turn['created_at']}"
                )
            else:
                lines.append("       最新進度：尚未有階段完成（可能剛啟動或卡在第一個階段）")
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


def _collect_status_text(config: "HarnessConfig") -> str:
    """Gather the same diagnostic report status_command prints, as plain text.
    Read-only — never modifies process state, files, or the trace store."""
    return _render_status_text(_collect_status_data(config))


def _collect_turn_detail(config: "HarnessConfig", turn_id: int) -> dict | None:
    """One Agent Loop stage attempt's full detail (artifact + model's final text) for
    the status dashboard's on-demand "click a stage" view — deliberately not part of
    _collect_status_data()/the SSE push, since artifact/final_text bodies can be large
    and most stages in a pipeline are never clicked. Read-only."""
    trace_store = TraceStore(config.trace_db_path)
    return trace_store.get_turn_detail(turn_id)


def status_command(args: argparse.Namespace) -> int:
    """One-shot diagnostic: what's actually running, what's registered, and what's
    stuck. Read-only — never modifies process state, files, or the trace store."""
    config = HarnessConfig.from_env()
    print(_collect_status_text(config))
    return 0


def remove_project_command(args: argparse.Namespace) -> int:
    """Unregister a project: remove it from projects.index.txt and, if the operator
    confirms, its per-project .env overrides. Deliberately never touches the target
    project's own harness-scope.md (that file belongs to that repo, not the Harness),
    and never deletes trace_store history (that's an audit trail, not live state)."""
    from memtrace_harness.config import (
        project_bot_token_env_var,
        project_chat_fallbacks_env_var,
        project_chat_model_env_var,
        project_chat_provider_env_var,
        project_memory_workspace_env_var,
        project_role_profiles_env_var,
    )

    config = HarnessConfig.from_env()
    trace_store = TraceStore(config.trace_db_path)
    projects = load_project_index(config.project_index_path)
    if not projects:
        print("沒有已註冊的專案可以移除。")
        return 1

    name = args.name
    if not name:
        print("已註冊的專案：")
        for scope in projects:
            print(f"  {scope.name}  (workspace_id={scope.workspace_id})")
        name = input("要移除哪一個？輸入專案名稱: ").strip()

    matched = next((s for s in projects if s.name == name), None)
    if not matched:
        print(f"找不到專案 {name!r}。", file=sys.stderr)
        return 1

    lock = trace_store.get_workspace_lock(matched.workspace_id)
    pending_approvals = trace_store.list_pending_approvals_for_workspace(matched.workspace_id)
    if lock or pending_approvals:
        print(f"⚠️  「{name}」目前還有進行中的狀態：")
        if lock:
            print(f"  - workspace 鎖定中：conversation_id={lock['conversation_id']}")
        if pending_approvals:
            print(f"  - {len(pending_approvals)} 筆待核准請求")
        print("移除註冊不會清掉這些狀態，之後如果又用同一個 workspace_id 註冊回來，鎖定/待核准可能還在。")

    if not args.yes:
        confirm = input(f"確定要移除「{name}」的註冊嗎？(y/N): ").strip().lower()
        if confirm not in {"y", "yes"}:
            print("已取消。")
            return 0

    index_path = config.project_index_path or Path("projects.index.txt")
    _remove_from_project_index(index_path, matched.scope_file_path)
    print(f"已從 {index_path} 移除。")
    print(f"（{matched.scope_file_path} 本身沒有被刪除——那是該專案 repo 自己的檔案。)")

    env_keys = [
        project_bot_token_env_var(name),
        project_chat_provider_env_var(name),
        project_chat_model_env_var(name),
        project_chat_fallbacks_env_var(name),
        project_memory_workspace_env_var(name),
        project_role_profiles_env_var(name),
    ]
    env_path = Path(".env")
    existing_lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.is_file() else []
    present_keys = [
        key for key in env_keys if any(line.split("=", 1)[0] == key for line in existing_lines if "=" in line)
    ]
    if present_keys:
        print(f"\n{env_path} 裡還有這個專案的專屬設定：")
        for key in present_keys:
            print(f"  {key}")
        remove_env = args.yes or input("要一併移除嗎？(y/N): ").strip().lower() in {"y", "yes"}
        if remove_env:
            _remove_env_keys(env_path, present_keys)
            print("已移除。")
        else:
            print("保留不動。")

    print()
    _wizard_offer_restart()
    return 0


def _remove_from_project_index(index_path: Path, scope_file_path: Path) -> None:
    if not index_path.is_file():
        return
    target = scope_file_path.resolve()
    lines = index_path.read_text(encoding="utf-8").splitlines()
    kept = [
        line
        for line in lines
        if not (line.strip() and not line.strip().startswith("#") and Path(line.strip()).resolve() == target)
    ]
    index_path.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")


def _remove_env_keys(env_path: Path, keys: list[str]) -> None:
    lines = env_path.read_text(encoding="utf-8").splitlines()
    kept = [line for line in lines if not ("=" in line and line.split("=", 1)[0] in keys)]
    env_path.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
