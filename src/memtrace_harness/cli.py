from __future__ import annotations

import argparse
import json
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

    subparsers.add_parser(
        "gateway", help="Start the persistent Telegram gateway and unattended scanner"
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
    if not config.telegram_bot_token:
        raise RuntimeError("HARNESS_TELEGRAM_BOT_TOKEN is required to run the gateway")
    trace_store = TraceStore(config.trace_db_path)
    approval_mgr = ApprovalManager(trace_store, config.telegram_allowed_chat_ids)
    projects = load_project_index(config.project_index_path)
    triage = ChatTriage(projects)
    memtrace_client = MemTraceClient(config.memtrace_mcp_url, config.memtrace_api_token) if config.memtrace_mcp_url else None
    primary_session_mgr = PrimarySessionManager(trace_store, memtrace_client)
    gateway = TelegramGateway(config, approval_mgr, triage, primary_session_mgr, projects)
    scanner = UnattendedScanner(config, trace_store, projects, memtrace_client, gateway, approval_mgr)

    print("Starting Telegram gateway and unattended scanner...")
    processed = gateway.poll_once()
    scan_results = scanner.run_scan_pass()
    print(f"Processed {processed} Telegram updates; scan pass results: {scan_results}")
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
