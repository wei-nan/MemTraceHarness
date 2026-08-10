from __future__ import annotations

import argparse
import json
import logging
import signal
import time
from pathlib import Path
import sys
from uuid import uuid4

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
        default=60,
        help="Minimum seconds between unattended backlog scan passes in --serve mode (default 60).",
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

    subparsers.add_parser(
        "scan", help="Run a single pass of the unattended backlog scanner"
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
    context_refs = list(args.context_ref)
    context_items = []
    if args.hydrate_context:
        assert memtrace_client is not None
        context_items = memtrace_client.hydrate_context_refs(
            workspace_id=args.workspace,
            refs=context_refs,
            max_response_tokens=args.context_max_tokens,
        )

    task = TaskEnvelope(
        task_id=f"task_{uuid4().hex[:12]}",
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
    context_refs = list(args.context_ref)
    context_items = []
    if args.hydrate_context:
        assert memtrace_client is not None
        context_items = memtrace_client.hydrate_context_refs(
            workspace_id=args.workspace,
            refs=context_refs,
            max_response_tokens=args.context_max_tokens,
        )
    return TaskEnvelope(
        task_id=f"task_{uuid4().hex[:12]}",
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
            poll_timeout=max(1, args.poll_timeout_seconds),
            scan_interval=max(1, args.scan_interval_seconds),
            consolidation_interval=max(1, args.consolidation_interval_seconds),
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
    *,
    poll_timeout: int,
    scan_interval: int,
    consolidation_interval: int,
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
        f"consolidation_interval={consolidation_interval}s. Ctrl-C to stop."
    )
    stop_requested = False

    def _handle_stop(signum: int, frame: object) -> None:
        nonlocal stop_requested
        stop_requested = True

    signal.signal(signal.SIGINT, _handle_stop)
    signal.signal(signal.SIGTERM, _handle_stop)

    last_scan = 0.0
    last_consolidation = 0.0
    while not stop_requested:
        for gw in gateways:
            if stop_requested:
                break
            label = ", ".join(p.name for p in gw.projects) or "unassigned"
            try:
                processed = gw.poll_once(timeout=poll_timeout)
                if processed:
                    print(f"[{label}] processed {processed} update(s)")
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
            except Exception:
                logger.exception("scan pass failed; continuing")
            last_scan = now

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

    print("Gateway serve loop stopped.")
    return 0


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


if __name__ == "__main__":
    raise SystemExit(main())
