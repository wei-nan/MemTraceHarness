from __future__ import annotations

import tempfile
from pathlib import Path
from unittest import TestCase

from memtrace_harness.approval import ApprovalManager
from memtrace_harness.trace_store import TraceStore


class ApprovalTests(TestCase):
    def test_approval_request_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            db_path = tmp_path / "test_approval.sqlite3"
            trace_store = TraceStore(db_path)
            allowed_chat_ids = {12345}
            mgr = ApprovalManager(trace_store, allowed_chat_ids)

            # 1. Create approval request
            req = mgr.request_approval(
                conversation_id="conv_001",
                workspace="ws_test",
                working_directory=str(tmp_path),
                reason="git_push",
                proposed_action="git push origin main",
            )
            self.assertTrue(req.id.startswith("appr_"))
            self.assertEqual(req.status, "pending")

            # 2. Reject response from non-allowed chat ID
            ok, msg, req_updated = mgr.respond(req.id, "approve", chat_id=99999)
            self.assertFalse(ok)
            self.assertIn("未經授權", msg)

            # 3. Approve response from allowed chat ID
            ok, msg, req_updated = mgr.respond(req.id, "approve", chat_id=12345)
            self.assertTrue(ok)
            self.assertIn("approved", msg)
            self.assertIsNotNone(req_updated)
            self.assertEqual(req_updated.status, "approved")

            # 4. Single terminal response constraint (cannot double-spend or re-respond)
            ok_second, msg_second, _ = mgr.respond(req.id, "reject", chat_id=12345)
            self.assertFalse(ok_second)
            self.assertIn("已經是", msg_second)
