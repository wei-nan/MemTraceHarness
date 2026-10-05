from __future__ import annotations

import io
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import MagicMock, patch

from memtrace_harness import taiwantrade_mcp as tw
from memtrace_harness.telegram_gateway import TelegramGateway
from memtrace_harness.trace_store import TraceStore
from tests.test_telegram_gateway import _build_schedule_gateway

TOKEN = "SECRET-ONE-TIME-TOKEN"


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def intent_opener(seen: list, *, token: str | None = TOKEN, intent_id: str = "intent-1"):
    def opener(request, timeout=None):
        seen.append(request)
        body = {
            "intent_id": intent_id,
            "status": "PendingConfirmation",
            "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
            "risk_snapshot": {"lot_size": 1},
            "confirmation_token": token,
        }
        return FakeResponse(json.dumps(body).encode())

    return opener


ARGS = {"symbol": "2327", "action": "Buy", "price": "628", "quantity": "1", "is_odd_lot": "true"}


class OrderProxyTests(TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = TraceStore(Path(self._tmp.name) / "t.sqlite3")
        self.ctx = tw.OrderContext(project="TWTradingStrategy", store=self.store)

    def _create(self, args=None, opener=None, **kwargs):
        seen: list = []
        text = tw.create_order_intent(
            args or ARGS,
            context=kwargs.pop("context", self.ctx),
            base_url="http://x/api/v1",
            api_key="k",
            opener=opener or intent_opener(seen),
            **kwargs,
        )
        return text, seen

    def test_the_confirmation_token_never_reaches_the_model(self) -> None:
        text, seen = self._create()
        self.assertNotIn(TOKEN, text)
        self.assertNotIn("confirmation_token", text)
        row = self.store.get_order_intent("intent-1")
        self.assertEqual(row["confirmation_token"], TOKEN)   # parked for the gateway only
        self.assertEqual((row["status"], row["project"]), ("pending", "TWTradingStrategy"))
        self.assertEqual(row["estimated_value"], 628.0)

    def test_it_only_creates_an_intent_and_never_calls_the_orders_endpoint(self) -> None:
        _, seen = self._create()
        self.assertEqual([r.full_url for r in seen], ["http://x/api/v1/trade/order-intents"])
        body = json.loads(seen[0].data)
        self.assertEqual(
            body,
            {"symbol": "2327", "action": "Buy", "price": 628.0, "quantity": 1,
             "price_type": "LMT", "order_type": "ROD", "is_odd_lot": True},
        )
        self.assertTrue(seen[0].get_header("X-idempotency-key").startswith("harness-"))

    def test_a_retried_proposal_maps_to_the_same_intent(self) -> None:
        _, first = self._create(now=1_000_000.0)
        _, second = self._create(now=1_000_010.0)
        self.assertEqual(
            first[0].get_header("X-idempotency-key"), second[0].get_header("X-idempotency-key")
        )
        _, other = self._create({**ARGS, "quantity": "2"}, now=1_000_010.0)
        self.assertNotEqual(
            first[0].get_header("X-idempotency-key"), other[0].get_header("X-idempotency-key")
        )

    def test_a_repeat_of_a_pending_proposal_issues_no_second_token_but_reshows_the_buttons(self) -> None:
        self._create()
        self.store.claim_pending_order_intents(["TWTradingStrategy"])    # shown once: awaiting
        text, _ = self._create(opener=intent_opener([], token=None))
        self.assertIn("buttons were sent to their Telegram again", text)
        self.assertEqual(self.store.get_order_intent("intent-1")["confirmation_token"], TOKEN)
        self.assertEqual(self.store.get_order_intent("intent-1")["status"], "pending")

    def test_a_repeat_of_a_finished_proposal_is_not_reshown(self) -> None:
        self._create()
        self.store.claim_pending_order_intents(["TWTradingStrategy"])
        self.store.finish_order_intent("intent-1", "submitted")
        text, _ = self._create(opener=intent_opener([], token=None))
        self.assertIn("already created", text)
        self.assertEqual(self.store.get_order_intent("intent-1")["status"], "submitted")

    def test_an_order_above_the_value_limit_is_refused_before_any_request(self) -> None:
        ctx = tw.OrderContext(project="P", store=self.store, max_order_value=1000)
        seen: list = []
        with self.assertRaises(tw.ProxyError) as caught:
            tw.create_order_intent(
                {**ARGS, "quantity": "2"}, context=ctx, base_url="u", api_key="k",
                opener=intent_opener(seen),
            )
        self.assertIn("harness limit", str(caught.exception))
        self.assertEqual(seen, [])
        # A whole lot is 1000 shares, so the same price/quantity weighs 1000x more.
        with self.assertRaises(tw.ProxyError):
            tw.create_order_intent(
                {**ARGS, "is_odd_lot": "false"}, context=tw.OrderContext("P", self.store, 100_000),
                base_url="u", api_key="k", opener=intent_opener(seen),
            )

    def test_malformed_or_smuggled_arguments_are_rejected(self) -> None:
        for bad in (
            {**ARGS, "action": "Hold"},
            {**ARGS, "price": "abc"},
            {**ARGS, "price": "0"},
            {**ARGS, "symbol": "../trade"},
            {**ARGS, "quantity": "-1"},
            {**ARGS, "price_type": "MKT"},       # not an accepted argument at all
            {**ARGS, "confirmation_token": "x"},
        ):
            with self.subTest(bad=bad), self.assertRaises(tw.ProxyError):
                self._create(bad)

    def test_the_order_tool_exists_only_when_a_project_was_enabled(self) -> None:
        listing = lambda ctx: [  # noqa: E731
            t["name"]
            for t in tw.handle_message(
                {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                base_url="u", api_key_loader=lambda: "k", order_context=ctx,
            )["result"]["tools"]
        ]
        self.assertNotIn("create_order_intent", listing(None))
        self.assertIn("create_order_intent", listing(self.ctx))
        # The model may only *propose* a trade or a cancellation; nothing it can call places,
        # confirms, cancels or amends for real.
        proposals = {"create_order_intent", "request_order_cancel"}
        self.assertEqual(set(listing(self.ctx)) - set(listing(None)), proposals)
        for name in listing(self.ctx):
            if name not in proposals:
                for word in ("place", "confirm", "cancel", "amend", "submit", "delete", "update"):
                    self.assertNotIn(word, name)

    def test_calling_the_order_tool_without_an_enabled_project_is_an_error(self) -> None:
        reply = tw.handle_message(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "create_order_intent", "arguments": ARGS}},
            base_url="u", api_key_loader=lambda: "k", opener=intent_opener([]),
        )
        self.assertTrue(reply["result"]["isError"])

    def test_spec_enables_orders_only_for_listed_projects(self) -> None:
        env = {
            "HARNESS_TAIWANTRADE_API_KEY_FILE": "/k",
            "HARNESS_TAIWANTRADE_ORDER_PROJECTS": "TWTradingStrategy, Other",
        }
        db = Path(self._tmp.name) / "t.sqlite3"
        on = tw.mcp_server_spec(env, order_project="twtradingstrategy", trace_db_path=db)
        self.assertEqual(on["env"]["HARNESS_ORDER_PROJECT"], "twtradingstrategy")
        self.assertEqual(on["env"]["HARNESS_TRACE_DB"], str(db.resolve()))
        self.assertNotIn("HARNESS_ORDER_PROJECT", tw.mcp_server_spec(env, order_project="Beri", trace_db_path=db)["env"])
        self.assertNotIn("HARNESS_ORDER_PROJECT", tw.mcp_server_spec(env)["env"])
        # Nobody is listed by default.
        bare = {"HARNESS_TAIWANTRADE_API_KEY_FILE": "/k"}
        self.assertNotIn(
            "HARNESS_ORDER_PROJECT",
            tw.mcp_server_spec(bare, order_project="TWTradingStrategy", trace_db_path=db)["env"],
        )

    def test_submit_order_posts_the_intent_and_token(self) -> None:
        seen: list = []

        def opener(request, timeout=None):
            seen.append(request)
            return FakeResponse(json.dumps({"order_id": "A1", "status": "Submitted"}).encode())

        out = tw.submit_order("intent-1", TOKEN, base_url="http://x/api/v1", api_key="k", opener=opener)
        self.assertEqual(out["order_id"], "A1")
        self.assertEqual(seen[0].full_url, "http://x/api/v1/trade/orders")
        self.assertEqual(
            json.loads(seen[0].data), {"intent_id": "intent-1", "confirmation_token": TOKEN}
        )


