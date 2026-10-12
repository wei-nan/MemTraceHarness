from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
import re
import tomllib

from memtrace_harness.command_policy import REFUSAL_HINT, forbidden_reason
from memtrace_harness.workspace import ENV_FILE_NAME, STATE_DIR_NAME


DEFAULT_MAX_WORKERS = 3


REPO_NAME = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
# A goal that starts `repo=<name>::` is for that repo (a chat task marker, a schedule, a scanner proposal).
REPO_GOAL_PREFIX = re.compile(r"^repo=([A-Za-z0-9_-]{1,40})::(.*)$", re.DOTALL)


# A one-off model for this task only: `model=<model>::` or `model=<provider>/<model>::`.
MODEL_GOAL_PREFIX = re.compile(r"^model=((?:[a-z]+/)?[A-Za-z0-9][A-Za-z0-9.\-]{0,79})::(.*)$", re.DOTALL)


def split_goal_directives(goal: str) -> tuple[str | None, str | None, str]:
    """(`repo` name, one-off `model` spec, the goal without its leading `repo=…::` / `model=…::`
    prefixes — either, in either order). Missing ones are None."""
    repo: str | None = None
    model: str | None = None
    rest = goal.strip()
    while True:
        if repo is None and (match := REPO_GOAL_PREFIX.match(rest)):
            repo = match.group(1)
        elif model is None and (match := MODEL_GOAL_PREFIX.match(rest)):
            model = match.group(1)
        else:
            break
        rest = match.group(2).strip()
    if repo is None and model is None:
        return None, None, goal
    return repo, model, rest


def split_repo_goal(goal: str) -> tuple[str | None, str]:
    """(`repo` name or None, the goal without its `repo=<name>::` prefix — and without a
    `model=…::` prefix, which `split_goal_directives` reads)."""
    repo, _, rest = split_goal_directives(goal)
    return repo, rest


@dataclass
class RepoScope:
    """A repo a Harness also works in and helps maintain, besides its primary one. Declared in the
    workspace's `.harness/repos.toml` (machine-local, never committed): where it is on this machine, how to
    verify a change, and what is off-limits in it."""

    name: str
    path: Path
    verify_command: str | None = None
    verify_timeout_seconds: int | None = None
    worktree_setup_command: str | None = None
    github_repo: str | None = None
    off_limits: list[str] = field(default_factory=list)


def load_repos(path: Path) -> dict[str, RepoScope]:
    if not path.is_file():
        return {}
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    repos: dict[str, RepoScope] = {}
    for name, entry in (data.get("repos") or {}).items():
        if not REPO_NAME.match(name):
            raise ValueError(f"{path}: {name!r} is not a usable repo name (letters, digits, - and _)")
        if not entry.get("path"):
            raise ValueError(f"{path}: repo {name!r} has no path")
        repos[name] = RepoScope(
            name=name,
            path=Path(str(entry["path"])).expanduser().resolve(),
            verify_command=entry.get("verify_command"),
            verify_timeout_seconds=entry.get("verify_timeout_seconds"),
            worktree_setup_command=entry.get("worktree_setup_command"),
            github_repo=entry.get("github_repo"),
            off_limits=[str(x) for x in entry.get("off_limits", [])],
        )
    return repos


def dump_repos(repos: dict[str, RepoScope]) -> str:
    """repos.toml text. (The file is managed by `memtrace-harness repo ...`; comments are not kept.)"""
    import json

    lines = ["# The repos this Harness also works in. Managed by `memtrace-harness repo add|remove`."]
    for name, repo in repos.items():
        lines += ["", f"[repos.{name}]", f"path = {json.dumps(str(repo.path), ensure_ascii=False)}"]
        for key in ("verify_command", "worktree_setup_command", "github_repo"):
            value = getattr(repo, key)
            if value:
                lines.append(f"{key} = {json.dumps(value, ensure_ascii=False)}")
        if repo.verify_timeout_seconds:
            lines.append(f"verify_timeout_seconds = {int(repo.verify_timeout_seconds)}")
        if repo.off_limits:
            lines.append("off_limits = [" + ", ".join(json.dumps(x, ensure_ascii=False) for x in repo.off_limits) + "]")
    return "\n".join(lines) + "\n"


