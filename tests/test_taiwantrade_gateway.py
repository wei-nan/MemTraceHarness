import io
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from memtrace_harness import taiwantrade_gateway as gw


class FakeResponse(io.BytesIO):
    status = 200
    headers = {"Content-Type": "image/png"}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class GatewayTest(unittest.TestCase):
    def setUp(self):
        self.seen = []

        def opener(request, timeout=None):
            self.seen.append(request)
            return FakeResponse(b"PNGDATA")

        env = {"HARNESS_TAIWANTRADE_API_KEY": "secret-key", "HARNESS_TAIWANTRADE_API_URL": "http://upstream:9/api/v1"}
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), gw.build_handler(env, opener))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def get(self, path, method="GET"):
        request = urllib.request.Request(self.base + path, method=method, data=b"x" if method != "GET" else None)
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def test_chart_is_forwarded_with_the_key_added(self):
        status, body = self.get("/api/v1/charts/taiex")
        self.assertEqual((status, body), (200, b"PNGDATA"))
        self.assertEqual(self.seen[0].full_url, "http://upstream:9/api/v1/charts/taiex")
        self.assertEqual(self.seen[0].get_header("X-api-key"), "secret-key")

    def test_read_tool_paths_with_query_are_forwarded(self):
        status, _ = self.get("/api/v1/data/stocks/2330/daily?start_date=2026-10-01&end_date=2026-10-14")
        self.assertEqual(status, 200)
        self.assertIn("start_date=2026-10-01", self.seen[0].full_url)

    def test_orders_writes_and_unknown_paths_are_refused(self):
        self.assertEqual(self.get("/api/v1/trade/orders/place")[0], 403)
        self.assertEqual(self.get("/api/v1/auth/login")[0], 403)
        self.assertEqual(self.get("/charts/taiex")[0], 403)
        self.assertEqual(self.get("/api/v1/trade/orders", method="POST")[0], 405)
        self.assertEqual(self.seen, [])

    def test_upstream_401_is_passed_through(self):
        def opener(request, timeout=None):
            raise urllib.error.HTTPError(request.full_url, 401, "no", {"Content-Type": "text/plain"}, io.BytesIO(b"no"))

        server = ThreadingHTTPServer(("127.0.0.1", 0), gw.build_handler({"HARNESS_TAIWANTRADE_API_KEY": "k"}, opener))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(f"http://127.0.0.1:{server.server_address[1]}/api/v1/charts/taiex")
            self.assertEqual(caught.exception.code, 401)
        finally:
            server.shutdown()
            server.server_close()

    def test_photo_is_queued_only_for_workspace_images(self):
        import json
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "ws"
            workspace.mkdir()
            (workspace / "chart.png").write_bytes(b"x")
            (workspace / "notes.txt").write_text("x")
            outside = Path(tmp) / "other.png"
            outside.write_bytes(b"x")
            queued = []

            def enqueue(kind, **fields):
                queued.append((kind, fields))
                return 7

            server = ThreadingHTTPServer(
                ("127.0.0.1", 0), gw.build_handler({}, notify_photo=gw.photo_notifier(workspace, enqueue))
            )
            threading.Thread(target=server.serve_forever, daemon=True).start()
            base = f"http://127.0.0.1:{server.server_address[1]}"

            def post(body):
                request = urllib.request.Request(base + "/notify/photo", data=json.dumps(body).encode(), method="POST")
                try:
                    with urllib.request.urlopen(request) as response:
                        return response.status, response.read()
                except urllib.error.HTTPError as exc:
                    return exc.code, exc.read()

            try:
                status, body = post({"path": str(workspace / "chart.png"), "caption": " hi "})
                self.assertEqual((status, json.loads(body)), (202, {"queued": 7}))
                self.assertEqual(queued, [("photo", {"text": "hi", "ref": str((workspace / "chart.png").resolve())})])
                for bad in (str(workspace / "notes.txt"), str(outside), "", str(workspace / "missing.png")):
                    self.assertEqual(post({"path": bad})[0], 400)
                self.assertEqual(len(queued), 1)
            finally:
                server.shutdown()
                server.server_close()

    def test_post_elsewhere_is_still_refused(self):
        self.assertEqual(self.get("/notify/photo", method="POST")[0], 405)

    def test_disabled_without_port_and_key_file(self):
        self.assertIsNone(gw.start_gateway({}))
        self.assertIsNone(gw.start_gateway({"HARNESS_TAIWANTRADE_GATEWAY_PORT": "18999"}))


if __name__ == "__main__":
    unittest.main()