class OrderConfirmationFlowTests(TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.gateway, self.store, self.scope = _build_schedule_gateway(Path(self._tmp.name))
        self.gateway.send_message = MagicMock(return_value=True)
        self.gateway.send_message_with_keyboard = MagicMock(return_value=777)
        self.gateway.clear_message_keyboard = MagicMock()
        self.gateway.answer_callback_query = MagicMock()
        self.project = self.scope.name

    def _propose(self, intent_id="intent-1", expires=None) -> None:
        self.store.create_order_intent(
            intent_id=intent_id, project=self.project, symbol="2327", action="Buy", price=628.0,
            quantity=1, is_odd_lot=True, price_type="LMT", order_type="ROD",
            estimated_value=628.0, risk={}, confirmation_token=TOKEN,
            expires_at=expires or (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
        )

    def _tap(self, action: str, intent_id="intent-1", chat_id=12345):
        return self.gateway._handle_callback_query(
            {"id": "cb", "data": f"{action}:{intent_id}",
             "message": {"chat": {"id": chat_id}, "message_id": 777}}
        )

    def _turns(self):
        return [t["content"] for t in self.store.get_primary_session_turns(f"psess_{self.project}")]

    def test_a_proposal_is_shown_with_confirm_and_cancel_buttons_and_nothing_is_sent(self) -> None:
        self._propose()
        with patch("memtrace_harness.telegram_gateway.submit_order") as submit:
            self.gateway.process_order_intents()
        submit.assert_not_called()
        _, text, keyboard = self.gateway.send_message_with_keyboard.call_args.args
        self.assertIn("買進 2327", text)
        self.assertIn("還沒有送到券商", text)
        self.assertEqual(
            [b["callback_data"] for b in keyboard[0]], ["order_confirm:intent-1", "order_cancel:intent-1"]
        )
        self.assertEqual(self.store.get_order_intent("intent-1")["status"], "awaiting")
        self.assertNotIn(TOKEN, text)
        # A second tick does not show it again.
        self.gateway.process_order_intents()
        self.assertEqual(self.gateway.send_message_with_keyboard.call_count, 1)

    def test_a_reshown_proposal_clears_the_old_buttons_and_sends_new_ones(self) -> None:
        self._propose()
        self.gateway.process_order_intents()                       # first notification, id 777
        self.gateway.send_message_with_keyboard.return_value = 888
        self.store.reshow_order_intent("intent-1")                 # the model proposed it again
        self.gateway.process_order_intents()
        self.gateway.clear_message_keyboard.assert_called_with(12345, 777)
        self.assertEqual(self.gateway.send_message_with_keyboard.call_count, 2)
        self.assertEqual(self.store.get_order_intent("intent-1")["telegram_message_id"], 888)
        with patch("memtrace_harness.telegram_gateway.load_api_key", return_value="k"), patch(
            "memtrace_harness.telegram_gateway.submit_order", return_value={"order_id": "A1", "status": "S"}
        ) as submit:
            self._tap("order_confirm")
        submit.assert_called_once()

    def test_confirm_sends_the_order_once_reports_it_and_tells_the_chat_model(self) -> None:
        self._propose()
        self.gateway.process_order_intents()
        with patch("memtrace_harness.telegram_gateway.load_api_key", return_value="k"), patch(
            "memtrace_harness.telegram_gateway.submit_order",
            return_value={"order_id": "A1", "status": "PreSubmitted", "filled_qty": 0},
        ) as submit:
            first = self._tap("order_confirm")
            second = self._tap("order_confirm")   # double tap
        submit.assert_called_once()
        self.assertEqual(submit.call_args.args, ("intent-1", TOKEN))
        self.assertIn("已送出委託", first)
        self.assertIn("A1", first)
        self.assertIn("沒有再送出", second)
        row = self.store.get_order_intent("intent-1")
        self.assertEqual(row["status"], "submitted")
        self.assertIsNone(row["confirmation_token"])      # useless afterwards, so dropped
        self.gateway.clear_message_keyboard.assert_called()
        self.assertTrue(any("已送出委託" in t for t in self._turns()))

    def test_cancel_never_sends_anything(self) -> None:
        self._propose()
        self.gateway.process_order_intents()
        with patch("memtrace_harness.telegram_gateway.submit_order") as submit:
            result = self._tap("order_cancel")
            late = self._tap("order_confirm")
        submit.assert_not_called()
        self.assertIn("已取消", result)
        self.assertIn("沒有再送出", late)
        self.assertEqual(self.store.get_order_intent("intent-1")["status"], "cancelled")

    def test_a_failed_submission_is_reported_with_the_reason(self) -> None:
        self._propose()
        self.gateway.process_order_intents()
        with patch("memtrace_harness.telegram_gateway.load_api_key", return_value="k"), patch(
            "memtrace_harness.telegram_gateway.submit_order",
            side_effect=tw.ProxyError("TaiwanTrade returned HTTP 502: Broker submission state is unknown"),
        ):
            result = self._tap("order_confirm")
        self.assertIn("沒有成功送出", result)
        self.assertIn("不要重複下單", result)
        self.assertEqual(self.store.get_order_intent("intent-1")["status"], "failed")

    def test_an_expired_proposal_is_not_sent_even_if_tapped(self) -> None:
        past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        self._propose(expires=past)
        self.store.transition_order_intent("intent-1", ("pending",), "awaiting")
        with patch("memtrace_harness.telegram_gateway.submit_order") as submit:
            result = self._tap("order_confirm")
        submit.assert_not_called()
        self.assertIn("過期", result)

    def test_unconfirmed_proposals_expire_and_the_human_is_told(self) -> None:
        past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        self._propose(expires=past)
        self.store.transition_order_intent("intent-1", ("pending",), "awaiting")
        self.store.set_order_intent_message("intent-1", chat_id=12345, message_id=777)
        self.gateway.process_order_intents()
        self.assertEqual(self.store.get_order_intent("intent-1")["status"], "expired")
        self.assertIn("已過期", self.gateway.send_message.call_args.args[1])
        self.assertTrue(any("已過期" in t for t in self._turns()))

    def test_a_tap_from_a_chat_that_is_not_allowlisted_does_nothing(self) -> None:
        self._propose()
        self.gateway.process_order_intents()
        with patch("memtrace_harness.telegram_gateway.submit_order") as submit:
            self.assertIsNone(self._tap("order_confirm", chat_id=999))
        submit.assert_not_called()
        self.assertEqual(self.store.get_order_intent("intent-1")["status"], "awaiting")

    def test_an_unknown_intent_or_another_projects_intent_is_ignored(self) -> None:
        self.store.create_order_intent(
            intent_id="other", project="SomeoneElse", symbol="2327", action="Buy", price=1.0,
            quantity=1, is_odd_lot=True, price_type="LMT", order_type="ROD", estimated_value=1.0,
            risk={}, confirmation_token=TOKEN, expires_at="2999-01-01T00:00:00+00:00",
        )
        self.store.transition_order_intent("other", ("pending",), "awaiting")
        with patch("memtrace_harness.telegram_gateway.submit_order") as submit:
            self.assertIn("找不到", self._tap("order_confirm", "other"))
            self.assertIn("找不到", self._tap("order_confirm", "missing"))
        submit.assert_not_called()


class ChatNoticeTests(TestCase):
    def test_the_order_paragraph_appears_only_for_an_enabled_project(self) -> None:
        env = {
            "HARNESS_TAIWANTRADE_API_KEY_FILE": "/k",
            "HARNESS_TAIWANTRADE_ORDER_PROJECTS": "TWTradingStrategy",
        }
        with patch.dict("os.environ", env, clear=True):
            enabled = TelegramGateway._taiwantrade_notice("TWTradingStrategy")
            other = TelegramGateway._taiwantrade_notice("Beri")
        self.assertIn("create_order_intent", enabled)
        self.assertIn("不要說已經下單", enabled)
        self.assertNotIn("create_order_intent", other)
        self.assertIn("不能下單", other)


def orders_opener(orders: list, seen: list | None = None):
    def opener(request, timeout=None):
        if seen is not None:
            seen.append(request)
        return FakeResponse(json.dumps(orders).encode())

    return opener


OPEN_ORDER = {
    "order_id": "89dd094d", "symbol": "2327", "action": "Buy", "price": 649.0, "quantity": 1,
    "is_odd_lot": True, "status": "OrderStatus.PendingSubmit",
}


class CancelProposalProxyTests(TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = TraceStore(Path(self._tmp.name) / "t.sqlite3")
        self.ctx = tw.OrderContext(project="TWTradingStrategy", store=self.store)

    def _request(self, order_id="89dd094d", orders=None, seen=None):
        return tw.create_cancel_request(
            {"order_id": order_id}, context=self.ctx, base_url="http://x/api/v1", api_key="k",
            opener=orders_opener(orders if orders is not None else [OPEN_ORDER], seen),
        )

    def test_it_records_a_proposal_and_only_reads_the_order_list(self) -> None:
        seen: list = []
        text = self._request(seen=seen)
        self.assertEqual([(r.method, r.full_url) for r in seen], [("GET", "http://x/api/v1/trade/orders")])
        data = json.loads(text)
        self.assertIn("NOT yet sent", data["note"])
        row = self.store.get_order_intent(data["request_id"])
        self.assertEqual(
            (row["kind"], row["target_order_id"], row["status"], row["symbol"], row["confirmation_token"]),
            ("cancel", "89dd094d", "pending", "2327", None),
        )

    def test_an_unknown_or_finished_order_cannot_be_proposed_for_cancel(self) -> None:
        with self.assertRaises(tw.ProxyError) as caught:
            self._request(order_id="nope")
        self.assertIn("no order", str(caught.exception))
        for status in ("OrderStatus.Filled", "Cancelled", "OrderStatus.Failed"):
            with self.subTest(status=status), self.assertRaises(tw.ProxyError):
                self._request(orders=[{**OPEN_ORDER, "status": status}])

    def test_asking_twice_reshows_the_same_proposal_instead_of_creating_another(self) -> None:
        first = json.loads(self._request())["request_id"]
        self.store.claim_pending_order_intents(["TWTradingStrategy"])    # shown: awaiting
        second = json.loads(self._request())
        self.assertEqual(second["request_id"], first)
        self.assertIn("again", second["note"])
        self.assertEqual(self.store.get_order_intent(first)["status"], "pending")

    def test_a_malformed_order_id_is_rejected(self) -> None:
        for bad in ("../orders", "a b", "x" * 80, ""):
            with self.subTest(bad=bad), self.assertRaises(tw.ProxyError):
                self._request(order_id=bad)

    def test_cancel_order_sends_a_delete_for_exactly_that_order(self) -> None:
        seen: list = []

        def opener(request, timeout=None):
            seen.append(request)
            return FakeResponse(json.dumps({"detail": "Cancel request sent", "order_id": "89dd094d"}).encode())

        out = tw.cancel_order("89dd094d", base_url="http://x/api/v1", api_key="k", opener=opener)
        self.assertEqual(out["detail"], "Cancel request sent")
        self.assertEqual((seen[0].method, seen[0].full_url), ("DELETE", "http://x/api/v1/trade/orders/89dd094d"))
        with self.assertRaises(tw.ProxyError):
            tw.cancel_order("../x", base_url="http://x/api/v1", api_key="k", opener=opener)


class CancelConfirmationFlowTests(OrderConfirmationFlowTests):
    """Same gateway, same safeguards, for a cancel proposal."""

    def _propose_cancel(self, request_id="cancel-1") -> None:
        self.store.create_cancel_request(
            intent_id=request_id, project=self.project, target_order_id="89dd094d", symbol="2327",
            action="Buy", price=649.0, quantity=1, is_odd_lot=True,
            expires_at=(datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
        )

    def test_the_cancel_proposal_is_shown_with_its_own_buttons_and_nothing_is_sent(self) -> None:
        self._propose_cancel()
        with patch("memtrace_harness.telegram_gateway.cancel_order") as cancel:
            self.gateway.process_order_intents()
        cancel.assert_not_called()
        _, text, keyboard = self.gateway.send_message_with_keyboard.call_args.args
        self.assertIn("待確認撤單", text)
        self.assertIn("89dd094d", text)
        self.assertEqual([b["text"] for b in keyboard[0]], ["✅ 確認撤單", "❌ 不撤單"])

    def test_confirm_sends_one_delete_and_reports_it(self) -> None:
        self._propose_cancel()
        self.gateway.process_order_intents()
        with patch("memtrace_harness.telegram_gateway.load_api_key", return_value="k"), patch(
            "memtrace_harness.telegram_gateway.cancel_order", return_value={"detail": "Cancel request sent"}
        ) as cancel:
            first = self._tap("order_confirm", "cancel-1")
            second = self._tap("order_confirm", "cancel-1")
        cancel.assert_called_once()
        self.assertEqual(cancel.call_args.args, ("89dd094d",))
        self.assertIn("已送出撤單要求", first)
        self.assertIn("沒有再送出", second)
        self.assertTrue(any("已送出撤單要求" in t for t in self._turns()))

    def test_declining_sends_nothing(self) -> None:
        self._propose_cancel()
        self.gateway.process_order_intents()
        with patch("memtrace_harness.telegram_gateway.cancel_order") as cancel:
            result = self._tap("order_cancel", "cancel-1")
        cancel.assert_not_called()
        self.assertIn("沒有撤單", result)

    def test_a_cancel_the_broker_refuses_is_reported_with_the_reason(self) -> None:
        self._propose_cancel()
        self.gateway.process_order_intents()
        with patch("memtrace_harness.telegram_gateway.load_api_key", return_value="k"), patch(
            "memtrace_harness.telegram_gateway.cancel_order",
            side_effect=tw.ProxyError("TaiwanTrade returned HTTP 400: order already filled"),
        ):
            result = self._tap("order_confirm", "cancel-1")
        self.assertIn("撤單沒有成功送出", result)
        self.assertIn("already filled", result)
        self.assertEqual(self.store.get_order_intent("cancel-1")["status"], "failed")

    def test_an_expired_cancel_proposal_is_never_sent(self) -> None:
        self.store.create_cancel_request(
            intent_id="cancel-old", project=self.project, target_order_id="89dd094d", symbol="2327",
            action="Buy", price=649.0, quantity=1, is_odd_lot=True,
            expires_at=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
        )
        self.store.transition_order_intent("cancel-old", ("pending",), "awaiting")
        with patch("memtrace_harness.telegram_gateway.cancel_order") as cancel:
            result = self._tap("order_confirm", "cancel-old")
        cancel.assert_not_called()
        self.assertIn("過期", result)

    def test_the_chat_model_is_told_about_the_cancel_tool(self) -> None:
        env = {"HARNESS_TAIWANTRADE_API_KEY_FILE": "/k", "HARNESS_TAIWANTRADE_ORDER_PROJECTS": "P"}
        with patch.dict("os.environ", env, clear=True):
            notice = TelegramGateway._taiwantrade_notice("P")
        self.assertIn("request_order_cancel", notice)
        self.assertIn("get_positions 為準", notice)
