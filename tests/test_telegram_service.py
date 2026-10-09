"""The Telegram service, against a fake Bot API and a fake Harness client."""

import json
import urllib.error
import urllib.request
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from memtrace_services.client import HarnessError, HarnessUnavailable
from memtrace_services.telegram.relay import start_relay
from memtrace_services.telegram.service import (
    TelegramService,
    TelegramSettings,
    audience_for,
    message_ref,
    parse_audience,
    parse_message_ref,
)

ALLOWED = 12345


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str, list | None]] = []
        self.photos: list[tuple[int, str, str | None]] = []
        self.cleared: list[tuple[int, int]] = []
        self.answers: list[tuple[str, str | None]] = []
        self.typing: list[int] = []
        self.commands: list = []
        self.updates: list[list[dict]] = []
        self.fail_sends = False
        self._next_id = 100

    def get_updates(self, offset, timeout):
        return self.updates.pop(0) if self.updates else []

    def send_text(self, chat_id, text, keyboard=None):
        if self.fail_sends:
            return None
        self._next_id += 1
        self.sent.append((chat_id, text, keyboard))
        return self._next_id

    def send_photo(self, chat_id, photo, filename, caption=None):
        self._next_id += 1
        self.photos.append((chat_id, filename, caption))
        return self._next_id

    def clear_keyboard(self, chat_id, message_id):
        self.cleared.append((chat_id, message_id))
        return True

    def answer_callback(self, callback_id, text=None):
        self.answers.append((callback_id, text))

    def send_typing(self, chat_id):
        self.typing.append(chat_id)

    def set_commands(self, commands):
        self.commands = commands
        return True


class FakeClient:
    def __init__(self) -> None:
        self.messages: list[dict] = []
        self.actions: list[dict] = []
        self.acks: list[tuple[int, bool, list]] = []
        self.events_queue: list[dict] = []
        self.down = False
        self.toast: str | None = "ok"
        self.error: HarnessError | None = None

    def post_message(self, text, *, audience, reply_to_ref=None, reply_to_text=None):
        if self.down:
            raise HarnessUnavailable("refused")
        if self.error:
            raise self.error
        self.messages.append(
            {"text": text, "audience": audience, "reply_to_ref": reply_to_ref, "reply_to_text": reply_to_text}
        )

    def post_action(self, action_id, *, audience, message_ref=None):
        if self.down:
            raise HarnessUnavailable("refused")
        self.actions.append({"id": action_id, "audience": audience, "message_ref": message_ref})
        return self.toast

    def commands(self):
        return [{"command": "status", "description": "d"}]

    def events(self, *, limit=20, wait=0.0):
        events, self.events_queue = self.events_queue, []
        return events

    def ack(self, event_id, *, ok, message_refs=None):
        self.acks.append((event_id, ok, message_refs or []))
        return "delivered" if ok else "retry"


def make_service(tmp: str, **overrides):
    values = {
        "TELEGRAM_BOT_TOKEN": "bot-token",
        "TELEGRAM_ALLOWED_CHAT_IDS": str(ALLOWED),
        "HARNESS_API_TOKEN": "api-token",
        **overrides,
    }
    settings = TelegramSettings.from_values(values, env_file=Path(tmp) / "telegram.env")
    bot, client = FakeBot(), FakeClient()
    return TelegramService(settings, client, bot), bot, client


def text_update(chat_id=ALLOWED, text="hello", update_id=1, **extra):
    return {"update_id": update_id, "message": {"chat": {"id": chat_id}, "text": text, **extra}}


