from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone, timedelta
import json
from pathlib import Path
from unittest import TestCase
from unittest.mock import MagicMock, patch

from memtrace_harness.improvement import (
    ImprovementDuty, ImprovementStore, export_observations, grounded_findings, settings, update_settings,
)
from memtrace_harness.runtime import build_harness
from memtrace_harness.scope import ProjectScope
from tests.workspace_support import workspace


class ImprovementTests(TestCase):
    def harness(self):
        ctx = workspace()
        root, config = ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)
        scope = replace(ProjectScope.from_file(root / "harness-scope.md"), github_repo="owner/platform", verify_command="python3 -m unittest")
        parts = build_harness(config, scope)
        return root, parts, ImprovementStore(parts.trace_store)

    def enable(self, parts, **extra):
        update_settings(parts.config, dict(enabled=True, auto_develop=True, sources=["Beri", "TestProj"], **extra))

    def event(self, eid="turn:1", text="使用者要求重跑，助手只是說會執行，但沒有啟動任務。"):
        return dict(id=eid, source="Beri", kind="conversation", at=datetime.now(timezone.utc).isoformat(), text=text)

    def ingest(self, parts, store, **extra):
        return store.ingest(parts.config, dict(source="Beri", cursor=1, events=[self.event()], **extra))

    def finding(self, action="develop", target="platform"):
        event = self.event()
        return dict(key="task-never-started", title="任務未真正啟動", summary="確認啟動流程並補上回歸測試。", action=action, target=target,
                    evidence=[dict(id="Beri/turn:1", quote=event["text"])])

    def test_sharing_and_autonomy_are_opt_in_and_bad_settings_fail_closed(self):
        root, parts, _ = self.harness()
        self.assertFalse(settings(parts.config)["enabled"])
        self.assertEqual(export_observations(parts.trace_store, parts.config, parts.scope)["events"], [])
        (root / ".harness/models.toml").write_text("[improvement]\nenabled = true\ninterval_seconds = 1\n")
        self.assertFalse(settings(parts.config)["enabled"])
        for body in (dict(enabled="true"), dict(sources="Beri"), dict(interval_seconds=1)):
            with self.assertRaises(ValueError):
                update_settings(parts.config, body)

    def test_export_is_incremental_bounded_and_excludes_observer_notices(self):
        _, parts, _ = self.harness()
        update_settings(parts.config, dict(share_enabled=True))
        for i in range(85):
            parts.primary_session_mgr.record_turn(project=parts.scope.name, speaker="user", turn_type="chat", content=f"message {i}")
        parts.primary_session_mgr.record_turn(project=parts.scope.name, speaker="assistant", turn_type="improvement_report", content="observation notice")
        initial = export_observations(parts.trace_store, parts.config, parts.scope)
        self.assertEqual(len(initial["events"]), 80)
        self.assertTrue(initial["bootstrap_limited"])
        self.assertNotIn("observation notice", json.dumps(initial))
        self.assertEqual(export_observations(parts.trace_store, parts.config, parts.scope, initial["cursor"])["events"], [])
        parts.primary_session_mgr.record_turn(project=parts.scope.name, speaker="user", turn_type="chat", content="latest")
        following = export_observations(parts.trace_store, parts.config, parts.scope, initial["cursor"])
        self.assertEqual(len(following["events"]), 1)
        self.assertIn("latest", following["events"][0]["text"])
        update_settings(parts.config, dict(share_enabled=False))
        self.assertFalse(export_observations(parts.trace_store, parts.config, parts.scope)["sharing"])

    def test_evidence_is_authorized_deduplicated_and_redacted(self):
        _, parts, store = self.harness()
        with self.assertRaises(ValueError):
            self.ingest(parts, store)
        self.enable(parts)
        self.assertEqual(self.ingest(parts, store), 1)
        self.assertEqual(self.ingest(parts, store), 0)
        with self.assertRaises(ValueError):
            store.ingest(parts.config, dict(source="Other", events=[]))
        store.ingest(parts.config, dict(source="Beri", cursor=2, events=[self.event("turn:2", "token=supersecret ghp_abcdefghijk")]))
        evidence = store.claim(parts.config, 100)
        self.assertEqual(len(evidence), 2)
        self.assertNotIn("supersecret", json.dumps(evidence))
        self.assertNotIn("ghp_abcdefghijk", json.dumps(evidence))
        self.assertFalse(store.claim(parts.config, 10000))  # same live owner, no concurrent analysis
        store.finish(evidence, [])

    def test_historical_or_undated_evidence_cannot_trigger_a_new_fix(self):
        _, parts, store = self.harness()
        self.enable(parts)
        old = self.event() | {"at": (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()}
        self.assertEqual(store.ingest(parts.config, dict(source="Beri", cursor=1, events=[old])), 0)
        self.assertEqual(store.ingest(parts.config, dict(source="Beri", cursor=2, events=[self.event("turn:2") | {"at": None}])), 0)
        self.assertEqual(store.public(parts.config)["pending_evidence"], 0)
        finding = self.finding()
        evidence = [old | {"id": "Beri/turn:1"}]
        store.finish([], grounded_findings(json.dumps(dict(findings=[finding])), evidence))
        self.assertIsNone(store.queue_one(parts.config, parts.scope))
        self.assertEqual(store.public(parts.config)["findings"][0]["state"], "historical")

    def test_findings_need_real_quotes_and_cannot_smuggle_extra_fields(self):
        evidence = [self.event() | {"id": "Beri/turn:1"}]
        finding = self.finding() | {"command": "do something", "approve": True}
        checked = grounded_findings(json.dumps(dict(findings=[finding])), evidence)
        self.assertNotIn("command", checked[0])
        self.assertNotIn("approve", checked[0])
        finding["evidence"][0]["quote"] = "這是不存在的證據文字，不能自動修改。"
        with self.assertRaises(ValueError):
            grounded_findings(json.dumps(dict(findings=[finding])), evidence)

    def test_a_busy_source_does_not_starve_other_harnesses(self):
        _, parts, store = self.harness()
        self.enable(parts)
        store.ingest(parts.config, dict(source="Beri", cursor=70, events=[self.event(f"turn:{i}") for i in range(70)]))
        store.ingest(parts.config, dict(source="TestProj", cursor=1, events=[self.event()]))
        evidence = store.claim(parts.config, 100)
        self.assertEqual(len(evidence), 60)
        self.assertIn("TestProj", {e["source"] for e in evidence})
        store.finish(evidence, [])

    def test_one_platform_fix_is_queued_atomically_and_project_changes_stay_suggestions(self):
        _, parts, store = self.harness()
        self.enable(parts)
        self.ingest(parts, store)
        evidence = store.claim(parts.config, 100)
        platform = grounded_findings(json.dumps(dict(findings=[self.finding()])), evidence)[0]
        project = platform | dict(key="business-schedule", title="其他專案排程建議", target="project")
        store.finish(evidence, [project, platform])
        queued = store.queue_one(parts.config, parts.scope)
        self.assertEqual(queued["key"], platform["key"])
        self.assertIsNone(store.queue_one(parts.config, parts.scope))
        queue = parts.trace_store.list_queued_tasks(parts.scope.workspace_id)
        self.assertEqual(len(queue), 1)
        self.assertEqual(queue[0]["kind"], "improvement")
        self.assertEqual(queue[0]["conversation_id"], queued["id"])
        self.assertIn("範圍只限目前平台 repo", queue[0]["payload"]["goal"])
        self.assertIn("不得改其他專案", queue[0]["payload"]["goal"])
        self.assertTrue(store.can_start(parts.config, queued["id"]))
        update_settings(parts.config, dict(sources=[]))
        self.assertFalse(store.can_start(parts.config, queued["id"]))

    def test_no_new_content_means_no_model_or_notifications(self):
        _, parts, _ = self.harness()
        self.enable(parts)
        call = MagicMock()
        parts.service.notify_operator = MagicMock()
        duty = ImprovementDuty(parts.service, parts.scope, call_model=call)
        duty.run_once(100)
        call.assert_not_called()
        parts.service.notify_operator.assert_not_called()

    def test_observation_cli_calls_are_isolated_and_keep_every_attempt_locally(self):
        from memtrace_harness.cli_process import ProcessResult

        root, parts, _ = self.harness()
        chain = (("claude", "haiku"), ("codex", "model-a"), ("antigravity", "model-b"), ("cursor", "model-c"))
        seen = []

        def run(command, *, cwd, timeout_seconds):
            self.assertNotIn(root, cwd.parents)
            self.assertEqual(timeout_seconds, 180)
            provider = chain[len(seen)][0]
            if provider == "claude":
                self.assertEqual(command[command.index("--tools") + 1], "")
                self.assertIn("--strict-mcp-config", command)
            elif provider == "codex":
                self.assertIn("read-only", command)
                self.assertIn("--ignore-user-config", command)
            elif provider == "antigravity":
                self.assertEqual(command[command.index("--mode") + 1], "plan")
                self.assertIn("--sandbox", command)
                self.assertNotIn("accept-edits", command)
            else:
                self.assertIn("ask", command)
                self.assertIn("enabled", command)
            seen.append(provider)
            return ProcessResult(command=command, return_code=0 if provider == "cursor" else 1, stdout='{"findings":[]}', stderr="", started_at="s", completed_at="e", duration_ms=1)

        with patch.object(parts.service.primary_agent, "candidates", return_value=chain), patch("memtrace_harness.cli_process.CliProcessRunner.run", side_effect=run):
            result = ImprovementDuty(parts.service, parts.scope)._call_model("只判斷引用資料")
        self.assertEqual(result, '{"findings":[]}')
        self.assertEqual(seen, [p for p, _ in chain])
        with parts.trace_store._connection() as conn:
            attempts = conn.execute("SELECT context_policy,raw_trace_ref,usage_json FROM cli_executions").fetchall()
            tasks = conn.execute("SELECT task_json FROM runs").fetchall()
        self.assertEqual(len(attempts), 4)
        self.assertTrue(all(row[0] == "observation-evidence" and Path(row[1]).is_file() and json.loads(row[2])["completeness"] == "unavailable" for row in attempts))
        self.assertTrue(all(json.loads(row[0])["source"] == "observation" for row in tasks))

    def test_empty_review_consumes_evidence_without_noise_and_failures_retain_it(self):
        _, parts, store = self.harness()
        self.enable(parts)
        self.ingest(parts, store)
        parts.service.notify_operator = MagicMock()
        duty = ImprovementDuty(parts.service, parts.scope, call_model=MagicMock(return_value='{"findings":[]}'))
        duty.run_once(100)
        self.assertEqual(store.public(parts.config)["pending_evidence"], 0)
        parts.service.notify_operator.assert_not_called()
        store.ingest(parts.config, dict(source="Beri", cursor=2, events=[self.event("turn:2")]))
        duty.call_model = MagicMock(return_value='invalid output')
        duty.run_once(2000)
        self.assertEqual(store.public(parts.config)["pending_evidence"], 1)
        self.assertEqual(store.public(parts.config)["last_error"], "analysis_failed")

    def test_disabling_during_analysis_does_not_dispatch_or_consume_evidence(self):
        _, parts, store = self.harness()
        self.enable(parts)
        self.ingest(parts, store)

        def call(_prompt):
            update_settings(parts.config, dict(enabled=False))
            return json.dumps(dict(findings=[self.finding()]))

        duty = ImprovementDuty(parts.service, parts.scope, call_model=call)
        duty.run_once(100)
        self.assertEqual(store.public(parts.config)["findings"], [])
        self.assertEqual(store.public(parts.config)["pending_evidence"], 1)
        self.assertEqual(parts.trace_store.list_queued_tasks(parts.scope.workspace_id), [])

    def test_duplicate_findings_do_not_notify_or_queue_twice(self):
        _, parts, store = self.harness()
        self.enable(parts)
        self.ingest(parts, store)
        parts.service.notify_operator = MagicMock()
        parts.service._auto_delivery_enabled = lambda: True
        call = MagicMock(return_value=json.dumps(dict(findings=[self.finding()])))
        duty = ImprovementDuty(parts.service, parts.scope, call_model=call)
        duty.run_once(100)
        self.assertEqual(len(parts.trace_store.list_queued_tasks(parts.scope.workspace_id)), 1)
        notices = parts.service.notify_operator.call_count
        store.ingest(parts.config, dict(source="Beri", cursor=2, events=[self.event("turn:2")]))
        repeated = self.finding()
        repeated["evidence"][0]["id"] = "Beri/turn:2"
        call.return_value = json.dumps(dict(findings=[repeated]))
        duty.run_once(2000)
        self.assertEqual(parts.service.notify_operator.call_count, notices)
        self.assertEqual(len(store.public(parts.config)["findings"]), 1)

    def test_pausing_after_queue_does_not_start_a_task(self):
        _, parts, store = self.harness()
        self.enable(parts)
        self.ingest(parts, store)
        evidence = store.claim(parts.config, 100)
        store.finish(evidence, grounded_findings(json.dumps(dict(findings=[self.finding()])), evidence))
        finding = store.queue_one(parts.config, parts.scope)
        item = parts.trace_store.peek_next_queued_task(parts.scope.workspace_id)
        update_settings(parts.config, dict(auto_develop=False))
        parts.service._launch_task = MagicMock()
        parts.service._start_queued_item(parts.scope, item, finding["id"])
        parts.service._launch_task.assert_not_called()
        self.assertEqual(store.public(parts.config)["findings"][0]["state"], "paused")

    def test_task_results_follow_durable_runtime_states_and_never_create_adoption(self):
        _, parts, store = self.harness()
        self.enable(parts)
        self.ingest(parts, store)
        evidence = store.claim(parts.config, 100)
        store.finish(evidence, grounded_findings(json.dumps(dict(findings=[self.finding()])), evidence))
        finding = store.queue_one(parts.config, parts.scope)
        parts.service._task_finished(finding["id"], finding["id"], "delivering", "local checks passed")
        self.assertEqual(store.public(parts.config)["findings"][0]["state"], "delivering")
        parts.service._task_finished(finding["id"], finding["id"], "succeeded", "remote merged")
        self.assertEqual(store.public(parts.config)["findings"][0]["state"], "succeeded")

    def test_a_later_recurrence_is_a_new_task_without_reopening_the_finished_one(self):
        _, parts, store = self.harness()
        self.enable(parts)
        self.ingest(parts, store)
        evidence = store.claim(parts.config, 100)
        finding = grounded_findings(json.dumps(dict(findings=[self.finding()])), evidence)[0]
        store.finish(evidence, [finding])
        first = store.queue_one(parts.config, parts.scope)
        parts.trace_store.claim_queued_task(parts.trace_store.peek_next_queued_task(parts.scope.workspace_id)["id"])
        store.task_state(first["id"], "succeeded")
        self.assertEqual(store.finish([], [finding]), [])
        newer = {**finding, "evidence": [{**finding["evidence"][0], "id": "Beri/turn:9", "at": (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()}]}
        fresh = store.finish([], [newer])
        self.assertEqual(len(fresh), 1)
        self.assertNotEqual(fresh[0]["id"], first["id"])
        self.assertEqual(fresh[0]["previous_finding"], first["id"])
        self.assertEqual(next(f for f in store.public(parts.config)["findings"] if f["id"] == first["id"])["state"], "succeeded")

    def test_automatic_dispatch_is_limited_to_three_cases_in_a_rolling_day(self):
        _, parts, store = self.harness()
        self.enable(parts)
        self.ingest(parts, store)
        evidence = store.claim(parts.config, 100)
        finding = grounded_findings(json.dumps(dict(findings=[self.finding()])), evidence)[0]
        for i in range(4):
            store.finish([], [{**finding, "key": f"different-issue-{i}"}])
            queued = store.queue_one(parts.config, parts.scope)
            if i == 3:
                self.assertIsNone(queued)
                break
            self.assertIsNotNone(queued)
            item = parts.trace_store.peek_next_queued_task(parts.scope.workspace_id)
            parts.trace_store.claim_queued_task(item["id"])
            store.task_state(queued["id"], "succeeded")

    def test_disabled_delivery_or_missing_verification_cannot_queue_autonomous_edits(self):
        _, parts, store = self.harness()
        self.enable(parts)
        self.ingest(parts, store)
        evidence = store.claim(parts.config, 100)
        store.finish(evidence, grounded_findings(json.dumps(dict(findings=[self.finding()])), evidence))
        duty = ImprovementDuty(parts.service, parts.scope)
        parts.service._auto_delivery_enabled = lambda: False
        duty._queue()
        self.assertEqual(parts.trace_store.list_queued_tasks(parts.scope.workspace_id), [])
        parts.service._auto_delivery_enabled = lambda: True
        duty.scope = replace(parts.scope, verify_command=None)
        duty._queue()
        self.assertEqual(parts.trace_store.list_queued_tasks(parts.scope.workspace_id), [])
