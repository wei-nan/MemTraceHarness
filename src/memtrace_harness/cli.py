from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
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
from memtrace_harness.scope import ProjectScope, load_repos
from memtrace_harness.trace_store import TraceStore
from memtrace_harness.workspace import (
    ENV_FILE_NAME,
    SCOPE_FILE_NAME,
    WorkspaceError,
    load_workspace,
    workspace_paths,
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
        if args.command == "repo":
            return repo_command(args)
        if args.command == "memory-topics":
            return memory_topics_command(args)
        if args.command == "memory-reorg":
            return memory_reorg_command(args)
        if args.command == "memory-digest":
            return memory_digest_command(args)
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
    scope = ProjectScope.from_file(path)
    scope.repos = load_repos(workspace_paths(config.workspace_root)["repos"])
    return scope


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
    migrate_parser.add_argument(
        "--primary", action="append",
        help="When projects share a MemTrace workspace they become one Harness; name the project that is its primary repo (default: the first in the index). Repeatable.",
    )
    migrate_parser.add_argument("--first-port", type=int, default=8800, help="First Harness API port to hand out (use a higher one when migrating a second legacy instance)")
    migrate_parser.add_argument("--apply", action="store_true", help="Write the files (default: only print the plan)")

    subparsers.add_parser(
        "status",
        help="Show this Harness's projects, locks, queue and pending approvals",
    )
    subparsers.add_parser(
        "doctor", help="Check the workspace: env file, API token, scope file, tools the models need"
    )

    repo_parser = subparsers.add_parser(
        "repo", help="The other repos this Harness works in besides its primary one (.harness/repos.toml)"
    )
    repo_sub = repo_parser.add_subparsers(dest="repo_action", required=True)
    repo_sub.add_parser("list", help="Show the primary repo and the others")
    repo_add = repo_sub.add_parser("add", help="Declare another repo of this project")
    repo_add.add_argument("name", help="Short name used to aim a task at it (repo=<name>::...)")
    repo_add.add_argument("path", type=Path, help="Where it is checked out on this machine")
    repo_add.add_argument("--verify", help="Command that must pass before a change in it is reviewed")
    repo_add.add_argument("--verify-timeout", type=int, help="Seconds that command may take (default 1200)")
    repo_add.add_argument("--setup", help="Command to make a fresh worktree of it usable")
    repo_add.add_argument("--github", help="owner/repo, to scan its open issues as backlog")
    repo_add.add_argument("--off-limits", help="Comma-separated phrases that are off-limits in it")
    repo_remove = repo_sub.add_parser("remove", help="Stop treating a folder as a repo of this project")
    repo_remove.add_argument("name")

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

    topics_parser = subparsers.add_parser(
        "memory-topics",
        help=(
            "Split finished days of the conversation into matters and write one MemTrace note per "
            "matter (the nightly archive; use this to catch up on older days). Oldest day first."
        ),
    )
    topics_parser.add_argument("--date", help="Only this local date (YYYY-MM-DD), even if already archived")
    topics_parser.add_argument(
        "--days", type=int, default=14, help="Catch up on the days not yet archived within this many days (default 14)"
    )
    topics_parser.add_argument("--dry-run", action="store_true", help="Only list the days that would be archived")
    topics_parser.add_argument(
        "--rebuild",
        action="store_true",
        help=(
            "Derive ALL matters again from the first day of the local log (current prompts and models); "
            "the old matters' notes go to the MemTrace trash only after the new set is written. "
            "An interrupted rebuild resumes when you run this again."
        ),
    )

    reorg_parser = subparsers.add_parser(
        "memory-reorg",
        help=(
            "Reorganise the Harness's own memory notes by hand: --review (merge / retitle / resolve "
            "matters), --retire-drafts (old hourly drafts now covered by matters), --undo RUN."
        ),
    )
    reorg_parser.add_argument("--review", action="store_true", help="Run the matter review now (ignores the weekly schedule)")
    reorg_parser.add_argument("--retire-drafts", action="store_true", help="Move covered old drafts to the MemTrace trash")
    reorg_parser.add_argument("--undo", type=int, metavar="RUN", help="Undo a reorganisation run (see the run id in its report)")
    reorg_parser.add_argument("--dry-run", action="store_true", help="With --review: only print the proposed changes")

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


def memory_topics_command(args: argparse.Namespace) -> int:
    from memtrace_harness.duties import archive_topics, make_archive_model_caller
    from memtrace_harness.memory_topics import due_topic_dates

    config, scope = _config_and_scope()
    trace_store = TraceStore(config.trace_db_path)
    tz = ZoneInfo(config.schedule_timezone)
    if args.rebuild:
        return _rebuild_matters(args, config, scope, trace_store, tz)
    if args.date:
        datetime.strptime(args.date, "%Y-%m-%d")
        dates = [args.date]
    else:
        dates = due_topic_dates(
            trace_store, scope.name, f"psess_{scope.name}", tz, datetime.now(timezone.utc), max_days_back=args.days
        )
    if args.dry_run:
        for d in dates:
            print(f"[{scope.name}] would archive {d} into matters")
        print(f"dry run: {len(dates)} day(s), nothing was changed (drop --dry-run to run them)")
        return 0
    if not config.memtrace_mcp_url:
        raise RuntimeError("MEMTRACE_MCP_URL is required")
    client = MemTraceClient(config.memtrace_mcp_url, config.memtrace_api_token)
    workspace_id = config.memory_workspace_id_for(scope.name, scope.workspace_id)
    recorded = archive_topics(
        trace_store, client, scope, workspace_id, tz, make_archive_model_caller(config, scope.name), dates
    )
    for d in dates:
        trace_store.mark_digest_edges_unsynced(scope.name, d)
    print(f"{recorded} matter(s) recorded for {len(dates)} day(s)")
    return 0


def _rebuild_matters(args, config, scope, trace_store, tz) -> int:
    from memtrace_harness.duties import rebuild_matters

    workspace_id = config.memory_workspace_id_for(scope.name, scope.workspace_id)
    if trace_store.running_reorg_run(scope.name, workspace_id, "rebuild") is None:
        days = len(set(_log_days(trace_store, scope.name, tz)))
        print(f"[{scope.name}] a rebuild derives matters again for {days} day(s) of the local log (one model call each).")
    if args.dry_run:
        print("dry run: nothing was changed (drop --dry-run to rebuild)")
        return 0
    if not config.memtrace_mcp_url:
        raise RuntimeError("MEMTRACE_MCP_URL is required")
    client = MemTraceClient(config.memtrace_mcp_url, config.memtrace_api_token)
    outcome = rebuild_matters(config, trace_store, client, scope)
    if outcome is None:
        return 1
    print(outcome.lines[0] + (f"；{outcome.failed} 個舊節點移除失敗" if outcome.failed else ""))
    print(f"undo with: memory-reorg --undo {outcome.run_id}")
    return 0


def _log_days(trace_store, project: str, tz):
    from memtrace_harness.memory_digest import turns_by_local_date

    return turns_by_local_date(trace_store.get_primary_session_turns(f"psess_{project}"), tz).keys()


def memory_reorg_command(args: argparse.Namespace) -> int:
    from memtrace_harness.duties import make_archive_model_caller
    from memtrace_harness.memory_reorg import describe_reorg, retire_covered_drafts, run_review_pass, undo_reorg_run

    config, scope = _config_and_scope()
    if not (args.review or args.retire_drafts or args.undo):
        print("choose --review, --retire-drafts or --undo RUN", file=sys.stderr)
        return 2
    if not config.memtrace_mcp_url:
        raise RuntimeError("MEMTRACE_MCP_URL is required")
    trace_store = TraceStore(config.trace_db_path)
    client = MemTraceClient(config.memtrace_mcp_url, config.memtrace_api_token)
    tz = ZoneInfo(config.schedule_timezone)
    workspace_id = config.memory_workspace_id_for(scope.name, scope.workspace_id)
    if args.undo:
        done, failed = undo_reorg_run(trace_store, client, args.undo)
        print(f"undone {done} op(s), {failed} failed")
        return 1 if failed else 0
    if args.retire_drafts:
        outcome = retire_covered_drafts(trace_store, client, scope.name, workspace_id, tz)
        print(describe_reorg(outcome) if outcome else "no old draft is fully covered by matters yet")
        if outcome:
            print(f"undo with: memory-reorg --undo {outcome.run_id}")
    if args.review:
        outcome = run_review_pass(
            trace_store, client, scope.name, workspace_id, make_archive_model_caller(config, scope.name),
            force=True, plan_only=args.dry_run,
        )
        if outcome is None:
            print("too few matters to review")
        else:
            print(describe_reorg(outcome) if outcome.lines else "the model proposed no change")
            if args.dry_run:
                print("dry run: nothing was changed")
            elif outcome.count:
                print(f"undo with: memory-reorg --undo {outcome.run_id}")
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
    from memtrace_harness.workspace import env_file_problems, global_secrets_problems

    config = HarnessConfig.from_env()
    problems: list[str] = []
    if config.workspace_root is None:
        raise ValueError("no workspace: run inside a folder that has a .harness.env, or pass -C <folder>")
    problems += env_file_problems(config.workspace_root)
    problems += global_secrets_problems()
    if not config.api_token:
        problems.append("HARNESS_API_TOKEN is not set; the API (and so every service) cannot start")
    if not config.memtrace_mcp_url:
        problems.append("MEMTRACE_MCP_URL is not set; the Harness has no knowledge base")
    scope: ProjectScope | None = None
    try:
        scope = load_scope(config)
    except ValueError as exc:
        problems.append(str(exc))
    if scope is not None:
        print(f"project: {scope.name} (MemTrace workspace {scope.workspace_id})")
        for name, repo in scope.repos.items():
            print(f"repo: {name} -> {repo.path}")
            if not repo.path.is_dir():
                problems.append(f"repo {name}: {repo.path} is not a folder")
            elif repo.path.resolve() == scope.working_directory.resolve():
                problems.append(f"repo {name} is the primary repo's own folder")
        problems += check_chat_tools(config, [scope.name])
    for problem in problems:
        print(f"PROBLEM: {problem}")
    if not problems:
        print("OK: nothing to fix")
    return 1 if problems else 0


def repo_command(args: argparse.Namespace) -> int:
    """List, add or remove the other repos this Harness works in (`.harness/repos.toml`)."""
    from memtrace_harness.scope import REPO_NAME, RepoScope, dump_repos

    config, scope = _config_and_scope()
    path = workspace_paths(config.workspace_root)["repos"]
    repos = dict(scope.repos)
    if args.repo_action == "list":
        print(f"{scope.name} (primary): {scope.working_directory}")
        for name, repo in repos.items():
            extras = [f"verify: {repo.verify_command}"] if repo.verify_command else []
            extras += [f"github: {repo.github_repo}"] if repo.github_repo else []
            print(f"{name}: {repo.path}" + (f"  ({'; '.join(extras)})" if extras else ""))
        return 0
    if args.repo_action == "remove":
        if args.name not in repos:
            raise ValueError(f"no repo named {args.name!r}")
        del repos[args.name]
    else:  # add
        if not REPO_NAME.match(args.name):
            raise ValueError(f"{args.name!r} is not a usable repo name (letters, digits, - and _)")
        if args.name.lower() in {"primary", scope.name.lower()} or args.name in repos:
            raise ValueError(f"{args.name!r} is already a repo of this project")
        directory = args.path.expanduser().resolve()
        if not directory.is_dir():
            raise ValueError(f"{directory} is not a folder")
        if directory == scope.working_directory.resolve():
            raise ValueError("that is the primary repo's own folder")
        repos[args.name] = RepoScope(
            name=args.name,
            path=directory,
            verify_command=args.verify,
            verify_timeout_seconds=args.verify_timeout,
            worktree_setup_command=args.setup,
            github_repo=args.github,
            off_limits=[x.strip() for x in (args.off_limits or "").split(",") if x.strip()],
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump_repos(repos), encoding="utf-8")
    print(f"{path} updated; the running Harness picks it up at its next restart.")
    return 0


def _deduplicate(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


if __name__ == "__main__":
    raise SystemExit(main())
