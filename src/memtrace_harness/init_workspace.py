"""`memtrace-harness init-workspace`: make a folder into a Harness workspace.

Writes `.harness.env` (this Harness's environment: secrets, endpoints, CLI paths; mode 0600),
`.harness/` with its `models.toml` (the preferences: which model chats, digests, recalls), and — when
absent — `harness-scope.md`. The folder may be empty and need not be a git repository. An optional
last step writes the Telegram service's own env file, outside the workspace.
"""

from __future__ import annotations

import argparse
import json
import secrets
import socket
import sys
from pathlib import Path

from memtrace_harness.memtrace_client import MemTraceClient
from memtrace_harness.workspace import (
    ENV_FILE_NAME,
    MODEL_ENV_VARS,
    SCOPE_FILE_NAME,
    STATE_DIR_NAME,
    init_workspace,
    models_file,
    parse_env_file,
    read_model_settings,
    update_model_settings,
    write_env_updates,
)

DEFAULT_API_PORT = 8800
DEFAULT_SERVICES_DIR = Path.home() / ".config" / "memtraceharness"

CHAT_PRESETS = {
    "1": ("antigravity", "gemini-3.6-flash-high", "claude/haiku"),
    "2": ("claude", "haiku", "antigravity/gemini-3.6-flash-high"),
    "3": ("codex", "", ""),
}


def free_port(start: int = DEFAULT_API_PORT, taken: set[int] | None = None) -> int:
    """The first loopback port from `start` that nothing listens on and no other workspace claimed."""
    for port in range(start, start + 200):
        if taken and port in taken:
            continue
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                continue
        return port
    raise RuntimeError("no free port found for the Harness API")


