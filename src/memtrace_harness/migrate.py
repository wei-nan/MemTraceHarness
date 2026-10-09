"""`memtrace-harness migrate`: from one shared `.env` + project index to one workspace per project.

The old Harness was one process serving every project in `projects.index.txt` from one `.env` with
`HARNESS_*_<PROJECT>` suffixed overrides and one shared SQLite store. This turns each registered
project into a workspace:

* `<project>/.harness.env`   that project's environment (MemTrace URL/token, CLI paths), a fresh API token and port
* `<project>/.harness/`      that project's slice of the shared store, its raw traces, its `models.toml`
                             (the unsuffixed model preferences) and its roles file
* `<services>/<name>/telegram.env`   the project's bot token and chat allowlist, outside the project
* `<services>/web.toml`      the status page's list of Harnesses
* `<services>/launchd/*.plist`       one job per Harness / bot / the page (written, never loaded)

Dry run unless `--apply`. The legacy files are never changed or deleted.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import secrets
import shutil
import sqlite3
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from memtrace_harness.init_workspace import DEFAULT_API_PORT, DEFAULT_SERVICES_DIR, free_port
from memtrace_harness.scope import ProjectScope, load_project_index
from memtrace_harness.workspace import (
    MODEL_ENV_VARS,
    STATE_DIR_NAME,
    init_workspace,
    parse_env_file,
    update_model_settings,
    write_env_updates,
)

# Settings that were global in the old .env and stay the same for every workspace.
GLOBAL_KEYS = (
    "MEMTRACE_MCP_URL", "MEMTRACE_API_TOKEN",
    "HARNESS_CLAUDE_COMMAND", "HARNESS_CODEX_COMMAND", "HARNESS_ANTIGRAVITY_COMMAND",
    "HARNESS_ANTIGRAVITY_OUTPUT_MODE", "HARNESS_CLI_TIMEOUT_SECONDS",
    "HARNESS_UNATTENDED_WRITE_REQUIRES_APPROVAL", "HARNESS_OPERATOR_PREFERENCE_WORKSPACE_ID",
    "HARNESS_REPLY_LANGUAGE", "HARNESS_SCHEDULE_TIMEZONE", "HARNESS_APPROVAL_TTL_HOURS",
    "HARNESS_SHUTDOWN_GRACE_SECONDS", "HARNESS_RECALL_ENABLED", "HARNESS_RECALL_TTL_HOURS",
    "HARNESS_TAIWANTRADE_API_KEY_FILE", "HARNESS_TAIWANTRADE_API_URL", "HARNESS_TAIWANTRADE_ORDER_PROJECTS",
)
# Settings that had a global value and a `_<PROJECT>` override: the override wins, else the global.
PER_PROJECT_KEYS = (
    "HARNESS_CHAT_PROVIDER", "HARNESS_CHAT_MODEL", "HARNESS_CHAT_FALLBACKS",
    "HARNESS_DIGEST_PROVIDER", "HARNESS_DIGEST_MODEL", "HARNESS_DIGEST_FALLBACKS",
    "HARNESS_RECALL_PROVIDER", "HARNESS_RECALL_MODEL", "HARNESS_RECALL_FALLBACKS",
    "HARNESS_MEMORY_WORKSPACE_ID",
)


def slug(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_").upper()


@dataclass
class WorkspacePlan:
    scope: ProjectScope
    root: Path
    env: dict[str, str]
    api_port: int
    api_token: str
    roles_source: Path | None
    telegram: dict[str, str] | None
    notes: list[str] = field(default_factory=list)


def plan_workspaces(
    legacy: dict[str, str], scopes: list[ProjectScope], *, first_port: int = DEFAULT_API_PORT
) -> list[WorkspacePlan]:
    plans: list[WorkspacePlan] = []
    taken: set[int] = set()
    for scope in scopes:
        suffix = slug(scope.name)
        env: dict[str, str] = {k: legacy[k] for k in GLOBAL_KEYS if legacy.get(k)}
        for key in PER_PROJECT_KEYS:
            value = legacy.get(f"{key}_{suffix}")
            if value is None:
                value = legacy.get(key)
            if value is not None and value != "":
                env[key] = value
        notes: list[str] = []
        roles = legacy.get(f"HARNESS_ROLE_PROFILES_FILE_{suffix}")
        roles_source = Path(roles) if roles else None
        port = free_port(first_port, taken)
        taken.add(port)
        token = secrets.token_urlsafe(32)
        env["HARNESS_API_PORT"] = str(port)
        env["HARNESS_API_TOKEN"] = token

        bot = scope.telegram_bot_token or legacy.get(f"HARNESS_TELEGRAM_BOT_TOKEN_{suffix}")
        telegram: dict[str, str] | None = None
        if bot and legacy.get("HARNESS_TELEGRAM_ALLOWED_CHAT_IDS"):
            telegram = {
                "TELEGRAM_BOT_TOKEN": bot,
                "TELEGRAM_ALLOWED_CHAT_IDS": legacy["HARNESS_TELEGRAM_ALLOWED_CHAT_IDS"],
                "HARNESS_URL": f"http://127.0.0.1:{port}",
                "HARNESS_API_TOKEN": token,
            }
        elif legacy.get("HARNESS_TELEGRAM_BOT_TOKEN"):
            notes.append(
                "had no bot of its own (it shared the global bot, routing by name in the message): "
                "a bot now serves exactly one Harness, so create one with @BotFather for it"
            )
        else:
            notes.append("no Telegram bot configured")
        if scope.working_directory.resolve() != scope.scope_file_path.parent.resolve():
            notes.append(
                f"harness-scope.md says working_directory {scope.working_directory} but sits in "
                f"{scope.scope_file_path.parent}; the workspace is the folder holding the scope file"
            )
        plans.append(WorkspacePlan(scope, scope.scope_file_path.parent.resolve(), env, port, token, roles_source, telegram, notes))
    for plan in plans:
        sharing = [o.scope.name for o in plans if o is not plan and o.scope.workspace_id == plan.scope.workspace_id]
        if sharing:
            plan.notes.append(
                f"shares MemTrace workspace {plan.scope.workspace_id} with {', '.join(sharing)}: the run history "
                "recorded against that workspace is copied into each of their stores"
            )
    return plans


# ---- splitting the shared store

def _split_store(source: Path, dest: Path, scope: ProjectScope, memory_workspace: str) -> None:
    """Copy `source` (online backup, safe while the old Harness runs) to `dest` and drop everything
    that belongs to another project. Global things stay: provider cooldowns, and the operator's
    global preferences."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        raise RuntimeError(f"{dest} already exists; refusing to overwrite a workspace's store")
    src = sqlite3.connect(str(source))
    out = sqlite3.connect(str(dest))
    try:
        src.backup(out)
    finally:
        src.close()
    name, ws = scope.name, scope.workspace_id
    mine_ws = (ws, memory_workspace)

    def tables() -> set[str]:
        return {r[0] for r in out.execute("SELECT name FROM sqlite_master WHERE type='table'")}

    def columns(table: str) -> set[str]:
        return {r[1] for r in out.execute(f"PRAGMA table_info({table})")}

    def delete(table: str, keep_sql: str, params: tuple = ()) -> None:
        if table in tables():
            out.execute(f"DELETE FROM {table} WHERE NOT ({keep_sql})", params)

    marks = ",".join("?" for _ in mine_ws)
    for table in ("approval_requests",):
        delete(table, f"workspace IN ({marks})", mine_ws)
    for table in (
        "completion_claims", "conversation_worktrees", "conversations", "kb_gardening_runs", "runs",
        "schedules", "task_queue", "work_reviews", "workspace_charters", "workspace_indexes",
        "workspace_locks", "edit_locks", "kb_promotions", "memory_drafts", "memory_refs",
    ):
        if table in tables() and "workspace_id" in columns(table):
            delete(table, f"workspace_id IN ({marks})", mine_ws)
    for table in (
        "completion_claims", "decision_records", "job_requests", "memory_digests", "memory_drafts",
        "order_intents", "primary_sessions_hot_log", "topic_briefs", "topic_recall_state", "work_reviews",
    ):
        if table in tables() and "project" in columns(table):
            delete(table, "project = ?", (name,))
    delete("preference_rules", "scope = 'global' OR project = ?", (name,))
    # children of what is left
    delete("checkpoints", "conversation_id IN (SELECT id FROM conversations)")
    for table in ("cli_executions", "conflicts", "loop_stages", "model_responses"):
        delete(table, "run_id IN (SELECT id FROM runs)")
    delete("provider_sessions", "run_id IN (SELECT id FROM runs)")
    delete("turns", "run_id IN (SELECT id FROM runs)")
    delete("kb_gardening_actions", "run_id IN (SELECT id FROM kb_gardening_runs)")
    if "telegram_gateway_state" in tables():
        out.execute("DROP TABLE telegram_gateway_state")
    out.commit()
    out.execute("VACUUM")
    out.close()


