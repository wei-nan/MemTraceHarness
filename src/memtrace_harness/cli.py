from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path
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
from memtrace_harness.loop import AgentLoopRunner
from memtrace_harness.memory_digest import AUTO_CATCH_UP_DAYS
from memtrace_harness.memtrace_client import MemTraceClient
from memtrace_harness.role_profiles import load_role_profiles
from memtrace_harness.runner import HarnessRunner
from memtrace_harness.schemas import TaskEnvelope
from memtrace_harness.scope import ProjectScope
from memtrace_harness.trace_store import TraceStore
from memtrace_harness.workspace import (
    ENV_FILE_NAME,
    SCOPE_FILE_NAME,
    WorkspaceError,
    load_workspace,
)

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
        if args.command == "init-workspace":
            from memtrace_harness.init_workspace import init_workspace_command

            return init_workspace_command(args)
        if args.command == "migrate":
            from memtrace_harness.migrate import migrate_command

            return migrate_command(args)
        if args.command:
            _load_workspace_for(args)
        if args.command == "run":
            return run_command(args)
        if args.command == "probe":
            return probe_command(args)
        if args.command == "loop":
            return loop_command(args)
        if args.command == "serve":
            return serve_command(args)
        if args.command == "scan":
            return scan_command(args)
        if args.command == "status":
            return status_command(args)
        if args.command == "doctor":
            return doctor_command(args)
        if args.command == "memory-digest":
            return memory_digest_command(args)
        if args.command == "memory-drafts":
            return memory_drafts_command(args)
    except (OSError, RuntimeError, ValueError, WorkspaceError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    parser.print_help()
    return 2


def _load_workspace_for(args: argparse.Namespace) -> Path | None:
    """Put the workspace's `.harness.env` in the environment. The workspace is `-C DIR`, else the
    current directory when it holds a `.harness.env`; a command that needs one (serve, scan, status,
    ...) says so itself, the one-off `run`/`probe`/`loop` work without."""
    chosen = getattr(args, "workspace_dir", None)
    if chosen is None and (Path.cwd() / ENV_FILE_NAME).is_file():
        chosen = Path.cwd()
    if chosen is None:
        return None
    return load_workspace(chosen)


def load_scope(config: HarnessConfig) -> ProjectScope:
    """The project this Harness serves: `harness-scope.md` in its workspace."""
    if config.workspace_root is None:
        raise ValueError(
            "no workspace: run inside a folder that has a .harness.env, or pass -C <folder> "
            "(`memtrace-harness init-workspace <folder>` creates one)"
        )
    path = config.workspace_root / SCOPE_FILE_NAME
    if not path.is_file():
        raise ValueError(f"{path} does not exist; init-workspace writes it, or create it by hand")
    return ProjectScope.from_file(path)


def _config_and_scope() -> tuple[HarnessConfig, ProjectScope]:
    config = HarnessConfig.from_env()
    return config, load_scope(config)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="memtrace-harness",
        description="Run Claude, Codex, and Antigravity through local CLIs only.",
    )
    parser.add_argument(
        "-C",
        "--workspace-dir",
        type=Path,
        help="The Harness workspace (a folder holding .harness.env); default: the current folder if it has one",
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

    serve_parser = subparsers.add_parser(
        "serve",
        help="Run this workspace's Harness: its housekeeping plus the local API services connect to",
    )
    serve_parser.add_argument(
        "--scan-interval-seconds",
        type=int,
        default=1800,
        help="Minimum seconds between unattended backlog scan passes (default 1800). "
        "Each pass costs one search_nodes call even when the backlog is empty — keep this "
        "well above a few minutes or idle scanning dominates MemTrace token spend (was 60s, see "
        "ws_spec_plan/mem_c9c4affd).",
    )
    serve_parser.add_argument(
        "--consolidation-interval-seconds",
        type=int,
        default=3600,
        help=(
            "Minimum seconds between cold-memory consolidation passes (hot primary-session "
            "turns -> draft MemTrace nodes) (default 3600)."
        ),
    )
    serve_parser.add_argument(
        "--schedule-check-interval-seconds",
        type=int,
        default=30,
        help=(
            "Minimum seconds between checks for due chat-created schedules "
            "(see HARNESS_SCHEDULE_START in the chat) (default 30). "
            "Cheap (a single indexed SQLite query), safe to keep short."
        ),
    )

    subparsers.add_parser(
        "scan", help="Run a single pass of the unattended backlog scanner"
    )

    init_parser = subparsers.add_parser(
        "init-workspace",
        help="Create (or finish) a workspace: .harness.env, .harness/, harness-scope.md",
    )
    init_parser.add_argument("directory", nargs="?", type=Path, help="The workspace folder (may be empty, need not be a git repo)")
    init_parser.add_argument("--name", help="Project name (default: the folder name)")
    init_parser.add_argument("--memtrace-workspace", help="MemTrace workspace id (ws_...) of the project's spec/backlog")
    init_parser.add_argument("--non-interactive", action="store_true", help="Ask nothing; use flags and defaults")

    migrate_parser = subparsers.add_parser(
        "migrate",
        help="Move a legacy .env + projects.index.txt Harness into per-workspace .harness.env files",
    )
    migrate_parser.add_argument("--env", type=Path, default=Path(".env"), help="The legacy Harness env file")
    migrate_parser.add_argument("--index", type=Path, help="The legacy project index (default: from the env file)")
    migrate_parser.add_argument("--services-dir", type=Path, help="Where to write each service's env file (default ~/.config/memtraceharness)")
    migrate_parser.add_argument("--first-port", type=int, default=8800, help="First Harness API port to hand out (use a higher one when migrating a second legacy instance)")
    migrate_parser.add_argument("--apply", action="store_true", help="Write the files (default: only print the plan)")

    subparsers.add_parser(
        "status",
        help="Show this Harness's projects, locks, queue and pending approvals",
    )
    subparsers.add_parser(
        "doctor", help="Check the workspace: env file, API token, scope file, tools the models need"
    )

    digest_parser = subparsers.add_parser(
        "memory-digest",
        help=(
            "Run the nightly memory digest (sleep cycle) now: consolidate finished days "
            "into digests, and adopt or retire the operator's standing preferences from them"
        ),
    )
    digest_parser.add_argument("--project", help="Only this project (default: every registered project)")
    digest_parser.add_argument(
        "--date", help="Only this local date (YYYY-MM-DD); replaces an existing digest for it"
    )
    digest_parser.add_argument(
        "--backfill",
        action="store_true",
        help=(
            f"Digest every past day that has none yet, not just the last "
            f"{AUTO_CATCH_UP_DAYS} days (one model call per project-day)"
        ),
    )
    digest_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List the project-days that would be digested, without any model call",
    )

    drafts_parser = subparsers.add_parser(
        "memory-drafts",
        help=(
            "Rename the old hourly archive nodes in MemTrace (all titled "
            "'Draft primary session consolidation: <project>') by date and turn range, and "
            "record which node holds which turns. Dry run unless --apply."
        ),
    )
    drafts_parser.add_argument("--project", help="Only this project (default: every registered project)")
    drafts_parser.add_argument(
        "--apply", action="store_true", help="Actually rename the nodes (default: only print the plan)"
    )
    drafts_parser.add_argument(
        "--plan-file", type=Path, help="Also write the full old -> new plan to this markdown file"
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


def memory_drafts_command(args: argparse.Namespace) -> int:
    from memtrace_harness.memory_drafts import apply_draft_retitle, plan_draft_retitle

    config, scope = _config_and_scope()
    if not config.memtrace_mcp_url:
        raise RuntimeError("MEMTRACE_MCP_URL is required")
    trace_store = TraceStore(config.trace_db_path)
    client = MemTraceClient(config.memtrace_mcp_url, config.memtrace_api_token)
    tz = ZoneInfo(config.schedule_timezone)
    report: list[str] = []
    total = 0
    workspace_id = config.memory_workspace_id_for(scope.name, scope.workspace_id)
    plans, skipped = plan_draft_retitle(client, trace_store, scope.name, workspace_id, tz)
    print(f"[{scope.name}] {workspace_id}: {len(plans)} draft(s) to rename, {len(skipped)} skipped")
    report.append(f"## {scope.name} — `{workspace_id}` ({len(plans)} to rename, {len(skipped)} skipped)\n")
    report += [f"- `{p.node_id}` → {p.new_title}" for p in plans]
    report += [f"- `{k.node_id}` **skipped**: {k.reason}" for k in skipped]
    report.append("")
    for p in plans[:3] + plans[-1:]:
        print(f"    {p.node_id}  →  {p.new_title}")
    for k in skipped:
        print(f"    skipped {k.node_id}: {k.reason}")
    if args.apply and plans:
        total += apply_draft_retitle(client, trace_store, scope.name, workspace_id, plans)
    if args.plan_file:
        args.plan_file.write_text("\n".join(report), encoding="utf-8")
        print(f"full plan written to {args.plan_file}")
    print(f"renamed {total} node(s)" if args.apply else "dry run: nothing was changed (use --apply)")
    return 0


def memory_digest_command(args: argparse.Namespace) -> int:
    from memtrace_harness.duties import run_memory_digests

    config, scope = _config_and_scope()
    trace_store = TraceStore(config.trace_db_path)
    if args.date:
        datetime.strptime(args.date, "%Y-%m-%d")
    memtrace_client = (
        MemTraceClient(config.memtrace_mcp_url, config.memtrace_api_token)
        if config.memtrace_mcp_url and not args.dry_run
        else None
    )
    changed = run_memory_digests(
        config,
        trace_store,
        memtrace_client,
        [scope],
        max_days_back=None if args.backfill else AUTO_CATCH_UP_DAYS,
        only_date=args.date,
        dry_run=args.dry_run,
    )
    if not args.dry_run:
        adopted = sum(len(c.adopted) for c in changed.values())
        retired = sum(len(c.retired) for c in changed.values())
        waiting = sum(len(c.waiting) for c in changed.values())
        print(
            f"preferences adopted: {adopted}, retired: {retired}, waiting for review: {waiting} "
            "(adjust them on the status page)"
        )
    return 0


def serve_command(args: argparse.Namespace) -> int:
    from memtrace_harness.runtime import serve

    config, scope = _config_and_scope()
    return serve(
        config,
        scope,
        scan_interval=max(1, args.scan_interval_seconds),
        consolidation_interval=max(1, args.consolidation_interval_seconds),
        schedule_check_interval=max(1, args.schedule_check_interval_seconds),
    )


def scan_command(args: argparse.Namespace) -> int:
    from memtrace_harness.runtime import build_harness

    config, scope = _config_and_scope()
    parts = build_harness(config, scope)
    results = parts.scanner.run_scan_pass()
    print(json.dumps(results, indent=2))
    return 0


def status_command(args: argparse.Namespace) -> int:
    """One-shot diagnostic: what this Harness has registered, running and waiting on. Read-only —
    never modifies the trace store."""
    from memtrace_harness.admin import collect_status_text

    config, scope = _config_and_scope()
    print(collect_status_text(config, scope))
    return 0


def doctor_command(args: argparse.Namespace) -> int:
    """Check what keeps a Harness from working before it is started."""
    from memtrace_harness.chat_tools_check import check_chat_tools
    from memtrace_harness.workspace import env_file_problems

    config = HarnessConfig.from_env()
    problems: list[str] = []
    if config.workspace_root is None:
        raise ValueError("no workspace: run inside a folder that has a .harness.env, or pass -C <folder>")
    problems += env_file_problems(config.workspace_root)
    if not config.api_token:
        problems.append("HARNESS_API_TOKEN is not set; the API (and so every service) cannot start")
    if not config.memtrace_mcp_url:
        problems.append("MEMTRACE_MCP_URL is not set; the Harness has no knowledge base")
    try:
        scope = load_scope(config)
        print(f"project: {scope.name} (MemTrace workspace {scope.workspace_id})")
        problems += check_chat_tools(config, [scope.name])
    except ValueError as exc:
        problems.append(str(exc))
    for problem in problems:
        print(f"PROBLEM: {problem}")
    if not problems:
        print("OK: nothing to fix")
    return 1 if problems else 0


def _deduplicate(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


if __name__ == "__main__":
    raise SystemExit(main())
