"""Operator-managed memory shared by Harnesses on this host, outside project stores."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from uuid import uuid4


DELIVERY_MEMORY_TITLE = "開發任務的標準交付流程"
DELIVERY_MEMORY_BODY = """所有 Harness 接到程式開發或修正任務時，統一使用各 repo 的獨立 Git worktree 與工作分支。
完成修改並實際執行測試、確認結果沒有問題後，可以推送工作分支到遠端並發起 MR／PR。
確認該次提交的 CI 通過，且 MR／PR 沒有合併衝突；若有衝突或 CI 失敗，回到原任務的 worktree 調整、重新測試並推送，再重新確認 CI 與衝突狀態。
CI 通過且無衝突後，合併進 main；確認遠端合併完成，再完成並清理該 worktree，關閉這次任務對應的 issues。
只對本次任務明確對應、已完成的 issues 關單，不批次更動其他歷史單。完成後若出現其他問題，以新的 issue 開單處理，不反覆糾結或重開已完成的舊單。
測試、推送、MR／PR、CI、合併與關單都以實際結果為準；若缺工具或權限，先探索可用能力並回報具體缺口，不把尚未完成的步驟宣稱為成功。"""


class SharedMemoryStore:
    def __init__(self, path: Path):
        self.path = Path(path).expanduser().resolve()
        self.store_id = hashlib.sha256(str(self.path).encode()).hexdigest()[:16]

    @contextmanager
    def _connection(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        try:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS memories (
                    id TEXT PRIMARY KEY, title TEXT NOT NULL, body TEXT NOT NULL,
                    enabled INTEGER NOT NULL, revision INTEGER NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, deleted_at TEXT,
                    policy TEXT
                );
                CREATE TABLE IF NOT EXISTS revisions (
                    id INTEGER PRIMARY KEY, memory_id TEXT NOT NULL, revision INTEGER NOT NULL,
                    entry_json TEXT NOT NULL, changed_at TEXT NOT NULL
                );
            """)
            if "policy" not in {row[1] for row in conn.execute("PRAGMA table_info(memories)")}:
                conn.execute("ALTER TABLE memories ADD COLUMN policy TEXT")
            with conn:
                yield conn
        finally:
            conn.close()

    def list(self) -> list[dict]:
        if not self.path.is_file():
            return []
        # Ordinary reads must not create/change another Harness's store.
        conn = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=15)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute("SELECT * FROM memories WHERE deleted_at IS NULL ORDER BY updated_at DESC, id").fetchall()
            return [{**dict(row), "enabled": bool(row["enabled"])} for row in rows]
        finally:
            conn.close()

    @staticmethod
    def _text(value, name: str, limit: int) -> str:
        if not isinstance(value, str) or not value.strip() or len(value.strip()) > limit:
            raise ValueError(f"{name} must be non-empty text of at most {limit} characters")
        return value.strip()

    def change(self, request: dict) -> dict:
        action = request.get("action")
        if action not in {"create", "update", "toggle", "remove"}:
            raise ValueError("unsupported shared memory action")
        now = datetime.now(timezone.utc).isoformat()
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if action == "create":
                title = self._text(request.get("title"), "title", 160)
                body = self._text(request.get("body"), "body", 6000)
                count = conn.execute("SELECT count(*) FROM memories WHERE deleted_at IS NULL").fetchone()[0]
                if count >= 32:
                    raise ValueError("at most 32 shared memories")
                memory_id = uuid4().hex
                policy = request.get("policy")
                if policy not in (None, "worktree-pr-ci-main"):
                    raise ValueError("unsupported shared workflow")
                conn.execute("INSERT INTO memories VALUES (?, ?, ?, 1, 1, ?, ?, NULL, ?)", (memory_id, title, body, now, now, policy))
            else:
                memory_id = self._text(request.get("id"), "id", 64)
                row = conn.execute("SELECT * FROM memories WHERE id=? AND deleted_at IS NULL", (memory_id,)).fetchone()
                if row is None:
                    raise ValueError("shared memory not found")
                revision = request.get("revision")
                if isinstance(revision, bool) or not isinstance(revision, int) or revision != row["revision"]:
                    raise ValueError("shared memory changed; refresh before saving")
                title, body, enabled = row["title"], row["body"], row["enabled"]
                if action == "update":
                    title = self._text(request.get("title"), "title", 160)
                    body = self._text(request.get("body"), "body", 6000)
                if action == "toggle":
                    if not isinstance(request.get("enabled"), bool):
                        raise ValueError("enabled must be a boolean")
                    enabled = int(request["enabled"])
                conn.execute(
                    "UPDATE memories SET title=?, body=?, enabled=?, revision=revision+1, updated_at=?, deleted_at=? WHERE id=?",
                    (title, body, enabled, now, now if action == "remove" else None, memory_id),
                )
            entry = dict(conn.execute("SELECT * FROM memories WHERE id=?", (memory_id,)).fetchone())
            conn.execute("INSERT INTO revisions (memory_id,revision,entry_json,changed_at) VALUES (?,?,?,?)",
                         (memory_id, entry["revision"], json.dumps(entry, ensure_ascii=False), now))
        entry["enabled"] = bool(entry["enabled"])
        return entry

    def context(self) -> str:
        entries = [entry for entry in self.list() if entry["enabled"]]
        if not entries:
            return ""
        text = "\n\n".join(f"### {entry['title']}\n{entry['body']}" for entry in entries)
        workflow = ""
        if any(entry.get("policy") == "worktree-pr-ci-main" for entry in entries):
            workflow = (
                "\n\n執行分工：自動交付已由操作者啟用。模型負責本機開發、測試和目標核對；"
                "Harness 執行程式接續處理推送、PR、CI、衝突修正、遠端合併、關單和清理。"
                "模型 CLI 不要自行推送、建立／合併 PR 或關單，也不要重複要求這套固定交付流程的授權。"
                "goal_plan 的模型階段與成功條件需涵蓋完整的本機實作需求；遠端交付條件另由執行程式逐項核實，"
                "不應在本機 goal_check 中冒稱 CI、合併或關單已完成，也不必為尚未開始的遠端交付停止本機工作。"
                "整個任務須等執行程式確認遠端交付完成才會標為 succeeded。"
            )
        return (
            "共享記憶：以下由操作者明確設定，適用所有 Harness；與各專案自己的對話、偏好分開儲存。"
            "目前使用者的明確指示優先；與舊有自動歸納偏好衝突時，以此處操作者明確設定為準。依此規劃工作，但每個步驟仍須使用可用工具並核實結果；"
            "記憶中的期望流程不代表該動作已執行或相關能力已實作。\n\n" + text + workflow
        )

    def delivery_enabled(self) -> bool:
        return any(entry["enabled"] and entry.get("policy") == "worktree-pr-ci-main" for entry in self.list())


def shared_status(config) -> dict:
    if config.shared_memory_path is None:
        return {"available": False, "entries": []}
    store = SharedMemoryStore(config.shared_memory_path)
    try:
        return {"available": True, "store_id": store.store_id, "entries": store.list()}
    except (OSError, sqlite3.Error):
        return {"available": False, "store_id": store.store_id, "entries": [], "error": "共享記憶暫時無法讀取"}
