from __future__ import annotations

import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import MagicMock

from memtrace_harness.primary_session import PrimarySessionManager
from memtrace_harness.trace_store import TraceStore


class PrimarySessionTests(TestCase):
    def test_primary_session_logging_and_consolidation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            db_path = tmp_path / "test_psess.sqlite3"
            trace_store = TraceStore(db_path)
            mock_client = MagicMock()
            mock_client.create_node.return_value = "mem_draft_123"

            psess_mgr = PrimarySessionManager(trace_store, mock_client)

            # 1. Record turns
            psess_mgr.record_turn(
                project="proj_a", speaker="user", turn_type="chat", content="Hello harness"
            )
            psess_mgr.record_turn(
                project="proj_a", speaker="chat_model", turn_type="decision", content="Agreed on architecture spec for design"
            )
            psess_mgr.record_turn(
                project="proj_a", speaker="work_session_report", turn_type="dev_report", content="Loop finished with PASS result"
            )

            # 2. Check rehydration context
            rehydrated = psess_mgr.get_rehydration_context("proj_a")
            self.assertIn("Agreed on architecture spec", rehydrated)
            self.assertIn("Loop finished with PASS", rehydrated)

            # 3. Consolidate back to MemTrace
            consolidated_ids = psess_mgr.consolidate_to_memtrace("proj_a", "ws_test")
            self.assertEqual(len(consolidated_ids), 2)  # decision and dev_report are substantive
            mock_client.create_node.assert_called_once()
