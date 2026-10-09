"""A Harness's workspace: one directory, one `.harness.env`, one `.harness/` state folder.

Every Harness is started against exactly one workspace, which may be an empty folder (it does not
have to be a git repository). Everything the Harness needs lives there:

* `.harness.env`  — the Harness's own settings and secrets: MemTrace URL/token, model and role
                    preferences, CLI paths, its local API token. Mode 0600.
* `.harness/`     — its state: the SQLite store, raw provider traces, git worktrees, roles.toml.
* `harness-scope.md` — what the project is (workspace id, off-limits, jobs, worker slots).

Transport and presentation (Telegram, the status web page) are separate services with their own
environment files and are deliberately not configured here: this file never holds a bot token.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

ENV_FILE_NAME = ".harness.env"
STATE_DIR_NAME = ".harness"
SCOPE_FILE_NAME = "harness-scope.md"

# Keys that identify a service's secret rather than the Harness's: if one of these shows up in a
# `.harness.env` the workspace is still configured the old way and the Harness refuses to carry
# it into child processes (cli_process.py scrubs the environment anyway).
SERVICE_ONLY_KEYS = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_ALLOWED_CHAT_IDS")


class WorkspaceError(RuntimeError):
    pass


def parse_env_file(path: Path) -> dict[str, str]:
    """KEY=VALUE lines; `#` comments, blank lines, an optional `export ` prefix and one layer of
    matching quotes are understood. Nothing is expanded or executed."""
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        values[key.strip()] = value
    return values


def write_env_updates(env_path: Path, updates: dict[str, str]) -> None:
    """Set keys in place (appending new ones), keeping every other line and comment."""
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
    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _restrict(env_path)


def remove_env_keys(env_path: Path, keys: list[str]) -> None:
    lines = env_path.read_text(encoding="utf-8").splitlines()
    kept = [line for line in lines if not ("=" in line and line.split("=", 1)[0] in keys)]
    env_path.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")


def _restrict(path: Path) -> None:
    """A file holding secrets is for its owner only."""
    try:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


def workspace_paths(root: Path) -> dict[str, Path]:
    root = root.resolve()
    state = root / STATE_DIR_NAME
    return {
        "root": root,
        "env": root / ENV_FILE_NAME,
        "state": state,
        "db": state / "harness.sqlite3",
        "traces": state / "traces",
        "roles": state / "roles.toml",
        "scope": root / SCOPE_FILE_NAME,
    }


def _exclude_from_git(root: Path) -> None:
    """When the workspace is a git checkout, keep the Harness's secrets and state out of its
    `git status` and out of every commit — locally (`.git/info/exclude`), without touching the
    project's own .gitignore. `.harness/` also carries its own `.gitignore`; `.harness.env` needs
    this."""
    git = root / ".git"
    if git.is_file():  # a linked worktree: .git is a pointer to the real git dir
        try:
            target = git.read_text(encoding="utf-8").strip().removeprefix("gitdir:").strip()
            common = Path(target) / "commondir"
            git_dir = (Path(target) / common.read_text(encoding="utf-8").strip()).resolve() if common.is_file() else Path(target)
        except OSError:
            return
    elif git.is_dir():
        git_dir = git
    else:
        return
    exclude = git_dir / "info" / "exclude"
    try:
        exclude.parent.mkdir(parents=True, exist_ok=True)
        present = exclude.read_text(encoding="utf-8").splitlines() if exclude.is_file() else []
        missing = [name for name in (ENV_FILE_NAME, f"{STATE_DIR_NAME}/") if name not in present]
        if missing:
            body = "\n".join(present + ([] if not present or present[-1] == "" else [""]) + missing) + "\n"
            exclude.write_text(body, encoding="utf-8")
    except OSError:
        pass  # a read-only .git is the operator's call; the file mode and the .harness/.gitignore still hold


def init_workspace(root: Path) -> dict[str, Path]:
    """Create the folder layout of an empty workspace (idempotent). Returns the paths."""
    paths = workspace_paths(root)
    paths["root"].mkdir(parents=True, exist_ok=True)
    paths["state"].mkdir(exist_ok=True)
    paths["traces"].mkdir(exist_ok=True)
    gitignore = paths["state"] / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text("*\n", encoding="utf-8")  # state and secrets never get committed
    if not paths["env"].exists():
        paths["env"].write_text("# Harness settings and secrets for this workspace.\n", encoding="utf-8")
    _restrict(paths["env"])
    _exclude_from_git(paths["root"])
    return paths


def load_workspace(root: Path | str, *, environ: dict[str, str] | None = None) -> Path:
    """Put the workspace's `.harness.env` into the process environment and point
    HARNESS_WORKSPACE at it. A variable already set in the real environment wins over the file
    (so a one-off override on the command line behaves as expected). Returns the resolved root."""
    target = os.environ if environ is None else environ
    root_path = Path(root).expanduser().resolve()
    if not root_path.is_dir():
        raise WorkspaceError(f"workspace {root_path} is not a directory")
    env_file = root_path / ENV_FILE_NAME
    if env_file.is_file():
        for key, value in parse_env_file(env_file).items():
            target.setdefault(key, value)
    target["HARNESS_WORKSPACE"] = str(root_path)
    return root_path


def env_file_problems(root: Path) -> list[str]:
    """Things wrong with a workspace's env file that the operator should know about."""
    env_file = workspace_paths(root)["env"]
    problems: list[str] = []
    if not env_file.is_file():
        return [f"{env_file} does not exist"]
    if env_file.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        problems.append(f"{env_file} is readable by other users; run: chmod 600 {env_file}")
    keys = parse_env_file(env_file)
    for key in keys:
        if key in SERVICE_ONLY_KEYS or key.startswith("HARNESS_TELEGRAM_"):
            problems.append(f"{key} belongs in the Telegram service's env file, not in {env_file.name}")
    return problems
