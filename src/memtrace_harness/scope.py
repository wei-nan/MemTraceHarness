from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re


@dataclass
class ProjectScope:
    name: str
    workspace_id: str
    working_directory: Path
    scope_file_path: Path
    default_risk_level: str = "medium"
    off_limits: list[str] | None = None
    raw_markdown: str = ""
    telegram_bot_token: str | None = None
    verify_command: str | None = None
    verify_timeout_seconds: int | None = None
    github_repo: str | None = None
    agent_loop_enabled: bool = True

    @classmethod
    def from_file(cls, path: Path) -> ProjectScope:
        resolved_path = path.resolve()
        if not resolved_path.is_file():
            raise ValueError(f"harness-scope file does not exist: {resolved_path}")
        content = resolved_path.read_text(encoding="utf-8")

        workspace_id = _extract_field(content, "workspace_id", "workspace")
        if not workspace_id:
            raise ValueError(f"harness-scope file {resolved_path} missing required 'workspace_id' field")
        working_dir_str = _extract_field(content, "working_directory", "working_dir")
        if working_dir_str:
            working_dir = Path(working_dir_str).resolve()
        else:
            working_dir = resolved_path.parent

        name = _extract_field(content, "name", "project_name") or working_dir.name
        default_risk = _extract_field(content, "default_risk_level", "risk_level") or "medium"
        if default_risk not in {"low", "medium", "high"}:
            default_risk = "medium"

        off_limits_str = _extract_field(content, "off_limits")
        off_limits = [s.strip() for s in off_limits_str.split(",") if s.strip()] if off_limits_str else []

        telegram_bot_token = _extract_field(content, "telegram_bot_token", "bot_token")

        verify_command = _extract_field(content, "verify_command")
        verify_timeout_str = _extract_field(content, "verify_timeout_seconds")
        verify_timeout_seconds = (
            int(verify_timeout_str)
            if verify_timeout_str and verify_timeout_str.isdigit()
            else None
        )

        # "owner/repo", used by the unattended scanner as an alternative backlog
        # source to MemTrace Task Nodes (2026-09-05: git/GitHub Issues chosen over
        # MemTrace Task Node tracking, decided after the MemTrace convention proved
        # hard to keep in sync in practice) — see scanner.py's
        # _find_ready_github_issues().
        github_repo = _extract_field(content, "github_repo")

        # Per-project capability flag: a project that never wants real code changes
        # (e.g. a pure operational/monitoring project with no repo to develop) sets
        # `- agent_loop: disabled` so Controller is told run_planner isn't available
        # and can only ever pick run_operational_action/ask_human/stop instead. Any
        # value other than the literal "disabled" (including the field being absent)
        # leaves the full development loop enabled — this is opt-out, not opt-in.
        agent_loop_str = _extract_field(content, "agent_loop")
        agent_loop_enabled = (agent_loop_str or "").strip().lower() != "disabled"

        return cls(
            name=name,
            workspace_id=workspace_id,
            working_directory=working_dir,
            scope_file_path=resolved_path,
            default_risk_level=default_risk,
            off_limits=off_limits,
            raw_markdown=content,
            telegram_bot_token=telegram_bot_token,
            verify_command=verify_command,
            verify_timeout_seconds=verify_timeout_seconds,
            github_repo=github_repo,
            agent_loop_enabled=agent_loop_enabled,
        )


def _extract_field(content: str, *keys: str) -> str | None:
    for key in keys:
        # Match markdown headers, bullet points or key-value lines
        # e.g., - workspace_id: ws_ef463f70 or workspace_id = ws_ef463f70
        pattern = rf"(?:^|\n)[-*\s]*`?{re.escape(key)}`?\s*[:=]\s*[`\"']?([^`\"\n\r]+)[`\"']?"
        match = re.search(pattern, content, re.IGNORECASE)
        if match:
            return match.group(1).strip()
    return None


def load_project_index(index_path: Path | None) -> list[ProjectScope]:
    if not index_path or not index_path.is_file():
        return []
    scopes: list[ProjectScope] = []
    lines = index_path.read_text(encoding="utf-8").splitlines()
    for line in lines:
        cleaned = line.strip()
        if not cleaned or cleaned.startswith("#"):
            continue
        p = Path(cleaned)
        if p.is_file():
            scopes.append(ProjectScope.from_file(p))
    return scopes
