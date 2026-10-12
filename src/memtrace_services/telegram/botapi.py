"""The little of Telegram's Bot API this service uses, over plain HTTPS (no SDK)."""

from __future__ import annotations

import json
import logging
import secrets
import urllib.error
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)

# Telegram's sendMessage rejects any text over 4096 UTF-16 code units; stay comfortably
# under that in plain characters so multi-byte text (e.g. Chinese) never trips it.
TELEGRAM_MAX_MESSAGE_LENGTH = 3500


def _split_telegram_text(text: str, limit: int = TELEGRAM_MAX_MESSAGE_LENGTH) -> list[str]:
    """Split text into <=limit-char chunks so a long report arrives as several readable
    messages instead of being truncated or rejected by Telegram. Breaks at the last
    paragraph/line boundary that fits, else the last sentence end, else the last space;
    only text with none of those (one unbroken run longer than `limit`) is cut hard."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        window = remaining[:limit]
        split_at = max(window.rfind("\n\n"), window.rfind("\n"))
        if split_at < limit // 4:
            split_at = max(
                (window.rfind(mark) + len(mark) for mark in ("。", "！", "？", ". ", "! ", "? ")
                 if window.rfind(mark) != -1),
                default=-1,
            )
        if split_at < limit // 4:
            split_at = window.rfind(" ")
        if split_at < limit // 4:
            split_at = limit
        chunks.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip("\n ")
    if remaining:
        chunks.append(remaining)
    return chunks


class BotApi:
    def __init__(self, token: str, *, api_base: str = "https://api.telegram.org") -> None:
        self._token = token
        self._base = api_base.rstrip("/")

    def _call(self, method: str, payload: dict[str, Any], *, timeout: float = 10.0) -> dict | None:
        url = f"{self._base}/bot{self._token}/{method}"
        try:
            request = urllib.request.Request(
                url,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            return data if data.get("ok") else None
        except urllib.error.HTTPError as exc:
            # Telegram's reason for refusing (chat not found, message not modified...) is in the body.
            try:
                detail = json.loads(exc.read().decode("utf-8")).get("description", exc.reason)
            except Exception:
                detail = exc.reason
            logger.error(f"Telegram {method} failed: HTTP {exc.code} {detail}")
            return None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            # The error text of a urllib failure never contains the URL (and so the token).
            logger.error(f"Telegram {method} failed: {exc}")
            return None

    def get_updates(self, offset: int, timeout: int) -> list[dict[str, Any]] | None:
        """None on failure (so the caller backs off), a possibly empty list otherwise."""
        data = self._call(
            "getUpdates",
            {"offset": offset, "timeout": timeout, "allowed_updates": ["message", "callback_query"]},
            timeout=timeout + 5,
        )
        return None if data is None else data.get("result", [])

    def send_text(self, chat_id: int, text: str, keyboard: list[list[dict[str, str]]] | None = None) -> int | None:
        """Send `text` (split if long); the keyboard rides on the last chunk, where it will be
        tapped. Plain text, deliberately no parse_mode: most fields interpolated into these
        messages (goal text, reasons, recommendations) are arbitrary and can contain unescaped
        Markdown characters — Telegram's Markdown mode then fails the whole send with "can't parse
        entities" and the message never arrives. Reliability beats styling. Returns the last
        message's id, or None if any chunk failed."""
        chunks = _split_telegram_text(text) or [text]
        last_id: int | None = None
        for index, chunk in enumerate(chunks):
            payload: dict[str, Any] = {"chat_id": chat_id, "text": chunk}
            if keyboard and index == len(chunks) - 1:
                payload["reply_markup"] = {"inline_keyboard": keyboard}
            data = self._call("sendMessage", payload)
            if data is None:
                return None
            result = data.get("result")
            last_id = result.get("message_id") if isinstance(result, dict) else last_id
        return last_id

    def send_photo(self, chat_id: int, photo: bytes, filename: str, caption: str | None = None) -> int | None:
        """multipart upload; returns the message id or None."""
        boundary = "----memtrace" + secrets.token_hex(8)
        parts: list[bytes] = []

        def field(name: str, value: str) -> None:
            parts.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode("utf-8")
            )

        field("chat_id", str(chat_id))
        if caption:
            field("caption", caption[:1024])
        parts.append(
            (
                f'--{boundary}\r\nContent-Disposition: form-data; name="photo"; filename="{filename}"\r\n'
                "Content-Type: application/octet-stream\r\n\r\n"
            ).encode("utf-8")
            + photo
            + b"\r\n"
        )
        parts.append(f"--{boundary}--\r\n".encode("utf-8"))
        url = f"{self._base}/bot{self._token}/sendPhoto"
        try:
            request = urllib.request.Request(
                url,
                data=b"".join(parts),
                headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=60) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            result = data.get("result") if data.get("ok") else None
            return result.get("message_id") if isinstance(result, dict) else None
        except urllib.error.HTTPError as exc:
            logger.error(f"Telegram sendPhoto failed: HTTP {exc.code}")
            return None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            logger.error(f"Telegram sendPhoto failed: {exc}")
            return None

    def download_file(self, file_id: str, max_bytes: int) -> tuple[bytes | None, str]:
        """(bytes, "") or (None, reason). Bots can download files up to 20 MB; a bigger one is refused by Telegram."""
        data = self._call("getFile", {"file_id": file_id})
        info = (data or {}).get("result") if isinstance(data, dict) else None
        if not isinstance(info, dict) or not info.get("file_path"):
            return None, "Telegram 沒有給出檔案（可能超過 20 MB）"
        if int(info.get("file_size") or 0) > max_bytes:
            return None, f"檔案超過 {max_bytes // (1024 * 1024)} MB"
        url = f"{self._base}/file/bot{self._token}/{info['file_path']}"
        try:
            with urllib.request.urlopen(url, timeout=60) as resp:
                body = resp.read(max_bytes + 1)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            return None, f"下載失敗：{exc}"  # a urllib error text never carries the URL (and so the token)
        if len(body) > max_bytes:
            return None, f"檔案超過 {max_bytes // (1024 * 1024)} MB"
        return body, ""

    def edit_text(self, chat_id: int, message_id: int, text: str, keyboard: list[list[dict[str, str]]] | None = None) -> bool:
        """Replace a delivered message's text (and buttons) in place; only the first chunk of a long text is kept."""
        chunks = _split_telegram_text(text) or [text]
        payload: dict[str, Any] = {"chat_id": chat_id, "message_id": message_id, "text": chunks[0]}
        payload["reply_markup"] = {"inline_keyboard": keyboard or []}
        return self._call("editMessageText", payload) is not None

    def clear_keyboard(self, chat_id: int, message_id: int) -> bool:
        return (
            self._call(
                "editMessageReplyMarkup",
                {"chat_id": chat_id, "message_id": message_id, "reply_markup": {"inline_keyboard": []}},
            )
            is not None
        )

    def answer_callback(self, callback_id: str, text: str | None = None) -> None:
        """Required after a button tap — until it is called Telegram keeps a spinner on the button."""
        payload: dict[str, Any] = {"callback_query_id": callback_id}
        if text:
            payload["text"] = text[:200]
        self._call("answerCallbackQuery", payload)

    def send_typing(self, chat_id: int) -> None:
        self._call("sendChatAction", {"chat_id": chat_id, "action": "typing"})

    def set_commands(self, commands: list[dict[str, str]]) -> bool:
        return self._call("setMyCommands", {"commands": commands}) is not None
