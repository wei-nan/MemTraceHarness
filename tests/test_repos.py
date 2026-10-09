"""One Harness, several repos: a primary one and others it also works in and helps maintain."""

import argparse
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import MagicMock, patch

from memtrace_harness.adapters.claude import ClaudeCliAdapter
from memtrace_harness.cli import main
from memtrace_harness.scanner import UnattendedScanner
from memtrace_harness.scope import (
    ProjectScope,
    RepoScope,
    dump_repos,
    load_repos,
    split_repo_goal,
)
from memtrace_harness.trace_store import TraceStore
from tests.test_service import _build_schedule_gateway


def two_repo_scope(root: Path) -> ProjectScope:
    root = root.resolve()
    backend, android = root / "beri", root / "beri-android"
    backend.mkdir(exist_ok=True)
    android.mkdir(exist_ok=True)
    return ProjectScope(
        name="Beri",
        workspace_id="ws_beri",
        working_directory=backend,
        scope_file_path=backend / "harness-scope.md",
        raw_markdown="- workspace_id: ws_beri",
        verify_command="pytest -q",
        off_limits=["production database"],
        github_repo="acme/beri",
        repos={
            "android": RepoScope(
                name="android", path=android, verify_command="./gradlew test", verify_timeout_seconds=1800,
                github_repo="acme/beri-android", off_limits=["google-services.json"],
            )
        },
    )


