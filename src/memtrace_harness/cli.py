from __future__ import annotations

import argparse
import json
import sys
from uuid import uuid4

from memtrace_harness.adapters import MockModelAdapter
from memtrace_harness.config import HarnessConfig
from memtrace_harness.memtrace_client import MemTraceClient
from memtrace_harness.runner import HarnessRunner
from memtrace_harness.schemas import TaskEnvelope
from memtrace_harness.trace_store import TraceStore


DEFAULT_CONSTRAINTS = [
    "Do not automatically resolve MemTrace inquiries.",
    "Do not promote model consensus into formal knowledge without human review.",
    "Every claim should include evidence references when available.",
]

DEFAULT_DONE_WHEN = [
    "Draft claims are produced.",
    "Potential conflicts or human-gate needs are listed.",
    "A recommendation is generated.",
]


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "run":
        return run_command(args)
    parser.print_help()
    return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="memtrace-harness")
    subparsers = parser.add_subparsers(dest="command")

    run_parser = subparsers.add_parser("run", help="Run the v1 harness workflow")
    run_parser.add_argument("--workspace", required=True, help="MemTrace workspace id")
    run_parser.add_argument("--goal", required=True, help="Task goal")
    run_parser.add_argument(
        "--context-ref",
        action="append",
        default=[],
        help="MemTrace node id or external context reference. Can be repeated.",
    )
    run_parser.add_argument(
        "--risk-level",
        choices=["low", "medium", "high"],
        default="medium",
        help="Risk level for the task envelope",
    )
    run_parser.add_argument(
        "--writeback",
        action="store_true",
        help="Create a draft inquiry node in MemTrace. Defaults to dry-run only.",
    )
    run_parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON instead of a compact text summary.",
    )
    return parser


def run_command(args: argparse.Namespace) -> int:
    config = HarnessConfig.from_env()
    trace_store = TraceStore(config.trace_db_path)
    memtrace_client = None
    if args.writeback:
        if not config.memtrace_mcp_url:
            print("MEMTRACE_MCP_URL is required when --writeback is set", file=sys.stderr)
            return 2
        memtrace_client = MemTraceClient(config.memtrace_mcp_url, config.memtrace_api_token)

    task = TaskEnvelope(
        task_id=f"task_{uuid4().hex[:12]}",
        workspace_id=args.workspace,
        goal=args.goal,
        context_refs=list(args.context_ref),
        constraints=DEFAULT_CONSTRAINTS,
        done_when=DEFAULT_DONE_WHEN,
        risk_level=args.risk_level,
    )
    runner = HarnessRunner(
        adapters=[
            MockModelAdapter("mock-planner", "Planner"),
            MockModelAdapter("mock-reviewer", "Evidence Judge"),
        ],
        trace_store=trace_store,
        memtrace_client=memtrace_client,
    )
    summary = runner.run(task, writeback=args.writeback)

    if args.json:
        print(json.dumps(summary.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(f"trace_id: {summary.trace_id}")
        print(f"run_mode: {summary.run_mode}")
        print(f"recommendation: {summary.recommendation}")
        if summary.writeback_node_id:
            print(f"writeback_node_id: {summary.writeback_node_id}")
        print(f"trace_db: {config.trace_db_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
