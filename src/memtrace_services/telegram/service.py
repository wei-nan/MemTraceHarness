"""The Telegram service proper: Telegram updates in, Harness outbox events out."""

from __future__ import annotations

import base64
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from memtrace_services.client import HarnessClient, HarnessError, HarnessUnavailable
from memtrace_services.telegram.botapi import BotApi

logger = logging.getLogger(__name__)

MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024  # what Telegram lets a bot download

AUDIENCE_PREFIX = "tg:"
# A getUpdates call that comes back empty this fast failed rather than long-polled; back off.
POLL_FAILURE_BACKOFF_SECONDS = 3.0
DELIVERY_IDLE_WAIT_SECONDS = 25.0
DELIVERY_FAILURE_BACKOFF_SECONDS = 3.0


@dataclass
class TelegramSettings:
    bot_token: str
    allowed_chat_ids: frozenset[int]
    harness_url: str
    harness_token: str
    state_file: Path
    poll_timeout: int = 30
    relay_port: int | None = None
    relay_token: str | None = None
    relay_photo_roots: tuple[Path, ...] = field(default_factory=tuple)
    # Where the Bot API lives; only ever changed to point a test or a corporate proxy at something else.
    api_base: str = "https://api.telegram.org"

    @classmethod
    def from_values(cls, values: dict[str, str], *, env_file: Path | None) -> "TelegramSettings":
        token = values.get("TELEGRAM_BOT_TOKEN", "").strip()
        if not token:
            raise ValueError("TELEGRAM_BOT_TOKEN is not set")
        harness_token = values.get("HARNESS_API_TOKEN", "").strip()
        if not harness_token:
            raise ValueError("HARNESS_API_TOKEN is not set (it must match the Harness's .harness.env)")
        allowed = frozenset(
            int(c.strip())
            for c in values.get("TELEGRAM_ALLOWED_CHAT_IDS", "").split(",")
            if c.strip().lstrip("-").isdigit()
        )
        if not allowed:
            raise ValueError("TELEGRAM_ALLOWED_CHAT_IDS is empty: a bot nobody may talk to is useless")
        state = values.get("TELEGRAM_STATE_FILE")
        state_file = Path(state) if state else (env_file.with_suffix(".state.json") if env_file else Path("telegram.state.json"))
        relay_port = values.get("TELEGRAM_RELAY_PORT", "").strip()
        roots = tuple(
            Path(p.strip()).resolve() for p in values.get("TELEGRAM_RELAY_PHOTO_ROOTS", "").split(",") if p.strip()
        )
        return cls(
            bot_token=token,
            allowed_chat_ids=allowed,
            harness_url=values.get("HARNESS_URL", "http://127.0.0.1:8800"),
            harness_token=harness_token,
            state_file=state_file,
            poll_timeout=int(values.get("TELEGRAM_POLL_TIMEOUT", "30") or 30),
            relay_port=int(relay_port) if relay_port else None,
            relay_token=values.get("TELEGRAM_RELAY_TOKEN", "").strip() or None,
            relay_photo_roots=roots,
            api_base=values.get("TELEGRAM_API_BASE", "").strip() or "https://api.telegram.org",
        )


def audience_for(chat_id: int) -> str:
    return f"{AUDIENCE_PREFIX}{chat_id}"


def parse_audience(audience: str | None) -> int | None:
    if audience and audience.startswith(AUDIENCE_PREFIX):
        tail = audience[len(AUDIENCE_PREFIX):]
        if tail.lstrip("-").isdigit():
            return int(tail)
    return None


def message_ref(chat_id: int, message_id: int) -> str:
    return f"{AUDIENCE_PREFIX}{chat_id}:{message_id}"


def parse_message_ref(ref: str | None) -> tuple[int, int] | None:
    if not ref or not ref.startswith(AUDIENCE_PREFIX):
        return None
    chat, _, message = ref[len(AUDIENCE_PREFIX):].rpartition(":")
    if chat.lstrip("-").isdigit() and message.isdigit():
        return int(chat), int(message)
    return None