class ScopeTests(TestCase):
    def test_a_goal_can_name_its_repo(self) -> None:
        self.assertEqual(split_repo_goal("repo=android::加上登入頁"), ("android", "加上登入頁"))
        self.assertEqual(split_repo_goal("repo=android::第一行\n第二行"), ("android", "第一行\n第二行"))
        self.assertEqual(split_repo_goal("加上登入頁"), (None, "加上登入頁"))
        self.assertEqual(split_repo_goal("repo=bad name::x"), (None, "repo=bad name::x"))

    def test_the_project_name_and_primary_mean_the_primary_repo_and_unknown_names_are_refused(self) -> None:
        with TemporaryDirectory() as tmp:
            scope = two_repo_scope(Path(tmp))
            self.assertEqual([scope.resolve_repo(n) for n in (None, "Beri", "primary", "BERI")], [None] * 4)
            self.assertEqual(scope.resolve_repo("Android"), "android")
            with self.assertRaises(ValueError) as caught:
                scope.resolve_repo("ios")
            self.assertIn("Beri, android", str(caught.exception))

    def test_a_repo_view_has_that_repos_directory_verify_and_github_and_the_union_of_off_limits(self) -> None:
        with TemporaryDirectory() as tmp:
            scope = two_repo_scope(Path(tmp))
            view = scope.for_repo("android")
            self.assertEqual(view.working_directory, scope.repos["android"].path)
            self.assertEqual((view.verify_command, view.verify_timeout_seconds), ("./gradlew test", 1800))
            self.assertEqual(view.github_repo, "acme/beri-android")
            self.assertEqual(view.off_limits, ["production database", "google-services.json"])
            # what is the project's stays the project's
            self.assertEqual((view.name, view.workspace_id, view.max_workers), ("Beri", "ws_beri", scope.max_workers))
            # asking a view for another repo, or for the primary, starts from the project again
            self.assertIs(view.for_repo(None), scope)
            self.assertEqual(view.for_repo("android").working_directory, view.working_directory)
            # the primary's own view is untouched
            self.assertEqual((scope.working_directory.name, scope.verify_command), ("beri", "pytest -q"))

    def test_two_repos_can_be_edited_side_by_side_but_one_repo_by_one_developer(self) -> None:
        with TemporaryDirectory() as tmp:
            scope = two_repo_scope(Path(tmp))
            self.assertEqual(scope.lock_key, "ws_beri")
            self.assertEqual(scope.for_repo("android").lock_key, "ws_beri#android")

    def test_repos_file_round_trips_and_rejects_bad_entries(self) -> None:
        with TemporaryDirectory() as tmp:
            scope = two_repo_scope(Path(tmp))
            path = Path(tmp) / "repos.toml"
            path.write_text(dump_repos(scope.repos), encoding="utf-8")
            loaded = load_repos(path)
            self.assertEqual(loaded["android"], scope.repos["android"])
            self.assertEqual(load_repos(Path(tmp) / "missing.toml"), {})
            path.write_text("[repos.bad]\nverify_command = 'x'\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                load_repos(path)
            path.write_text("[repos.'no good']\npath = '/x'\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                load_repos(path)


class StoreTests(TestCase):
    def test_the_repo_of_a_conversation_is_remembered_and_the_primary_needs_no_row(self) -> None:
        with TemporaryDirectory() as tmp:
            store = TraceStore(Path(tmp) / "t.sqlite3")
            store.set_conversation_repo("c1", "android")
            store.set_conversation_repo("c2", None)
            self.assertEqual((store.get_conversation_repo("c1"), store.get_conversation_repo("c2")), ("android", None))
            store.set_conversation_repo("c1", "ios")
            self.assertEqual(store.get_conversation_repo("c1"), "ios")


class RunInTheRightRepoTests(TestCase):
    def _gateway(self, tmp: str):
        gateway, store, scope = _build_schedule_gateway(Path(tmp))
        scope = two_repo_scope(Path(tmp))
        scope.max_workers = 1  # edit in place: no git needed for this test
        return gateway, store, scope

    def _run(self, gateway, scope, conv_id, goal):
        summary = MagicMock(conversation_id=conv_id, status="succeeded", recommendation="done", stages=[])
        with patch("memtrace_harness.service.AgentLoopRunner") as runner, patch(
            "memtrace_harness.service.build_role_adapter_candidates", return_value={}
        ) as adapters, patch("memtrace_harness.service.load_role_profiles", return_value={}):
            runner.return_value.run.return_value = summary
            gateway._run_new_task(scope, conv_id, goal)
        return runner.call_args.kwargs, runner.return_value.run.call_args, adapters.call_args.kwargs

    def test_a_task_naming_a_repo_runs_in_that_repo_with_its_verify_command_and_limits(self) -> None:
        with TemporaryDirectory() as tmp:
            gateway, store, scope = self._gateway(tmp)
            kwargs, run, adapter_kwargs = self._run(gateway, scope, "chat_a", "repo=android::加登入頁")
            android = scope.repos["android"].path
            self.assertEqual(adapter_kwargs["working_directory"], android)
            self.assertEqual((kwargs["verify_command"], kwargs["verify_timeout_seconds"]), ("./gradlew test", 1800))
            task = run.args[0]
            self.assertEqual(task.goal, "加登入頁")  # the prefix is routing, not part of the request
            self.assertEqual(task.constraints, ["production database", "google-services.json"])
            self.assertEqual(store.get_conversation_repo("chat_a"), "android")
            # the other repo may be read by a read-only role, never written
            self.assertEqual(adapter_kwargs["read_directories"], (scope.working_directory,))
            repos_item = next(i for i in task.context_items if i.ref.startswith("harness:repos:"))
            self.assertIn("`android`", repos_item.body)
            self.assertIn(str(scope.working_directory), repos_item.body)

    def test_a_task_without_a_repo_runs_in_the_primary_one(self) -> None:
        with TemporaryDirectory() as tmp:
            gateway, store, scope = self._gateway(tmp)
            kwargs, run, adapter_kwargs = self._run(gateway, scope, "chat_b", "修後端")
            self.assertEqual(adapter_kwargs["working_directory"], scope.working_directory)
            self.assertEqual(kwargs["verify_command"], "pytest -q")
            self.assertIsNone(store.get_conversation_repo("chat_b"))
            self.assertEqual(adapter_kwargs["read_directories"], (scope.repos["android"].path,))

    def test_an_unknown_repo_is_refused_before_a_slot_is_taken(self) -> None:
        with TemporaryDirectory() as tmp:
            gateway, store, scope = self._gateway(tmp)
            with patch.object(gateway, "_launch_task") as launch:
                result = gateway._start_or_queue_task(scope, "repo=ios::x", "tg:1")
            launch.assert_not_called()
            self.assertIn("no repo named 'ios'", result)
            self.assertEqual(store.list_workspace_locks("ws_beri"), [])

    def test_a_resumed_conversation_goes_back_to_its_repo(self) -> None:
        with TemporaryDirectory() as tmp:
            gateway, store, scope = self._gateway(tmp)
            gateway.projects = [scope]
            req = gateway.approval_manager.request_approval(
                conversation_id="chat_r", workspace="ws_beri", working_directory=str(scope.working_directory),
                reason="ambiguous_requirement", proposed_action="？", resume_goal="加登入頁",
            )
            store.set_conversation_repo("chat_r", "android")
            summary = MagicMock(conversation_id="chat_r", status="succeeded", recommendation="ok", stages=[])
            done = []
            with patch("memtrace_harness.service.AgentLoopRunner") as runner, patch(
                "memtrace_harness.service.build_role_adapter_candidates", return_value={}
            ) as adapters, patch("memtrace_harness.service.load_role_profiles", return_value={}), patch.object(
                gateway, "_report_run_outcome", side_effect=lambda **kw: done.append(1)
            ):
                runner.return_value.run.return_value = summary
                gateway._launch_resume(req, "用 Compose")
                import threading

                for thread in threading.enumerate():
                    if thread.name.startswith("agent-loop-resume"):
                        thread.join(10)
            self.assertEqual(adapters.call_args.kwargs["working_directory"], scope.repos["android"].path)
            self.assertEqual(runner.call_args.kwargs["verify_command"], "./gradlew test")
            self.assertIn("用 Compose", runner.return_value.run.call_args.args[0].goal)

    def test_the_chat_model_is_told_about_the_repos_only_when_there_are_some(self) -> None:
        with TemporaryDirectory() as tmp:
            gateway, _, scope = self._gateway(tmp)
            self.assertIn("repo=android::", gateway._repos_notice(scope))
            self.assertIn("beri-android", gateway._repos_notice(scope))
            plain = ProjectScope(name="P", workspace_id="ws_p", working_directory=Path(tmp), scope_file_path=Path(tmp) / "s")
            self.assertEqual(gateway._repos_notice(plain), "")
            self.assertIn("beri-android", gateway._identity_context(scope))


class ScannerAcrossReposTests(TestCase):
    def test_issues_from_every_repo_are_one_queue_and_each_is_worked_in_its_own_repo(self) -> None:
        with TemporaryDirectory() as tmp:
            gateway, store, _ = _build_schedule_gateway(Path(tmp))
            scope = two_repo_scope(Path(tmp))
            gateway.projects = [scope]

            def issues(repo_name, github_repo, checkout):
                number = 1 if repo_name is None else 2
                return [{"id": f"gh:{github_repo}#{number}", "title": f"issue {number}",
                         "created_at": "2026-10-0%dT00:00:00Z" % (2 if repo_name is None else 1),
                         "body": "", "url": "", "number": number, "repo": repo_name}]

            scanner = UnattendedScanner(
                MagicMock(), store, [scope], None, gateway, gateway.approval_manager
            )
            with patch.object(UnattendedScanner, "_list_github_issues", staticmethod(issues)):
                candidates = scanner._find_ready_candidates(scope)
                self.assertEqual([c["id"] for c in candidates], ["gh:acme/beri-android#2", "gh:acme/beri#1"])  # oldest first
                results = scanner.run_scan_pass()
            self.assertEqual(results["ws_beri"], "found_2_items")
            [req] = store.list_pending_approvals_for_workspace("ws_beri")
            self.assertEqual(req["working_directory"], str(scope.repos["android"].path))
            self.assertEqual(req["resume_goal"], "repo=android::Implement planned backlog item: gh:acme/beri-android#2")
            self.assertIn("repo：android", req["proposed_action"])
            self.assertEqual(store.get_conversation_repo(req["conversation_id"]), "android")


class AdapterTests(TestCase):
    def _adapter(self, permission: str):
        return ClaudeCliAdapter(
            adapter_id="a", role="r", executable="claude", working_directory=Path("/work"),
            trace_root=Path("/t"), timeout_seconds=1, permission=permission,
            read_directories=(Path("/other-repo"),),
        )

    def test_only_a_read_only_claude_role_may_look_at_the_other_repos(self) -> None:
        read_only = self._adapter("read-only").build_command("p")
        self.assertEqual(read_only[read_only.index("--add-dir") + 1], "/other-repo")
        self.assertNotIn("--add-dir", self._adapter("workspace-write").build_command("p"))


class RepoCommandTests(TestCase):
    def test_add_list_and_remove(self) -> None:
        from tests.workspace_support import workspace

        with workspace() as (root, _), TemporaryDirectory() as other:
            def run(*argv):
                with patch("builtins.print") as printed:
                    code = main(["-C", str(root), *argv])
                return code, "\n".join(str(c.args[0]) for c in printed.call_args_list if c.args)

            self.assertEqual(run("repo", "add", "android", other, "--verify", "./gradlew test", "--github", "acme/a",
                                 "--off-limits", "a.json, b.json")[0], 0)
            code, listing = run("repo", "list")
            self.assertEqual(code, 0)
            self.assertIn(f"android: {Path(other).resolve()}", listing)
            self.assertIn("verify: ./gradlew test", listing)
            repos = load_repos(root / ".harness" / "repos.toml")
            self.assertEqual(repos["android"].off_limits, ["a.json", "b.json"])
            for bad in (("repo", "add", "android", other), ("repo", "add", "primary", other),
                        ("repo", "add", "x y", other), ("repo", "add", "gone", other + "/nope"),
                        ("repo", "add", "same", str(root)), ("repo", "remove", "missing")):
                with self.subTest(bad=bad):
                    self.assertEqual(run(*bad)[0], 2)
            self.assertEqual(run("repo", "remove", "android")[0], 0)
            self.assertEqual(load_repos(root / ".harness" / "repos.toml"), {})

    def test_doctor_reports_a_repo_whose_folder_is_gone(self) -> None:
        from tests.workspace_support import workspace

        with workspace({"HARNESS_API_TOKEN": "t", "MEMTRACE_MCP_URL": "http://x"}) as (root, _), TemporaryDirectory() as other:
            (root / ".harness" / "repos.toml").write_text(
                dump_repos({"android": RepoScope(name="android", path=Path(other) / "gone")}), encoding="utf-8"
            )
            with patch("memtrace_harness.chat_tools_check.check_chat_tools", return_value=[]), patch(
                "builtins.print"
            ) as printed:
                code = main(["-C", str(root), "doctor"])
            text = "\n".join(str(c.args[0]) for c in printed.call_args_list if c.args)
            self.assertEqual(code, 1)
            self.assertIn("repo android", text)
            self.assertIn("is not a folder", text)