def write_scope_file(path: Path, name: str, workspace_id: str, off_limits: str) -> None:
    # default_risk_level intentionally not asked/written here: risk-level-driven behavior belongs to
    # the Agent Loop policy/KB layer, not something the wizard should ask the operator to configure.
    guarded = [ENV_FILE_NAME, STATE_DIR_NAME, *[s.strip() for s in off_limits.split(",") if s.strip()]]
    lines = [
        f"# Harness scope — {name}",
        "",
        f"- name: {name}",
        f"- workspace_id: {workspace_id}",
        f"- working_directory: {path.parent}",
        f"- off_limits: {', '.join(dict.fromkeys(guarded))}",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def service_env_dir(name: str, services_dir: Path | None = None) -> Path:
    return (services_dir or DEFAULT_SERVICES_DIR) / name


def write_telegram_env(
    path: Path, *, harness_port: int, harness_token: str, bot_token: str, chat_ids: str
) -> None:
    """The Telegram service's own env file. It lives outside the workspace, where the agents that
    work in that folder do not look, and holds the only copy of the bot token."""
    path.parent.mkdir(parents=True, exist_ok=True)
    write_env_updates(
        path,
        {
            "TELEGRAM_BOT_TOKEN": bot_token,
            "TELEGRAM_ALLOWED_CHAT_IDS": chat_ids,
            "HARNESS_URL": f"http://127.0.0.1:{harness_port}",
            "HARNESS_API_TOKEN": harness_token,
        },
    )


def _prompt(question: str, default: str = "") -> str:
    suffix = f"（預設：{default}）" if default else ""
    answer = input(f"{question}{suffix}: ").strip()
    return answer or default


def _yes(question: str, default: bool) -> bool:
    hint = "Y/n" if default else "y/N"
    answer = input(f"{question} [{hint}]: ").strip().lower()
    return default if not answer else answer in {"y", "yes"}


def _prompt_memtrace_workspace(url: str | None, token: str | None, question: str) -> str:
    if url:
        try:
            result = MemTraceClient(url, token).call_tool("list_workspaces", {})
            workspaces = json.loads(result.get("content", [{}])[0].get("text", "[]"))
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


def init_workspace_command(args: argparse.Namespace) -> int:
    interactive = not args.non_interactive and sys.stdin.isatty()
    directory = args.directory or getattr(args, "workspace_dir", None)
    if directory is None:
        if not interactive:
            raise ValueError("init-workspace needs a folder: memtrace-harness init-workspace <folder>")
        directory = Path(_prompt("工作區資料夾（可以是空的，不必是 git repo)")).expanduser()
    paths = init_workspace(Path(directory).expanduser())
    existing = {**read_model_settings(paths["models"]), **(parse_env_file(paths["env"]) if paths["env"].is_file() else {})}
    name = args.name or paths["root"].name
    if interactive and not args.name:
        name = _prompt("專案名稱", default=name)
    print(f"工作區：{paths['root']}")

    # --- scope file
    if paths["scope"].exists():
        print(f"沿用既有的 {SCOPE_FILE_NAME}")
    else:
        memtrace_ws = args.memtrace_workspace
        if not memtrace_ws:
            if not interactive:
                raise ValueError(f"{SCOPE_FILE_NAME} 不存在，需要 --memtrace-workspace ws_...")
            memtrace_ws = _prompt_memtrace_workspace(
                existing.get("MEMTRACE_MCP_URL"), existing.get("MEMTRACE_API_TOKEN"),
                "這個專案的 MemTrace 規格/待辦 workspace_id（產品規劃、決策用）",
            )
        off_limits = _prompt("禁區關鍵字（逗號分隔，沒有就留空）") if interactive else ""
        write_scope_file(paths["scope"], name, memtrace_ws, off_limits)
        print(f"已寫入 {paths['scope']}")

    # --- .harness.env
    updates: dict[str, str] = {}
    if "HARNESS_API_TOKEN" not in existing:
        updates["HARNESS_API_TOKEN"] = secrets.token_urlsafe(32)
    if "HARNESS_API_PORT" not in existing:
        updates["HARNESS_API_PORT"] = str(free_port())
    if interactive:
        if "MEMTRACE_MCP_URL" not in existing:
            url = _prompt("MemTrace MCP 位址（MEMTRACE_MCP_URL，沒有就留空）")
            if url:
                updates["MEMTRACE_MCP_URL"] = url
                token = _prompt("MemTrace API token（MEMTRACE_API_TOKEN）")
                if token:
                    updates["MEMTRACE_API_TOKEN"] = token
        if "HARNESS_CHAT_PROVIDER" not in existing:
            print("\n聊天模型（快速對話用，走的不是完整 Agent Loop）：")
            print("  1) Gemini（便宜、預設建議）  2) Claude  3) Codex  4) 之後再設")
            choice = _prompt("選擇", default="1")
            if choice in CHAT_PRESETS:
                provider, model, fallbacks = CHAT_PRESETS[choice]
                updates["HARNESS_CHAT_PROVIDER"] = provider
                updates["HARNESS_CHAT_MODEL"] = model
                if fallbacks:
                    updates["HARNESS_CHAT_FALLBACKS"] = fallbacks
        if "HARNESS_MEMORY_WORKSPACE_ID" not in existing and _yes(
            "冷記憶（對話紀錄）要寫進獨立的 MemTrace workspace 嗎？（不要就寫進上面的規格 workspace）", False
        ):
            updates["HARNESS_MEMORY_WORKSPACE_ID"] = _prompt_memtrace_workspace(
                updates.get("MEMTRACE_MCP_URL") or existing.get("MEMTRACE_MCP_URL"),
                updates.get("MEMTRACE_API_TOKEN") or existing.get("MEMTRACE_API_TOKEN"),
                "冷記憶要寫進哪個 workspace_id",
            )
        if not paths["roles"].exists() and _yes(
            "要複製一份可自訂的 role-profiles（Agent Loop 各角色用的模型）到 .harness/roles.toml 嗎？", False
        ):
            default_toml = Path(__file__).with_name("default-role-profiles.toml")
            paths["roles"].write_text(default_toml.read_text(encoding="utf-8"), encoding="utf-8")
    if "HARNESS_ROLE_PROFILES_FILE" in updates:
        del updates["HARNESS_ROLE_PROFILES_FILE"]  # .harness/roles.toml is found by its place
    preferences = {k: v for k, v in updates.items() if k in MODEL_ENV_VARS}
    environment = {k: v for k, v in updates.items() if k not in MODEL_ENV_VARS}
    if environment:
        write_env_updates(paths["env"], environment)
        print(f"\n已更新 {paths['env']}（權限 600）：{', '.join(k for k in environment if 'TOKEN' not in k)}"
              + ("，並產生了 API token" if "HARNESS_API_TOKEN" in environment else ""))
    if preferences:
        update_model_settings(models_file(paths["root"]), preferences)
        print(f"已寫入 {models_file(paths['root'])}：{', '.join(preferences)}")
    merged = {**existing, **updates}
    api_port = int(merged["HARNESS_API_PORT"])

    # --- optional Telegram service
    if interactive and _yes("\n要現在設定這個 Harness 的 Telegram 服務嗎？（bot token 只會存在 Harness 資料夾以外）", False):
        print("去 Telegram 找 @BotFather，傳 /newbot 建立一個 bot，把它給你的 token 貼在這裡。")
        bot_token = _prompt("Bot token")
        chats = _prompt("允許對話的 chat id（逗號分隔）")
        if bot_token and chats:
            target = service_env_dir(name) / "telegram.env"
            write_telegram_env(
                target, harness_port=api_port, harness_token=merged["HARNESS_API_TOKEN"],
                bot_token=bot_token, chat_ids=chats,
            )
            print(f"已寫入 {target}（權限 600）")
            print(f"啟動：python -m memtrace_services.telegram.main --env-file {target}")

    print("\n=== 完成 ===")
    print(f"檢查：  memtrace-harness -C {paths['root']} doctor")
    print(f"啟動：  memtrace-harness -C {paths['root']} serve")
    return 0
