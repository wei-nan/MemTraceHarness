import io
import json
import urllib.error
import unittest

from memtrace_harness import taiwantrade_mcp as tw


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def make_opener(body=b'{"ok":true}', seen=None):
    def opener(request, timeout=None):
        if seen is not None:
            seen.append(request)
        return FakeResponse(body)

    return opener


def rpc(method, params=None, *, opener=None, key="k"):
    message = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
    return tw.handle_message(
        message,
        base_url="http://127.0.0.1:8000/api/v1",
        api_key_loader=lambda: key,
        opener=opener or make_opener(),
    )


class TaiwanTradeProxyTests(unittest.TestCase):
    def test_no_write_endpoints_are_exposed(self):
        for tool in tw.TOOLS:
            for banned in ("order-intents", "/auth", "keys"):
                self.assertNotIn(banned, tool.path)
            self.assertFalse(tool.path.endswith("/orders") and tool.name != "list_orders")

    def test_only_get_requests_with_key_header(self):
        seen = []
        reply = rpc("tools/call", {"name": "get_balance", "arguments": {}}, opener=make_opener(seen=seen))
        self.assertFalse(reply["result"].get("isError"))
        self.assertEqual(seen[0].get_method(), "GET")
        self.assertEqual(seen[0].full_url, "http://127.0.0.1:8000/api/v1/trade/balance")
        self.assertEqual(seen[0].get_header("X-api-key"), "k")

    def test_path_and_query_arguments(self):
        seen = []
        rpc(
            "tools/call",
            {"name": "get_stock_daily", "arguments": {"ticker": "2330", "start_date": "2026-01-01", "end_date": "2026-01-31"}},
            opener=make_opener(seen=seen),
        )
        self.assertEqual(
            seen[0].full_url,
            "http://127.0.0.1:8000/api/v1/data/stocks/2330/daily?start_date=2026-01-01&end_date=2026-01-31",
        )

    def test_path_traversal_is_rejected(self):
        seen = []
        reply = rpc(
            "tools/call",
            {"name": "get_stock_daily", "arguments": {"ticker": "../trade/orders", "start_date": "2026-01-01", "end_date": "2026-01-02"}},
            opener=make_opener(seen=seen),
        )
        self.assertTrue(reply["result"]["isError"])
        self.assertEqual(seen, [])

    def test_unknown_tool_and_extra_arguments_rejected(self):
        self.assertTrue(rpc("tools/call", {"name": "place_order", "arguments": {}})["result"]["isError"])
        reply = rpc("tools/call", {"name": "get_balance", "arguments": {"api_key": "x"}})
        self.assertTrue(reply["result"]["isError"])

    def test_missing_key_fails_closed(self):
        reply = rpc("tools/call", {"name": "get_balance", "arguments": {}}, key="")
        self.assertTrue(reply["result"]["isError"])

    def test_http_error_does_not_leak_key(self):
        def opener(request, timeout=None):
            raise urllib.error.HTTPError(request.full_url, 403, "no", {}, io.BytesIO(b"forbidden"))

        reply = rpc("tools/call", {"name": "get_balance", "arguments": {}}, opener=opener, key="SECRET")
        text = reply["result"]["content"][0]["text"]
        self.assertIn("403", text)
        self.assertNotIn("SECRET", text)

    def test_tools_list_and_notification(self):
        names = [t["name"] for t in rpc("tools/list")["result"]["tools"]]
        self.assertIn("get_quotes", names)
        self.assertIsNone(
            tw.handle_message({"jsonrpc": "2.0", "method": "notifications/initialized"}, base_url="", api_key_loader=lambda: "")
        )

    def test_spec_requires_opt_in_and_hides_key(self):
        self.assertIsNone(tw.mcp_server_spec({}))
        spec = tw.mcp_server_spec({"HARNESS_TAIWANTRADE_API_KEY_FILE": "/k", "HARNESS_TAIWANTRADE_API_KEY": "SECRET"})
        self.assertNotIn("SECRET", json.dumps(spec))
        self.assertEqual(spec["env"]["HARNESS_TAIWANTRADE_API_KEY_FILE"], "/k")


if __name__ == "__main__":
    unittest.main()
