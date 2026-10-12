"""Commands the Harness never runs for a model, whatever the allowlist says (operator decision, 2026-10-12).

Deleting things, ``sudo``, and changing system or security settings are the user's own to do: the
Harness refuses to declare such a command as a job, refuses to run one that is already declared, and the
chat model is told to hand the user the command instead. This is a fail-closed text check, not a
sandbox: it looks at every simple command in a one-line shell string (split on ``; & | ( ) ` $(``) and at a
few multi-word forms, so it may refuse a harmless command that merely looks dangerous — the user can
always run that one themselves.
"""

from __future__ import annotations

import re

_SEGMENT_SPLIT = re.compile(r"&&|\|\||[;&|()`\n]|\$\(")
# Command words that are never run for a model. `sudo`/`doas`/`su` escalate; the rest delete or rewrite
# system and security state.
_FORBIDDEN_WORDS = {
    "sudo": "需要提權（sudo）",
    "doas": "需要提權（doas）",
    "su": "切換使用者（su）",
    "rm": "刪除檔案（rm）",
    "rmdir": "刪除目錄（rmdir）",
    "unlink": "刪除檔案（unlink）",
    "shred": "銷毀檔案（shred）",
    "trash": "刪除檔案（trash）",
    "diskutil": "磁碟操作（diskutil）",
    "dscl": "改使用者／群組（dscl）",
    "scutil": "改系統設定（scutil）",
    "pmset": "改電源設定（pmset）",
    "csrutil": "改系統完整性保護（csrutil）",
    "spctl": "改 Gatekeeper（spctl）",
    "networksetup": "改網路設定（networksetup）",
    "systemsetup": "改系統設定（systemsetup）",
    "security": "鑰匙圈／憑證操作（security）",
    "tccutil": "改隱私權限（tccutil）",
    "visudo": "改 sudoers（visudo）",
    "passwd": "改密碼（passwd）",
    "chown": "改檔案擁有者（chown）",
    "mkfs": "格式化（mkfs）",
}
_FORBIDDEN_FORMS = (
    (re.compile(r"\bdefaults\s+(write|delete|import|rename)\b"), "改系統／應用程式偏好（defaults write）"),
    (re.compile(r"\blaunchctl\s+(bootout|bootstrap|load|unload|remove|disable|enable|config)\b"), "新增或移除系統服務（launchctl）"),
    (re.compile(r"\bgit\s+clean\b"), "刪除未追蹤檔案（git clean）"),
    (re.compile(r"\bgit\s+reset\s+--hard\b"), "丟棄修改（git reset --hard）"),
    (re.compile(r"\bgit\s+push\b[^\n]*(--force\b|--force-with-lease\b|\s-f\b|--delete\b)"), "強制或刪除式推送（git push）"),
    (re.compile(r"\bfind\b[^\n]*(\s-delete\b|\s-exec\s+(rm|shred|unlink)\b)"), "用 find 刪除檔案"),
    (re.compile(r"\bxargs\s+(-\S+\s+)*(rm|shred|unlink)\b"), "用 xargs 刪除檔案"),
    (re.compile(r"\bchmod\s+(-\S+\s+)*[0-7]*7[0-7]{0,2}\s"), "放寬檔案權限（chmod）"),
    (re.compile(r"\bsqlite3?\b[^\n]*\b(drop|delete\s+from|truncate)\b", re.IGNORECASE), "刪除資料庫內容"),
    (re.compile(r">\s*/(etc|System|Library|usr|private)/"), "寫入系統路徑"),
)
_WRAPPERS = {"command", "builtin", "exec", "nohup", "time", "nice", "env", "xargs", "stdbuf", "caffeinate"}
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def forbidden_reason(command: str) -> str | None:
    """Why this one-line command is never run for a model, or None when the policy has no objection."""
    text = command or ""
    for segment in _SEGMENT_SPLIT.split(text):
        words = segment.split()
        while words and (_ASSIGNMENT.match(words[0]) or words[0] in _WRAPPERS or words[0].startswith("-")):
            words = words[1:]
        if not words:
            continue
        word = words[0].strip("'\"").rsplit("/", 1)[-1]
        if word in _FORBIDDEN_WORDS:
            return _FORBIDDEN_WORDS[word]
    for pattern, reason in _FORBIDDEN_FORMS:
        if pattern.search(text):
            return reason
    return None


REFUSAL_HINT = "這類動作只能由你自己在終端機執行；Harness 不會代跑，也不能放進允許清單。"