class TelegramService:
    def __init__(self, settings: TelegramSettings, client: HarnessClient, bot: BotApi) -> None:
        self.settings = settings
        self.client = client
        self.bot = bot
        self.offset = self._load_offset()
        self._commands_registered = False

    # ---- state
    def _load_offset(self) -> int:
        try:
            return int(json.loads(self.settings.state_file.read_text(encoding="utf-8")).get("offset", 0))
        except (OSError, ValueError):
            return 0

    def _save_offset(self) -> None:
        try:
            self.settings.state_file.parent.mkdir(parents=True, exist_ok=True)
            self.settings.state_file.write_text(json.dumps({"offset": self.offset}), encoding="utf-8")
        except OSError:
            logger.exception("could not save the Telegram update offset")

    # ---- Telegram -> Harness
    def process_update(self, update: dict) -> bool:
        """Relay one update. True when it is done with (relayed, or deliberately dropped), False
        when the Harness could not be reached and the update should be tried again."""
        callback = update.get("callback_query")
        if callback:
            return self._relay_callback(callback)
        message = update.get("message")
        if not message:
            return True
        chat_id = (message.get("chat") or {}).get("id")
        if chat_id is None or chat_id not in self.settings.allowed_chat_ids:
            # HARD REQUIREMENT: any chat not on the allowlist is ignored at the transport layer.
            return True
        text = message.get("text") or message.get("caption") or ""
        media = self._media_of(message)
        if not text and not media:
            return True
        attachments: list[dict] = []
        for item in media:
            data, problem = self.bot.download_file(item["file_id"], MAX_DOWNLOAD_BYTES)
            if data is None:
                self.bot.send_text(chat_id, f"⚠️ 「{item['name']}」沒有收到：{problem}")
                continue
            attachments.append({"name": item["name"], "mime": item["mime"], "data_b64": base64.b64encode(data).decode("ascii")})
        if media and not attachments and not text:
            return True  # nothing arrived and nothing was said: the person has been told why
        reply_to = message.get("reply_to_message") or {}
        reply_text = reply_to.get("text") or reply_to.get("caption")
        reply_text = reply_text.strip() if isinstance(reply_text, str) else None
        reply_ref = (
            message_ref(chat_id, reply_to["message_id"]) if reply_to.get("message_id") is not None else None
        )
        try:
            self.client.post_message(
                text,
                audience=audience_for(chat_id),
                reply_to_ref=reply_ref,
                reply_to_text=reply_text or None,
                attachments=attachments or None,
            )
        except HarnessUnavailable as exc:
            logger.error(f"the Harness is unreachable: {exc}")
            return False
        except HarnessError as exc:
            logger.error(f"the Harness refused a message: {exc}")
            self.bot.send_text(chat_id, f"⚠️ Harness 拒絕了這則訊息：{exc.message}")
        return True

    @staticmethod
    def _media_of(message: dict) -> list[dict]:
        """The files a message carries: photo (largest size), document, voice, audio, video, video note, animation."""
        found: list[dict] = []
        photo = message.get("photo")
        if isinstance(photo, list) and photo:
            best = max((p for p in photo if isinstance(p, dict) and p.get("file_id")), key=lambda p: p.get("file_size") or 0, default=None)
            if best:
                found.append({"file_id": best["file_id"], "name": "photo.jpg", "mime": "image/jpeg"})
        for key, default_name, default_mime in (
            ("document", "document", ""), ("voice", "voice.ogg", "audio/ogg"), ("audio", "audio.mp3", "audio/mpeg"),
            ("video", "video.mp4", "video/mp4"), ("video_note", "video_note.mp4", "video/mp4"),
            ("animation", "animation.mp4", "video/mp4"),
        ):
            item = message.get(key)
            if isinstance(item, dict) and item.get("file_id"):
                found.append({
                    "file_id": item["file_id"],
                    "name": item.get("file_name") or default_name,
                    "mime": item.get("mime_type") or default_mime,
                })
        return found

    def _relay_callback(self, callback: dict) -> bool:
        callback_id = callback.get("id")
        source = callback.get("message") or {}
        chat_id = (source.get("chat") or {}).get("id")
        if chat_id is None or chat_id not in self.settings.allowed_chat_ids:
            if callback_id:
                self.bot.answer_callback(callback_id)
            return True
        message_id = source.get("message_id")
        try:
            toast = self.client.post_action(
                callback.get("data") or "",
                audience=audience_for(chat_id),
                message_ref=message_ref(chat_id, message_id) if message_id is not None else None,
            )
        except HarnessUnavailable:
            if callback_id:
                self.bot.answer_callback(callback_id, "Harness 暫時連不上，請稍後再按一次")
            return True  # the tap is not retried: a stale button must not fire later
        except HarnessError as exc:
            if callback_id:
                self.bot.answer_callback(callback_id, f"失敗：{exc.message}")
            return True
        if callback_id:
            self.bot.answer_callback(callback_id, toast)
        return True

    def poll_once(self, timeout: int | None = None) -> int:
        """One getUpdates round. Returns how many updates were relayed."""
        updates = self.bot.get_updates(self.offset, self.settings.poll_timeout if timeout is None else timeout)
        if updates is None:
            return 0
        relayed = 0
        for update in updates:
            if not self.process_update(update):
                break  # the Harness is down: keep this update (and the rest) for the next round
            self.offset = max(self.offset, update.get("update_id", 0) + 1)
            relayed += 1
        if relayed:
            self._save_offset()
        return relayed

    def poll_forever(self, stop: threading.Event) -> None:
        while not stop.is_set():
            started = time.monotonic()
            try:
                relayed = self.poll_once()
            except Exception:
                logger.exception("polling Telegram failed; continuing")
                relayed = 0
            if not relayed and time.monotonic() - started < 1.0:
                stop.wait(POLL_FAILURE_BACKOFF_SECONDS)

    # ---- Harness -> Telegram
    def _targets(self, audience: str | None) -> list[int]:
        if audience is None:
            return sorted(self.settings.allowed_chat_ids)
        chat = parse_audience(audience)
        if chat is not None and chat in self.settings.allowed_chat_ids:
            return [chat]
        return []  # not ours (another transport's address) or not allowlisted: never sent

    def deliver(self, event: dict) -> tuple[bool, list[str]]:
        """Deliver one outbox event. Returns (ok, message refs)."""
        kind = event.get("kind")
        if kind == "clear":
            parsed = parse_message_ref(event.get("ref"))
            if parsed:
                self.bot.clear_keyboard(*parsed)
            return True, []
        if kind == "edit":
            parsed = parse_message_ref(event.get("ref"))
            if parsed:
                keyboard = [
                    [{"text": button["label"], "callback_data": button["id"]} for button in row]
                    for row in (event.get("actions") or [])
                ] or None
                # An edit that changes nothing is refused by Telegram ("message is not modified"); the state it
                # would have shown is already there, so a failed edit is logged by the bot and not retried.
                self.bot.edit_text(*parsed, event.get("text") or "", keyboard)
            return True, []
        targets = self._targets(event.get("audience"))
        if kind == "typing":
            for chat in targets:
                self.bot.send_typing(chat)
            return True, []
        if kind != "message":
            logger.warning(f"unknown outbox event kind {kind!r}; dropping it")
            return True, []
        if not targets:
            logger.warning(f"outbox event {event.get('id')} has no deliverable target; dropping it")
            return True, []
        keyboard = [
            [{"text": button["label"], "callback_data": button["id"]} for button in row]
            for row in (event.get("actions") or [])
        ] or None
        refs: list[str] = []
        ok = True
        for chat in targets:
            message_id = self.bot.send_text(chat, event.get("text") or "", keyboard)
            if message_id is None:
                ok = False
            else:
                refs.append(message_ref(chat, message_id))
        return ok, refs

    def deliver_once(self, wait: float = 0.0) -> int:
        """Fetch what the Harness wants said, deliver it, acknowledge it. Returns the count."""
        events = self.client.events(limit=20, wait=wait)
        if not self._commands_registered:
            # The Harness answered, so it can list its commands (it may have started after us).
            self.register_commands()
        for event in events:
            try:
                ok, refs = self.deliver(event)
            except Exception:
                logger.exception(f"delivering outbox event {event.get('id')} failed")
                ok, refs = False, []
            self.client.ack(event["id"], ok=ok, message_refs=refs)
        return len(events)

    def deliver_forever(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                self.deliver_once(wait=DELIVERY_IDLE_WAIT_SECONDS)
            except HarnessUnavailable as exc:
                logger.warning(f"the Harness is unreachable ({exc}); retrying")
                stop.wait(DELIVERY_FAILURE_BACKOFF_SECONDS)
            except Exception:
                logger.exception("fetching outbox events failed; continuing")
                stop.wait(DELIVERY_FAILURE_BACKOFF_SECONDS)

    def register_commands(self) -> None:
        try:
            self._commands_registered = self.bot.set_commands(self.client.commands())
        except (HarnessUnavailable, HarnessError) as exc:
            logger.warning(f"could not register the bot commands: {exc}")
