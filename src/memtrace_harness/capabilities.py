"""Installing what the Harness is missing — only after the user says yes, every time (operator decision, 2026-10-12).

When the chat model finds a function or skill missing (a speech-to-text model, a CLI tool, a library, a skill) it calls
``request_install`` (ops_mcp.py), which only records a request. The user sees a confirm card with the exact source, version,
what will be run and why, and nothing happens until they tap it. There is no "always allow" for installs.

Kinds, each a fixed procedure (an argv list or a download, never a shell string the model wrote):

- ``pip`` / ``npm``   into ``<workspace>/.harness/capabilities/{python,node}`` (isolated from the system);
- ``brew``            a Homebrew formula — global, so the card says "會安裝到系統";
- ``model_file``      an https URL and the SHA-256 it must have (a mismatch is deleted, not kept);
- ``script``          an https URL whose script is downloaded, hashed, and run with ``sh`` (the "curl | sh" case);
- ``git``             a clone into ``.harness/capabilities/src/<name>``, plus an optional one-line install command;
- ``skill``           a SKILL.md saved to ``.harness/skills/<name>/`` and offered to the chat model (not the Developer).

**Never, whatever the consent:** ``sudo`` / ``doas`` (a script that mentions them is refused, and a shim named ``sudo`` that fails is
first on the PATH of everything installed here), deletion and system or security settings (command_policy.py: the optional git
install command is checked with it). Uninstalling is deletion, so it is the user's own. **Honest limits:** a downloaded script or a
package's own install step can still do whatever the user's account can; the card says so, and the SHA-256 of what ran is recorded
in ``.harness/capabilities.json`` with where it came from and why.

Using an installed thing is not automatic permission: a CLI is run through the usual tiers (a declared job, a tool), and
``capabilities_env`` only puts what was installed on the PATH of those jobs and of the transcriber.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from memtrace_harness.command_policy import forbidden_reason

KINDS = ("pip", "npm", "brew", "model_file", "script", "git", "skill")
CAP_DIR = "capabilities"
SKILL_DIR = "skills"
MANIFEST = "capabilities.json"
INSTALL_SECONDS = 30 * 60
MAX_SCRIPT_BYTES = 1_000_000
MAX_SKILL_BYTES = 64_000
MAX_MODEL_BYTES = 4 * 1024 ** 3

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@+/-]{0,80}$")  # the folder name of a git checkout
# What may stand where a package name goes. These refuse paths, URLs and option-looking text, which package managers would
# otherwise take as "install from there" / flags.
_PACKAGE_NAME = {
    "pip": re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}(\[[A-Za-z0-9,_-]{1,60}\])?$"),
    "npm": re.compile(r"^(@[a-z0-9~][a-z0-9._~-]{0,80}/)?[a-z0-9~][a-z0-9._~-]{0,80}$"),
    "brew": re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@+-]{0,80}(/[A-Za-z0-9._-]{1,80}){0,2}$"),
}
_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+!-]{0,40}$")
_SKILL_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")
_FILENAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_GIT_SSH = re.compile(r"^[A-Za-z0-9_.-]+@[A-Za-z0-9.-]+:[A-Za-z0-9_./~-]+$")
_SUDO_WORD = re.compile(r"(?<![\w.-])(sudo|doas)(?![\w-])")


class InstallError(ValueError):
    pass


# ---- where things live
def caps_dir(state_dir: Path) -> Path:
    return Path(state_dir) / CAP_DIR


def skills_dir(state_dir: Path) -> Path:
    return Path(state_dir) / SKILL_DIR


def _https(url: object, what: str) -> str:
    text = str(url or "").strip()
    parsed = urllib.parse.urlparse(text)
    if parsed.scheme != "https" or not parsed.hostname or any(c in text for c in " \n\r\t'\"`$;|&<>"):
        raise InstallError(f"{what} 要是 https 網址")
    return text


def _fetch_text(url: str, limit: int) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "memtrace-harness-install/1"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            data = response.read(limit + 1)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise InstallError(f"下載不到 {url}：{exc}") from exc
    if len(data) > limit:
        raise InstallError(f"{url} 太大（上限 {limit} bytes）")
    return data.decode("utf-8", errors="replace")


def _head_size(url: str) -> int | None:
    try:
        request = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "memtrace-harness-install/1"})
        with urllib.request.urlopen(request, timeout=10) as response:
            return int(response.headers.get("Content-Length") or 0) or None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return None


# ---- validation: the model's request, normalised (and checked again when the user taps)
def validate_install(args: dict[str, Any], *, fetch: Callable[[str, int], str] = _fetch_text) -> dict[str, Any]:
    kind = str(args.get("kind") or "").strip()
    if kind not in KINDS:
        raise InstallError(f"kind 要是 {'、'.join(KINDS)} 其中之一")
    reason = " ".join(str(args.get("reason") or "").split())[:200]
    if not reason:
        raise InstallError("要寫 reason：為什麼需要裝這個")
    payload: dict[str, Any] = {"kind": kind, "reason": reason}
    name = str(args.get("name") or "").strip()
    version = str(args.get("version") or "").strip()
    if kind in {"pip", "npm", "brew"}:
        if not _PACKAGE_NAME[kind].match(name):
            raise InstallError(f"name 不是有效的 {kind} 套件名稱（不接受路徑、網址或選項）")
        if version and not _VERSION.match(version):
            raise InstallError("version 格式不對")
        if kind == "brew" and version:
            raise InstallError("brew 不指定版本")
        payload |= {"name": name, **({"version": version} if version else {})}
    elif kind == "model_file":
        filename = str(args.get("filename") or "").strip()
        sha = str(args.get("sha256") or "").strip().lower()
        if not _FILENAME.match(filename):
            raise InstallError("filename 不是有效的檔名")
        if not _SHA256.match(sha):
            raise InstallError("model_file 一定要給 64 位十六進位的 sha256")
        payload |= {"url": _https(args.get("url"), "url"), "filename": filename, "sha256": sha}
        size = _head_size(payload["url"])
        if size:
            payload["size_bytes"] = size
    elif kind == "script":
        payload |= {"url": _https(args.get("url"), "url")}
    elif kind == "git":
        url = str(args.get("url") or "").strip()
        if not (url.startswith("https://") or _GIT_SSH.match(url)) or any(c in url for c in " \n\r\t'\"`$;|&<>"):
            raise InstallError("git 來源要是 https 網址或 git@host:path")
        if not _NAME.match(name):
            raise InstallError("git 要給 name（存放的資料夾名稱）")
        ref = str(args.get("ref") or "").strip()
        if ref and not _VERSION.match(ref):
            raise InstallError("ref 格式不對")
        command = str(args.get("install_command") or "").strip()
        if command:
            if "\n" in command or len(command) > 500:
                raise InstallError("install_command 要是一行、500 字以內")
            if (why := forbidden_reason(command)) or _SUDO_WORD.search(command):
                raise InstallError(f"install_command 不能執行：{why or 'sudo'}")
        payload |= {"url": url, "name": name.replace("/", "_"), **({"ref": ref} if ref else {}), **({"install_command": command} if command else {})}
    else:  # skill
        skill = str(args.get("name") or "").strip()
        if not _SKILL_NAME.match(skill):
            raise InstallError("skill 的 name 要是小寫英數與 -")
        content = str(args.get("content") or "")
        if not content and args.get("url"):
            payload["url"] = _https(args.get("url"), "url")
            content = fetch(payload["url"], MAX_SKILL_BYTES)
        if not content.strip() or len(content.encode("utf-8")) > MAX_SKILL_BYTES:
            raise InstallError(f"skill 要有 content 或 url，內容在 {MAX_SKILL_BYTES} bytes 以內")
        payload |= {"name": skill, "content": content}
    return payload


def summarize(payload: dict[str, Any]) -> str:
    kind = payload["kind"]
    label = {
        "pip": lambda: f"Python 套件 {payload['name']}{('==' + payload['version']) if payload.get('version') else ''}",
        "npm": lambda: f"npm 套件 {payload['name']}{('@' + payload['version']) if payload.get('version') else ''}",
        "brew": lambda: f"Homebrew 套件 {payload['name']}",
        "model_file": lambda: f"模型檔 {payload['filename']}",
        "script": lambda: f"安裝腳本 {payload['url']}",
        "git": lambda: f"Git 專案 {payload['url']}",
        "skill": lambda: f"Skill {payload['name']}",
    }[kind]()
    return f"安裝{label}"


def card_lines(payload: dict[str, Any]) -> list[str]:
    """What the confirm card shows besides the title: where from, what will run, the honest warning."""
    kind = payload["kind"]
    lines = [f"用途：{payload['reason']}"]
    if kind == "pip":
        lines += ["來源：PyPI", "位置：工作區內 .harness/capabilities/python（不動系統）", "⚠️ 套件的安裝步驟可能執行該套件附帶的程式。"]
    elif kind == "npm":
        lines += ["來源：npm", "位置：工作區內 .harness/capabilities/node（不動系統）", "⚠️ 套件的安裝步驟可能執行該套件附帶的程式。"]
    elif kind == "brew":
        lines += ["來源：Homebrew", "⚠️ 會安裝到系統（Homebrew 全域），影響這台機器上的其他程式。"]
    elif kind == "model_file":
        size = payload.get("size_bytes")
        lines += [f"來源：{payload['url']}", f"檔案：{payload['filename']}" + (f"（約 {size / 1024 / 1024:.0f} MB）" if size else ""),
                  f"必須符合 SHA-256：{payload['sha256'][:16]}…（不符會刪除）"]
    elif kind == "script":
        lines += [f"來源：{payload['url']}", "⚠️ 會下載並執行網路上的腳本，內容無法事先審查；執行前會算出它的 SHA-256 並記錄。",
                  "腳本含 sudo 就不會執行（sudo 一律不行）。"]
    elif kind == "git":
        lines += [f"來源：{payload['url']}" + (f"（{payload['ref']}）" if payload.get("ref") else ""),
                  f"位置：.harness/capabilities/src/{payload['name']}",
                  "⚠️ 會下載第三方原始碼。"]
        if payload.get("install_command"):
            lines.append(f"之後會在該資料夾執行：{payload['install_command']}")
    else:
        body = payload["content"].strip()
        shown = body if len(body) <= 1500 else body[:1500] + f"\n…（共 {len(body)} 字，這裡只顯示開頭；完整內容會存起來）"
        lines += [f"位置：.harness/skills/{payload['name']}/SKILL.md（只給聊天模型參考，不給 Developer）", "內容：", shown]
    return lines


# ---- doing it
def _sudo_shim_dir() -> Path:
    shim = Path(tempfile.mkdtemp(prefix="harness-shim-"))
    for name in ("sudo", "doas"):
        path = shim / name
        path.write_text("#!/bin/sh\necho 'sudo is never run by the Harness' >&2\nexit 1\n")
        try:
            path.chmod(path.stat().st_mode | stat.S_IXUSR)
        except OSError:
            pass
        if os.name == "nt":
            cmd_path = shim / f"{name}.cmd"
            cmd_path.write_text("@echo sudo is never run by the Harness >&2\r\n@exit /b 1\r\n")
    return shim


def capabilities_env(env: dict[str, str], state_dir: Path) -> dict[str, str]:
    """`env` with what was installed on the PATH (and PYTHONPATH), and the sudo shim in front of it all."""
    caps = caps_dir(state_dir)
    paths = [str(caps / "node" / "node_modules" / ".bin"), str(caps / "python" / "bin"), str(caps / "bin")]
    out = dict(env)
    default_path = "" if os.name == "nt" else "/usr/bin:/bin"
    out["PATH"] = os.pathsep.join([*paths, out.get("PATH") or default_path])
    out["PYTHONPATH"] = os.pathsep.join(filter(None, [str(caps / "python"), out.get("PYTHONPATH", "")]))
    return out


def _download(url: str, dest: Path, limit: int) -> str:
    """Stream `url` to `dest`, returning its SHA-256; deleted and an error when it is bigger than `limit`."""
    digest = hashlib.sha256()
    total = 0
    request = urllib.request.Request(url, headers={"User-Agent": "memtrace-harness-install/1"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response, dest.open("wb") as out:
            while chunk := response.read(1024 * 1024):
                total += len(chunk)
                if total > limit:
                    raise InstallError(f"檔案超過上限 {limit // (1024 * 1024)} MB")
                digest.update(chunk)
                out.write(chunk)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        dest.unlink(missing_ok=True)
        raise InstallError(f"下載失敗：{exc}") from exc
    except InstallError:
        dest.unlink(missing_ok=True)
        raise
    return digest.hexdigest()


def _run(argv: list[str], *, cwd: Path | None, env: dict[str, str], runner: Callable[..., Any]) -> tuple[bool, str]:
    try:
        done = runner(argv, cwd=cwd, env=env, capture_output=True, text=True, timeout=INSTALL_SECONDS, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return False, f"超過 {INSTALL_SECONDS // 60} 分鐘，已中止"
    except OSError as exc:
        return False, f"無法執行 {argv[0]}：{exc}"
    out = ((done.stdout or "") + (done.stderr or "")).strip()
    return done.returncode == 0, (out[-1500:] if out else "")


def run_install(
    payload: dict[str, Any], state_dir: Path, *, base_env: dict[str, str] | None = None,
    runner: Callable[..., Any] = subprocess.run, download: Callable[[str, Path, int], str] = _download,
) -> tuple[bool, str, dict[str, Any]]:
    """Do one validated install. (ok, text for the user, manifest fields). Never raises for an install that merely fails."""
    kind = payload["kind"]
    caps = caps_dir(state_dir)
    caps.mkdir(parents=True, exist_ok=True)
    shim = _sudo_shim_dir()
    env = capabilities_env(base_env if base_env is not None else {"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin", "HOME": str(Path.home())}, state_dir)
    env["PATH"] = f"{shim}:{env['PATH']}"
    meta: dict[str, Any] = {}
    try:
        if kind == "pip":
            spec = payload["name"] + (f"=={payload['version']}" if payload.get("version") else "")
            ok, text = _run([sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--no-input", "--target", str(caps / "python"), spec], cwd=caps, env=env, runner=runner)
            meta["path"] = str(caps / "python")
        elif kind == "npm":
            spec = payload["name"] + (f"@{payload['version']}" if payload.get("version") else "")
            (caps / "node").mkdir(exist_ok=True)
            ok, text = _run(["npm", "install", "--prefix", str(caps / "node"), "--no-audit", "--no-fund", spec], cwd=caps, env=env, runner=runner)
            meta["path"] = str(caps / "node")
        elif kind == "brew":
            ok, text = _run(["brew", "install", payload["name"]], cwd=caps, env=env, runner=runner)
        elif kind == "model_file":
            models = caps / "models"
            models.mkdir(exist_ok=True)
            temp = models / (payload["filename"] + ".part")
            digest = download(payload["url"], temp, MAX_MODEL_BYTES)
            if digest != payload["sha256"]:
                temp.unlink(missing_ok=True)
                return False, f"下載的檔案 SHA-256 不符（得到 {digest[:16]}…），已刪除，沒有安裝。", {}
            temp.replace(models / payload["filename"])
            ok, text, meta = True, f"已存到 {models / payload['filename']}", {"path": str(models / payload["filename"]), "sha256": digest}
        elif kind == "script":
            temp = caps / f"script-{os.getpid()}.sh"
            digest = download(payload["url"], temp, MAX_SCRIPT_BYTES)
            body = temp.read_text(encoding="utf-8", errors="replace")
            if _SUDO_WORD.search(body):
                temp.unlink(missing_ok=True)
                return False, f"這個腳本裡有 sudo，不會執行（sha256 {digest[:16]}…）。sudo 一律只能由你自己操作。", {}
            ok, text = _run(["sh", str(temp)], cwd=caps, env=env, runner=runner)
            temp.unlink(missing_ok=True)
            meta["sha256"] = digest
        elif kind == "git":
            src = caps / "src"
            src.mkdir(exist_ok=True)
            dest = src / payload["name"]
            if dest.exists():
                return False, f"{dest} 已經存在，不會覆蓋。要重裝請你先自己處理那個資料夾。", {}
            argv = ["git", "clone", "--depth", "1", *(["--branch", payload["ref"]] if payload.get("ref") else []), payload["url"], str(dest)]
            ok, text = _run(argv, cwd=src, env=env, runner=runner)
            meta["path"] = str(dest)
            if ok and payload.get("install_command"):
                if forbidden_reason(payload["install_command"]) or _SUDO_WORD.search(payload["install_command"]):
                    return False, "install_command 屬於不能執行的類型，已 clone 但沒有執行它。", meta
                ok2, text2 = _run(["sh", "-c", payload["install_command"]], cwd=dest, env=env, runner=runner)
                ok, text = ok2, f"{text}\n{text2}".strip()
        else:  # skill
            target = skills_dir(state_dir) / payload["name"]
            target.mkdir(parents=True, exist_ok=True)
            (target / "SKILL.md").write_text(payload["content"], encoding="utf-8")
            os.chmod(target / "SKILL.md", 0o600)
            ok, text, meta = True, f"已存到 {target / 'SKILL.md'}", {"path": str(target), "sha256": hashlib.sha256(payload["content"].encode()).hexdigest()}
    except InstallError as exc:
        return False, str(exc), {}
    finally:
        shutil.rmtree(shim, ignore_errors=True)
    if ok:
        record(state_dir, payload, meta)
    return ok, (text or "完成"), meta


# ---- what is installed
def manifest_path(state_dir: Path) -> Path:
    return Path(state_dir) / MANIFEST


def load_manifest(state_dir: Path) -> list[dict]:
    try:
        data = json.loads(manifest_path(state_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [e for e in data.get("installed", []) if isinstance(e, dict)] if isinstance(data, dict) else []


def record(state_dir: Path, payload: dict[str, Any], meta: dict[str, Any]) -> None:
    entries = load_manifest(state_dir)
    entry = {
        "kind": payload["kind"], "name": payload.get("name") or payload.get("filename") or payload.get("url"),
        "version": payload.get("version"), "source": payload.get("url") or payload["kind"], "reason": payload["reason"],
        "installed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), **meta,
    }
    entries.append({k: v for k, v in entry.items() if v is not None})
    path = manifest_path(state_dir)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps({"installed": entries}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(temp, 0o600)
    temp.replace(path)


def skill_entries(state_dir: Path) -> list[tuple[str, str, Path]]:
    """(name, description, SKILL.md path) of the installed skills; the description is the frontmatter's, else the first line."""
    out = []
    root = skills_dir(state_dir)
    if not root.is_dir():
        return out
    for folder in sorted(p for p in root.iterdir() if p.is_dir()):
        path = folder / "SKILL.md"
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        match = re.search(r"(?im)^description:\s*(.+)$", text)
        first = next((ln.strip("# ").strip() for ln in text.splitlines() if ln.strip() and not ln.startswith("---")), "")
        out.append((folder.name, (match.group(1) if match else first)[:160], path))
    return out


def prompt_notice(state_dir: Path) -> str:
    """For the chat prompt: how to ask for an install, what is installed, which skills exist."""
    lines = [
        "缺少功能（例如沒有語音轉文字模型、要用的 CLI 或函式庫、某個 skill）時，用 `request_install` 向使用者提出安裝"
        "（kind：pip、npm、brew、model_file、script、git、skill）；一定要寫 reason，model_file 一定要給 sha256。"
        "它只是提出，使用者看過來源與指令、按確認才會裝；說「已請你確認」，不要說已經裝了。sudo 不能裝，需要 sudo 的把指令寫給使用者自己執行。"
        "不要在沒問使用者的情況下叫他自己去裝。"
    ]
    installed = load_manifest(state_dir)
    if installed:
        lines.append("已安裝：" + "、".join(f"{e.get('kind')}:{e.get('name')}" for e in installed[-12:]) + "（用法仍要走 job／工具，裝好不等於已授權執行）。")
    skills = skill_entries(state_dir)
    if skills:
        lines.append("已安裝的 skills（內容是使用者核准過的參考指引；相關時用 Read 讀它，不要硬套）：")
        lines += [f"- {name}：{desc}（{path}）" for name, desc, path in skills]
    return "\n".join(lines) + "\n"