@dataclass
class ProjectScope:
    name: str
    workspace_id: str
    working_directory: Path
    scope_file_path: Path
    default_risk_level: str = "medium"
    off_limits: list[str] | None = None
    raw_markdown: str = ""
    # Legacy: a bot token written into the project's own scope file. Only `migrate` reads it (to
    # move it into the Telegram service's env file); the Harness itself has no use for one.
    telegram_bot_token: str | None = None
    verify_command: str | None = None
    verify_timeout_seconds: int | None = None
    github_repo: str | None = None
    agent_loop_enabled: bool = True
    # How many tasks of this project may run at once. Each runs in its own git
    # worktree; 1 keeps the old one-task-at-a-time behavior, editing the checkout in
    # place. A project that is not a git repository always runs one task at a time.
    max_workers: int = DEFAULT_MAX_WORKERS
    # Optional shell command run inside a fresh worktree to make it usable (link
    # node_modules, copy an .env — whatever git does not carry over).
    worktree_setup_command: str | None = None
    # What the chat model's operations tools (ops_mcp.py) may touch, all declared by the
    # operator in the scope file: hostnames http_probe may request, log files read_log may
    # read (paths relative to working_directory), and named shell commands run_job may run
    # after the human taps confirm in the chat ("- job: name = command", one per line).
    probe_hosts: list[str] | None = None
    log_files: list[str] | None = None
    jobs: dict[str, str] | None = None
    # Extra folders the read-only ops tools (read_file / list_dir / query_db / system_status git) may look at, beyond
    # the project's own folder: "- read_roots: <workspace>/Beri, ~/.config/memtraceharness". Declared by the operator;
    # credential files stay unreadable inside them. Reading another Harness's workspace needs it declared here.
    read_roots: list[str] | None = None
    # Name of a declared job the Harness runs by itself after a task finishes with its work
    # delivered (e.g. republishing a preview); "- publish_job: preview_start". No confirm tap:
    # the operator already chose the command by declaring it, and it must exist in `jobs`.
    publish_job: str | None = None
    # Other repos this Harness works in (see RepoScope); the scope above describes the primary one.
    repos: dict[str, RepoScope] = field(default_factory=dict)
    # Which repo this view of the scope is for: None = the primary one. See for_repo().
    repo: str | None = None
    base: "ProjectScope | None" = field(default=None, repr=False, compare=False)

    def resolve_repo(self, name: str | None) -> str | None:
        """None for the primary repo (also when named by the project's own name or `primary`), the
        declared repo's name otherwise; ValueError for one that does not exist."""
        if name is None:
            return None
        lowered = name.lower()
        if lowered in {"primary", self.name.lower()}:
            return None
        for declared in self.repos:
            if declared.lower() == lowered:
                return declared
        known = ", ".join([self.name, *self.repos]) or self.name
        raise ValueError(f"no repo named {name!r} (this project's repos: {known})")

    def for_repo(self, name: str | None) -> "ProjectScope":
        """This scope as seen by a task in one repo: that repo's directory, verify command, setup command
        and GitHub repo, and its off-limits added to the project's. Everything about the project (workspace
        id, name, worker slots) is unchanged. Asking a repo's view for another repo (or None) starts again
        from the project's own scope."""
        base = self.base or self
        resolved = base.resolve_repo(name)
        if resolved is None:
            return base
        repo = base.repos[resolved]
        return replace(
            base,
            base=base,
            repo=resolved,
            working_directory=repo.path,
            verify_command=repo.verify_command,
            verify_timeout_seconds=repo.verify_timeout_seconds,
            worktree_setup_command=repo.worktree_setup_command,
            github_repo=repo.github_repo,
            off_limits=[*(base.off_limits or []), *[o for o in repo.off_limits if o not in (base.off_limits or [])]],
        )

    @property
    def lock_key(self) -> str:
        """What one Developer at a time is serialized on: the MemTrace workspace for the primary repo,
        that workspace plus the repo for another one (two repos can be edited side by side)."""
        return self.workspace_id if self.repo is None else f"{self.workspace_id}#{self.repo}"

    def repo_label(self) -> str:
        return self.name if self.repo is None else self.repo

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
        # The Harness's own files sit in the workspace its agents work in: they are never theirs to
        # read, whatever the scope file says.
        for guarded in (ENV_FILE_NAME, STATE_DIR_NAME):
            if guarded not in off_limits:
                off_limits.append(guarded)

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

        max_workers_str = _extract_field(content, "max_workers", "worker_slots")
        max_workers = (
            max(1, int(max_workers_str))
            if max_workers_str and max_workers_str.strip().isdigit()
            else DEFAULT_MAX_WORKERS
        )
        worktree_setup_command = _extract_field(content, "worktree_setup_command")

        probe_hosts = _split_list(_extract_field(content, "probe_hosts"))
        log_files = _split_list(_extract_field(content, "log_files"))
        read_roots = _split_list(_extract_field(content, "read_roots"))
        jobs = {
            match.group(1): match.group(2).strip()
            for match in re.finditer(
                r"(?:^|\n)[-*\s]*`?job`?\s*[:=]\s*([A-Za-z0-9_-]{1,40})\s*=\s*([^\n\r]+)", content
            )
        }

        publish_job = _extract_field(content, "publish_job")
        if publish_job is not None and publish_job not in jobs:
            publish_job = None

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
            max_workers=max_workers,
            worktree_setup_command=worktree_setup_command,
            probe_hosts=[h.lower() for h in probe_hosts],
            log_files=log_files,
            read_roots=read_roots,
            jobs=jobs,
            publish_job=publish_job,
        )