class SettingsTests(TestCase):
    def test_a_token_a_harness_token_and_an_allowlist_are_all_required(self) -> None:
        base = {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_ALLOWED_CHAT_IDS": "1", "HARNESS_API_TOKEN": "h"}
        TelegramSettings.from_values(base, env_file=None)
        for missing in base:
            with self.subTest(missing=missing), self.assertRaises(ValueError):
                TelegramSettings.from_values({k: v for k, v in base.items() if k != missing}, env_file=None)

    def test_chat_ids_may_be_negative_groups_and_junk_is_ignored(self) -> None:
        settings = TelegramSettings.from_values(
            {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_ALLOWED_CHAT_IDS": "1, -100200 ,abc,", "HARNESS_API_TOKEN": "h"},
            env_file=None,
        )
        self.assertEqual(settings.allowed_chat_ids, frozenset({1, -100200}))

    def test_addresses_roundtrip(self) -> None:
        self.assertEqual(parse_audience(audience_for(-5)), -5)
        self.assertEqual(parse_message_ref(message_ref(-5, 9)), (-5, 9))
        self.assertIsNone(parse_audience("slack:U1"))
        self.assertIsNone(parse_message_ref("tg:abc"))
        self.assertIsNone(parse_message_ref(None))


class IncomingTests(TestCase):
    def test_a_message_from_a_chat_outside_the_allowlist_never_reaches_the_harness(self) -> None:
        with TemporaryDirectory() as tmp:
            service, bot, client = make_service(tmp)
            self.assertTrue(service.process_update(text_update(chat_id=66666, text="/approve appr_x")))
            self.assertTrue(service.process_update({"update_id": 2, "callback_query": {
                "id": "cb", "data": "approve:appr_x", "message": {"chat": {"id": 66666}, "message_id": 5}}}))
        self.assertEqual((client.messages, client.actions), ([], []))
        self.assertEqual(bot.sent, [])
        self.assertEqual(bot.answers, [("cb", None)])  # the spinner is still stopped

    def test_an_allowed_message_is_relayed_with_its_address_and_the_message_it_replies_to(self) -> None:
        with TemporaryDirectory() as tmp:
            service, bot, client = make_service(tmp)
            service.process_update(
                text_update(text="那檔怎麼了", reply_to_message={"message_id": 7, "text": " 排程推播 "})
            )
        self.assertEqual(
            client.messages,
            [{"text": "那檔怎麼了", "audience": "tg:12345", "reply_to_ref": "tg:12345:7", "reply_to_text": "排程推播"}],
        )

    def test_a_non_text_update_is_dropped(self) -> None:
        with TemporaryDirectory() as tmp:
            service, _, client = make_service(tmp)
            self.assertTrue(service.process_update({"update_id": 1, "message": {"chat": {"id": ALLOWED}}}))
            self.assertTrue(service.process_update({"update_id": 2, "edited_message": {}}))
        self.assertEqual(client.messages, [])

    def test_when_the_harness_is_down_the_update_is_kept_for_the_next_round(self) -> None:
        with TemporaryDirectory() as tmp:
            service, bot, client = make_service(tmp)
            bot.updates = [[text_update(update_id=10), text_update(update_id=11)], [text_update(update_id=10)]]
            client.down = True
            self.assertEqual(service.poll_once(), 0)
            self.assertEqual(service.offset, 0)  # not advanced: nothing was lost
            client.down = False
            self.assertEqual(service.poll_once(), 1)
            self.assertEqual(service.offset, 11)

    def test_the_offset_survives_a_restart(self) -> None:
        with TemporaryDirectory() as tmp:
            service, bot, client = make_service(tmp)
            bot.updates = [[text_update(update_id=41)]]
            service.poll_once()
            again, _, _ = make_service(tmp)
            self.assertEqual(again.offset, 42)

    def test_a_refusal_from_the_harness_is_told_to_the_person(self) -> None:
        with TemporaryDirectory() as tmp:
            service, bot, client = make_service(tmp)
            client.error = HarnessError(400, "text is required")
            self.assertTrue(service.process_update(text_update()))
        self.assertIn("text is required", bot.sent[0][1])

    def test_a_tap_goes_to_the_harness_and_its_toast_is_shown(self) -> None:
        with TemporaryDirectory() as tmp:
            service, bot, client = make_service(tmp)
            client.toast = "已核准"
            service.process_update({"update_id": 1, "callback_query": {
                "id": "cb1", "data": "approve:appr_1", "message": {"chat": {"id": ALLOWED}, "message_id": 9}}})
        self.assertEqual(client.actions, [{"id": "approve:appr_1", "audience": "tg:12345", "message_ref": "tg:12345:9"}])
        self.assertEqual(bot.answers, [("cb1", "已核准")])

    def test_a_tap_while_the_harness_is_down_says_so_and_is_not_retried(self) -> None:
        with TemporaryDirectory() as tmp:
            service, bot, client = make_service(tmp)
            client.down = True
            done = service.process_update({"update_id": 1, "callback_query": {
                "id": "cb1", "data": "approve:appr_1", "message": {"chat": {"id": ALLOWED}, "message_id": 9}}})
        self.assertTrue(done)  # a stale button must not fire later
        self.assertIn("連不上", bot.answers[0][1])


class OutgoingTests(TestCase):
    def test_a_message_with_buttons_becomes_an_inline_keyboard_and_is_acknowledged_with_its_reference(self) -> None:
        with TemporaryDirectory() as tmp:
            service, bot, client = make_service(tmp)
            client.events_queue = [{
                "id": 5, "kind": "message", "audience": None, "text": "要核准嗎？",
                "actions": [[{"label": "✅ 核准", "id": "approve:appr_1"}, {"label": "❌ 拒絕", "id": "reject:appr_1"}]],
            }]
            self.assertEqual(service.deliver_once(), 1)
        chat, text, keyboard = bot.sent[0]
        self.assertEqual((chat, text), (ALLOWED, "要核准嗎？"))
        self.assertEqual(
            keyboard,
            [[{"text": "✅ 核准", "callback_data": "approve:appr_1"}, {"text": "❌ 拒絕", "callback_data": "reject:appr_1"}]],
        )
        self.assertEqual(client.acks, [(5, True, ["tg:12345:101"])])

    def test_no_audience_goes_to_every_allowed_chat_and_a_given_one_only_there(self) -> None:
        with TemporaryDirectory() as tmp:
            service, bot, client = make_service(tmp, TELEGRAM_ALLOWED_CHAT_IDS="1,2")
            client.events_queue = [
                {"id": 1, "kind": "message", "audience": None, "text": "to all", "actions": None},
                {"id": 2, "kind": "message", "audience": "tg:2", "text": "to two", "actions": None},
            ]
            service.deliver_once()
        self.assertEqual([(c, t) for c, t, _ in bot.sent], [(1, "to all"), (2, "to all"), (2, "to two")])

    def test_an_address_that_is_not_ours_or_not_allowlisted_is_never_sent_but_is_acknowledged(self) -> None:
        with TemporaryDirectory() as tmp:
            service, bot, client = make_service(tmp)
            client.events_queue = [
                {"id": 1, "kind": "message", "audience": "tg:999", "text": "x", "actions": None},
                {"id": 2, "kind": "message", "audience": "slack:U1", "text": "y", "actions": None},
            ]
            service.deliver_once()
        self.assertEqual(bot.sent, [])
        self.assertEqual([(i, ok) for i, ok, _ in client.acks], [(1, True), (2, True)])

    def test_a_failed_send_is_acknowledged_as_failed_so_the_harness_offers_it_again(self) -> None:
        with TemporaryDirectory() as tmp:
            service, bot, client = make_service(tmp)
            bot.fail_sends = True
            client.events_queue = [{"id": 3, "kind": "message", "audience": None, "text": "x", "actions": None}]
            service.deliver_once()
        self.assertEqual(client.acks, [(3, False, [])])

    def test_clear_and_typing_events(self) -> None:
        with TemporaryDirectory() as tmp:
            service, bot, client = make_service(tmp)
            client.events_queue = [
                {"id": 1, "kind": "clear", "ref": "tg:12345:77"},
                {"id": 2, "kind": "typing", "audience": "tg:12345"},
                {"id": 3, "kind": "from-the-future", "audience": None},
            ]
            service.deliver_once()
        self.assertEqual(bot.cleared, [(ALLOWED, 77)])
        self.assertEqual(bot.typing, [ALLOWED])
        self.assertEqual([ok for _, ok, _ in client.acks], [True, True, True])

    def test_the_bot_commands_are_registered_once_the_harness_answers(self) -> None:
        with TemporaryDirectory() as tmp:
            service, bot, client = make_service(tmp)
            service.deliver_once()
            self.assertEqual(bot.commands, [{"command": "status", "description": "d"}])
            bot.commands = []
            service.deliver_once()
            self.assertEqual(bot.commands, [])  # not again


class RelayTests(TestCase):
    def _post(self, port, path, body, token="relay-token"):
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}{path}", data=json.dumps(body).encode(), method="POST",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def _relay(self, tmp, **extra):
        service, bot, _ = make_service(
            tmp, TELEGRAM_RELAY_PORT="0", TELEGRAM_RELAY_TOKEN="relay-token", **extra
        )
        server = start_relay(service)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server.server_address[1], bot

    def test_a_script_posts_text_with_its_own_token_and_never_sees_the_bot_token(self) -> None:
        with TemporaryDirectory() as tmp:
            port, bot = self._relay(tmp)
            self.assertEqual(self._post(port, "/v1/send", {"text": "收盤了"})[0], 200)
            self.assertEqual(self._post(port, "/v1/send", {"text": "x"}, token="wrong")[0], 401)
            self.assertEqual(self._post(port, "/v1/send", {})[0], 400)
        self.assertEqual([(c, t) for c, t, _ in bot.sent], [(ALLOWED, "收盤了")])

    def test_photos_are_read_only_from_the_configured_roots(self) -> None:
        with TemporaryDirectory() as tmp:
            allowed_dir = Path(tmp) / "charts"
            allowed_dir.mkdir()
            (allowed_dir / "taiex.png").write_bytes(b"png")
            (Path(tmp) / "secret.txt").write_text("nope", encoding="utf-8")
            port, bot = self._relay(tmp, TELEGRAM_RELAY_PHOTO_ROOTS=str(allowed_dir))
            ok = self._post(port, "/v1/send_photo", {"path": str(allowed_dir / "taiex.png"), "caption": "加權指數"})
            outside = self._post(port, "/v1/send_photo", {"path": str(Path(tmp) / "secret.txt")})
            traversal = self._post(port, "/v1/send_photo", {"path": str(allowed_dir / ".." / "secret.txt")})
        self.assertEqual(ok[0], 200)
        self.assertEqual((outside[0], traversal[0]), (400, 400))
        self.assertEqual(bot.photos, [(ALLOWED, "taiex.png", "加權指數")])

    def test_photos_are_refused_when_no_root_is_configured(self) -> None:
        with TemporaryDirectory() as tmp:
            port, bot = self._relay(tmp)
            self.assertEqual(self._post(port, "/v1/send_photo", {"path": "/etc/hosts"})[0], 400)
        self.assertEqual(bot.photos, [])

    def test_the_relay_needs_a_token(self) -> None:
        with TemporaryDirectory() as tmp:
            service, _, _ = make_service(tmp, TELEGRAM_RELAY_PORT="0")
            with self.assertRaises(ValueError):
                start_relay(service)