def _move_traces(db: Path, old_root: Path, new_root: Path) -> int:
    """Copy this store's run directories to the new trace root and point the rows at them."""
    conn = sqlite3.connect(str(db))
    copied = 0
    try:
        run_ids = [r[0] for r in conn.execute("SELECT id FROM runs")]
        new_root.mkdir(parents=True, exist_ok=True)
        for run_id in run_ids:
            source = old_root / run_id
            if source.is_dir() and not (new_root / run_id).exists():
                shutil.copytree(source, new_root / run_id)
                copied += 1
        old_prefix = f"{old_root.as_posix().rstrip('/')}/"
        new_prefix = f"{new_root.as_posix().rstrip('/')}/"
        for table, column in (("turns", "raw_trace_ref"), ("cli_executions", "raw_trace_ref"), ("cli_executions", "stderr_ref")):
            conn.execute(
                f"UPDATE {table} SET {column} = ? || substr({column}, ?) WHERE {column} LIKE ?",
                (new_prefix, len(old_prefix) + 1, old_prefix + "%"),
            )
        conn.commit()
    finally:
        conn.close()
    return copied


def legacy_telegram_offset(db: Path, bot_token: str) -> int | None:
    """Where the old gateway had read this bot's updates up to (it stored sha256(token) -> offset)."""
    if not db.is_file():
        return None
    conn = sqlite3.connect(str(db))
    try:
        row = conn.execute(
            "SELECT last_offset FROM telegram_gateway_state WHERE bot_token_hash = ?",
            (hashlib.sha256(bot_token.encode("utf-8")).hexdigest(),),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    finally:
        conn.close()
    return int(row[0]) if row else None


def merged_web_config(existing: Path, entries: list[dict[str, str]]) -> str:
    """web.toml with `entries` added to (or replacing, by name) what an earlier migration wrote —
    migrating the second legacy instance must not erase the first one's Harnesses."""
    host, port, kept = "127.0.0.1", 8787, []
    if existing.is_file():
        data = tomllib.loads(existing.read_text(encoding="utf-8"))
        host, port = str(data.get("host", host)), int(data.get("port", port))
        kept = [h for h in data.get("harness", []) if h["name"] not in {e["name"] for e in entries}]
    blocks = [
        f'[[harness]]\nname = {json.dumps(h["name"])}\nurl = {json.dumps(h["url"])}\ntoken = {json.dumps(h["token"])}\n'
        for h in [*kept, *entries]
    ]
    return f'host = {json.dumps(host)}\nport = {port}\n\n' + "\n".join(blocks)


# ---- launchd

def plist(label: str, argv: list[str], *, log: Path, exit_timeout: int | None = None) -> str:
    args = "\n".join(f"        <string>{a}</string>" for a in argv)
    timeout = f"    <key>ExitTimeOut</key>\n    <integer>{exit_timeout}</integer>\n" if exit_timeout else ""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{label}</string>
    <key>ProgramArguments</key>
    <array>
{args}
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
{timeout}    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    </dict>
    <key>StandardOutPath</key>
    <string>{log}.log</string>
    <key>StandardErrorPath</key>
    <string>{log}.err.log</string>
</dict>
</plist>
"""


def migrate_command(args: argparse.Namespace) -> int:
    env_path = args.env.resolve()
    if not env_path.is_file():
        raise ValueError(f"{env_path} does not exist")
    legacy = parse_env_file(env_path)
    base = env_path.parent
    index = args.index or (Path(legacy["HARNESS_PROJECT_INDEX"]) if legacy.get("HARNESS_PROJECT_INDEX") else None)
    if index is None:
        raise ValueError("no project index: pass --index or set HARNESS_PROJECT_INDEX in the env file")
    index = (index if index.is_absolute() else base / index).resolve()
    scopes = load_project_index(index)
    if not scopes:
        raise ValueError(f"{index} lists no projects")
    services = (args.services_dir or DEFAULT_SERVICES_DIR).expanduser()
    repo_python = Path(__file__).resolve().parents[2] / ".venv" / "bin" / "python"
    plans = plan_workspaces(legacy, scopes, first_port=args.first_port)

    old_db = Path(legacy.get("HARNESS_TRACE_DB", "data/harness.sqlite3"))
    old_db = old_db if old_db.is_absolute() else base / old_db
    old_traces = Path(legacy.get("HARNESS_TRACE_ROOT", "data/traces"))
    old_traces = old_traces if old_traces.is_absolute() else base / old_traces

    print(f"legacy env: {env_path}\nlegacy store: {old_db}\nservices dir: {services}\n")
    for plan in plans:
        scope = plan.scope
        print(f"[{scope.name}] -> workspace {plan.root}")
        preferences = [k for k in plan.env if k in MODEL_ENV_VARS]
        print(
            f"    .harness.env: {len(plan.env) - len(preferences)} settings, API on 127.0.0.1:{plan.api_port}; "
            f"models.toml: {len(preferences)} preference(s)"
        )
        print(f"    state: {plan.root / STATE_DIR_NAME}" + (f", roles from {plan.roles_source}" if plan.roles_source else ""))
        print(f"    telegram: {'service env ' + str(services / scope.name / 'telegram.env') if plan.telegram else 'none'}")
        for note in plan.notes:
            print(f"    note: {note}")
    if not args.apply:
        print("\ndry run: nothing was written (use --apply)")
        return 0

    web_entries: list[dict[str, str]] = []
    for plan in plans:
        scope = plan.scope
        paths = init_workspace(plan.root)
        if paths["env"].read_text(encoding="utf-8").count("=") > 0 or paths["models"].is_file():
            raise RuntimeError(f"{paths['env']} already has settings; refusing to overwrite a workspace")
        env = {k: v for k, v in plan.env.items() if k not in MODEL_ENV_VARS}
        preferences = {k: v for k, v in plan.env.items() if k in MODEL_ENV_VARS}
        if plan.roles_source is not None:
            roles_source = plan.roles_source if plan.roles_source.is_absolute() else base / plan.roles_source
            if roles_source.is_file():
                shutil.copyfile(roles_source, paths["roles"])  # found by its place, no setting needed
        write_env_updates(paths["env"], env)
        if preferences:
            update_model_settings(paths["models"], preferences)
        memory_ws = preferences.get("HARNESS_MEMORY_WORKSPACE_ID", scope.workspace_id)
        if old_db.is_file():
            _split_store(old_db, paths["db"], scope, memory_ws)
            copied = _move_traces(paths["db"], old_traces, paths["traces"])
            print(f"[{scope.name}] store split; {copied} run trace folder(s) copied")
        if plan.telegram:
            target = services / scope.name / "telegram.env"
            write_env_updates(target, plan.telegram)
            offset = legacy_telegram_offset(old_db, plan.telegram["TELEGRAM_BOT_TOKEN"])
            if offset is not None:  # carry on where the old gateway stopped reading, so nothing is replayed
                (services / scope.name / "telegram.state.json").write_text(json.dumps({"offset": offset}), encoding="utf-8")
            (services / "launchd").mkdir(parents=True, exist_ok=True)
            (services / "launchd" / f"com.memtraceharness.{scope.name}.telegram.plist").write_text(
                plist(
                    f"com.memtraceharness.{scope.name}.telegram",
                    [str(repo_python), "-m", "memtrace_services.telegram.main", "--env-file", str(target)],
                    log=services / scope.name / "telegram",
                ),
                encoding="utf-8",
            )
        (services / "launchd").mkdir(parents=True, exist_ok=True)
        (services / "launchd" / f"com.memtraceharness.{scope.name}.plist").write_text(
            plist(
                f"com.memtraceharness.{scope.name}",
                [str(repo_python), "-m", "memtrace_harness", "-C", str(plan.root), "serve"],
                log=paths["state"] / "serve",
                exit_timeout=150,
            ),
            encoding="utf-8",
        )
        web_entries.append({"name": scope.name, "url": f"http://127.0.0.1:{plan.api_port}", "token": plan.api_token})

    web = services / "web.toml"
    web.parent.mkdir(parents=True, exist_ok=True)
    web.write_text(merged_web_config(web, web_entries), encoding="utf-8")
    web.chmod(0o600)
    (services / "launchd" / "com.memtraceharness.web.plist").write_text(
        plist(
            "com.memtraceharness.web",
            [str(repo_python), "-m", "memtrace_services.web.main", "--config", str(web)],
            log=services / "web",
        ),
        encoding="utf-8",
    )
    print(f"\nwritten: {len(plans)} workspace(s), {web}, plists in {services / 'launchd'}")
    print("The legacy .env, project index and store are untouched. To switch over: stop the old gateway "
          "(launchctl bootout), then for each plist: cp it to ~/Library/LaunchAgents and "
          "launchctl bootstrap gui/$(id -u) <plist>.")
    return 0