def _split_list(value: str | None) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()] if value else []


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


# ---- changes to the ops declarations in a scope file (probe_hosts / log_files / job), proposed by the
# chat model and applied only after the human confirms in the chat (see ops_mcp.propose_scope_change).
SCOPE_CHANGE_KINDS = ("probe_host", "log_file", "job")
_HOST_NAME = re.compile(r"^(?=.{1,253}$)[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*$")
_JOB_NAME = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
MAX_JOB_COMMAND_CHARS = 1000


def validate_scope_change(
    working_directory: Path,
    kind: str,
    value: str | None = None,
    name: str | None = None,
    command: str | None = None,
) -> dict[str, str]:
    """The normalized change, or ValueError. Run again when the human confirms: the proposal is data,
    not trusted."""
    if kind == "probe_host":
        host = (value or "").strip().lower()
        if not _HOST_NAME.match(host):
            raise ValueError("probe_host must be a bare hostname (no scheme, port or path)")
        return {"kind": kind, "value": host}
    if kind == "log_file":
        relative = (value or "").strip()
        if not relative or "," in relative or "\n" in relative or Path(relative).is_absolute():
            raise ValueError("log_file must be a relative path inside the project")
        base = working_directory.resolve()
        resolved = (base / relative).resolve()
        if base != resolved and base not in resolved.parents:
            raise ValueError("log_file must stay inside the project directory")
        return {"kind": kind, "value": relative}
    if kind == "job":
        job_name, job_command = (name or "").strip(), (command or "").strip()
        if not _JOB_NAME.match(job_name):
            raise ValueError("job name must be 1-40 letters, digits, '_' or '-'")
        if not job_command or "\n" in job_command or "\r" in job_command:
            raise ValueError("job command must be one non-empty line")
        if len(job_command) > MAX_JOB_COMMAND_CHARS:
            raise ValueError(f"job command is longer than {MAX_JOB_COMMAND_CHARS} characters")
        reason = forbidden_reason(job_command)
        if reason:
            raise ValueError(f"{reason}：{REFUSAL_HINT}")
        return {"kind": kind, "name": job_name, "command": job_command}
    raise ValueError(f"unknown scope change kind {kind!r} (allowed: {', '.join(SCOPE_CHANGE_KINDS)})")


def describe_scope_change(change: dict[str, str]) -> str:
    if change["kind"] == "job":
        return f"job {change['name']} = {change['command']}"
    return f"{change['kind']}: {change['value']}"


def apply_scope_change(scope_file: Path, change: dict[str, str]) -> bool:
    """Write one validated change into the scope file; False when it is already there. Existing text
    and comments are kept; the file is replaced atomically."""
    text = scope_file.read_text(encoding="utf-8")
    lines = text.split("\n")
    kind = change["kind"]
    if kind == "job":
        new_line = f"- job: {change['name']} = {change['command']}"
        pattern = re.compile(rf"^[-*\s]*`?job`?\s*[:=]\s*{re.escape(change['name'])}\s*=")
        for index, line in enumerate(lines):
            if pattern.match(line):
                if line.strip() == new_line:
                    return False
                lines[index] = new_line
                break
        else:
            lines = _append_line(lines, new_line)
    else:
        field_name = "probe_hosts" if kind == "probe_host" else "log_files"
        item = change["value"]
        pattern = re.compile(rf"^([-*\s]*`?{field_name}`?\s*[:=]\s*)(.*)$", re.IGNORECASE)
        for index, line in enumerate(lines):
            match = pattern.match(line)
            if match:
                current = _split_list(match.group(2))
                if item.lower() in (c.lower() for c in current):
                    return False
                lines[index] = f"{match.group(1)}{', '.join([*current, item])}"
                break
        else:
            lines = _append_line(lines, f"- {field_name}: {item}")
    updated = "\n".join(lines)
    temp = scope_file.with_name(scope_file.name + ".tmp")
    temp.write_text(updated, encoding="utf-8")
    temp.replace(scope_file)
    return True


def _append_line(lines: list[str], new_line: str) -> list[str]:
    trimmed = list(lines)
    while trimmed and trimmed[-1] == "":
        trimmed.pop()
    return [*trimmed, new_line, ""]
