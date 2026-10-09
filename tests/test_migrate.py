import argparse
import json
import sqlite3
import stat
import tomllib
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from memtrace_harness.migrate import migrate_command, plan_workspaces, slug
from memtrace_harness.scope import load_project_index
from memtrace_harness.trace_store import TraceStore
from memtrace_harness.workspace import ENV_FILE_NAME, parse_env_file, read_model_settings

LEGACY = """\
MEMTRACE_MCP_URL=https://kb.example/mcp
MEMTRACE_API_TOKEN=mt-secret
HARNESS_CLAUDE_COMMAND=/opt/claude
HARNESS_TELEGRAM_BOT_TOKEN=shared-bot
HARNESS_TELEGRAM_ALLOWED_CHAT_IDS=111,222
HARNESS_TELEGRAM_BOT_TOKEN_ALPHA=alpha-bot
HARNESS_CHAT_PROVIDER=claude
HARNESS_CHAT_MODEL=haiku
HARNESS_CHAT_FALLBACKS=antigravity/gemini
HARNESS_CHAT_MODEL_ALPHA=sonnet
HARNESS_DIGEST_PROVIDER=claude
HARNESS_DIGEST_MODEL=claude-sonnet-5-5
HARNESS_MEMORY_WORKSPACE_ID=ws_shared_mem
HARNESS_MEMORY_WORKSPACE_ID_BETA_PROJECT=ws_beta_mem
HARNESS_ROLE_PROFILES_FILE_ALPHA={roles}
HARNESS_PROJECT_INDEX={index}
HARNESS_TRACE_DB={db}
HARNESS_TRACE_ROOT={traces}
HARNESS_STATUS_SERVER_PORT=8787
"""


class MigrationFixture(TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name).resolve()
        self.alpha, self.beta = self.tmp / "alpha", self.tmp / "beta-project"
        for d, ws in ((self.alpha, "ws_alpha"), (self.beta, "ws_beta")):
            d.mkdir()
            (d / "harness-scope.md").write_text(f"# scope\n\n- workspace_id: {ws}\n", encoding="utf-8")
        self.index = self.tmp / "projects.index.txt"
        self.index.write_text(f"{self.alpha / 'harness-scope.md'}\n{self.beta / 'harness-scope.md'}\n", encoding="utf-8")
        self.roles = self.tmp / "alpha-roles.toml"
        self.roles.write_text("[profiles.controller]\n", encoding="utf-8")
        self.db = self.tmp / "legacy.sqlite3"
        self.traces = self.tmp / "traces"
        self.env = self.tmp / ".env"
        self.env.write_text(
            LEGACY.format(roles=self.roles, index=self.index, db=self.db, traces=self.traces), encoding="utf-8"
        )
        self._seed()

    def _seed(self) -> None:
        store = TraceStore(self.db)
        for project, scope, text in (
            ("alpha", "global", "全域"), ("alpha", "project", "只有 alpha"), ("beta-project", "project", "只有 beta"),
        ):
            store.add_preference_candidate(
                project=project, scope=scope, category="x", text=text, evidence=[], explicit=True,
                source_digest_date=None, status="adopted", adopted_by="operator",
            )
        with sqlite3.connect(self.db) as conn:
            for project, ws in (("alpha", "ws_alpha"), ("beta-project", "ws_beta")):
                run = f"run_{project[:3]}"
                conn.execute("INSERT INTO runs (id, workspace_id, goal, task_json, created_at) VALUES (?, ?, 'g', '{}', 't')", (run, ws))
                conn.execute(
                    "INSERT INTO conversations (id, task_id, workspace_id, state, created_at, updated_at, last_activity_at) "
                    "VALUES (?, 't', ?, 'succeeded', 't', 't', 't')", (f"chat_{project[:3]}", ws),
                )
                conn.execute(
                    "INSERT INTO turns (conversation_id, run_id, sequence, stage, profile_id, provider, state, usage_json, "
                    "raw_trace_ref, created_at) VALUES (?, ?, 1, 'plan', 'p', 'claude', 'succeeded', '{}', ?, 't')",
                    (f"chat_{project[:3]}", run, f"{self.traces}/{run}/out.log"),
                )
                conn.execute(
                    "INSERT INTO primary_sessions_hot_log (primary_session_id, project, turn_seq, created_at, speaker, turn_type, content) "
                    "VALUES (?, ?, 1, 't', 'user', 'chat', ?)", (f"psess_{project}", project, f"hello {project}"),
                )
                (self.traces / run).mkdir(parents=True)
                (self.traces / run / "out.log").write_text(f"trace of {project}", encoding="utf-8")
            conn.execute("CREATE TABLE telegram_gateway_state (bot_token_hash TEXT PRIMARY KEY, last_offset INTEGER NOT NULL)")
            import hashlib

            conn.execute(
                "INSERT INTO telegram_gateway_state VALUES (?, 77)", (hashlib.sha256(b"alpha-bot").hexdigest(),)
            )
            # the old shape: an approval remembered as a (chat, message) pair, a schedule by chat id
            conn.execute("ALTER TABLE approval_requests ADD COLUMN telegram_chat_id INTEGER")
            conn.execute("ALTER TABLE approval_requests ADD COLUMN telegram_message_id INTEGER")
            conn.execute(
                "INSERT INTO approval_requests (id, conversation_id, workspace, working_directory, reason, proposed_action, "
                "status, created_at, telegram_chat_id, telegram_message_id) "
                "VALUES ('appr_old', 'chat_alp', 'ws_alpha', '/x', 'git_push', 'x', 'pending', 't', 111, 42)"
            )
            conn.execute("ALTER TABLE schedules ADD COLUMN chat_id INTEGER")
            conn.execute(
                "INSERT INTO schedules (id, project, workspace_id, goal, kind, active, created_at, next_run_at, chat_id) "
                "VALUES ('sched_old', 'alpha', 'ws_alpha', 'g', 'daily', 1, 't', 't', 111)"
            )
            conn.commit()

    def apply(self) -> int:
        return migrate_command(
            argparse.Namespace(env=self.env, index=None, services_dir=self.tmp / "services", first_port=8800, apply=True)
        )


class PlanTests(MigrationFixture):
    def test_suffixed_settings_become_plain_ones_and_each_project_gets_its_own_token_and_port(self) -> None:
        plans = plan_workspaces(parse_env_file(self.env), load_project_index(self.index))
        alpha, beta = plans
        self.assertEqual(alpha.env["HARNESS_CHAT_MODEL"], "sonnet")        # its own override
        self.assertEqual(beta.env["HARNESS_CHAT_MODEL"], "haiku")          # the global one
        self.assertEqual(alpha.env["HARNESS_MEMORY_WORKSPACE_ID"], "ws_shared_mem")
        self.assertEqual(beta.env["HARNESS_MEMORY_WORKSPACE_ID"], "ws_beta_mem")
        self.assertEqual(alpha.env["MEMTRACE_API_TOKEN"], "mt-secret")
        self.assertNotEqual(alpha.api_port, beta.api_port)
        self.assertNotEqual(alpha.api_token, beta.api_token)
        for plan in plans:
            self.assertFalse([k for k in plan.env if "TELEGRAM" in k or k.endswith(("_ALPHA", "_BETA_PROJECT"))])
        self.assertEqual(alpha.roles_source, self.roles)

    def test_a_bot_of_its_own_moves_to_a_service_env_and_a_shared_bot_is_flagged(self) -> None:
        alpha, beta = plan_workspaces(parse_env_file(self.env), load_project_index(self.index))
        self.assertEqual(alpha.telegram["TELEGRAM_BOT_TOKEN"], "alpha-bot")
        self.assertEqual(alpha.telegram["TELEGRAM_ALLOWED_CHAT_IDS"], "111,222")
        self.assertEqual(alpha.telegram["HARNESS_API_TOKEN"], alpha.api_token)
        self.assertEqual(alpha.telegram["HARNESS_URL"], f"http://127.0.0.1:{alpha.api_port}")
        self.assertIsNone(beta.telegram)
        self.assertTrue(any("shared the global bot" in n for n in beta.notes))

    def test_slug_matches_the_old_suffix_rule(self) -> None:
        self.assertEqual(slug("beri-android"), "BERI_ANDROID")
        self.assertEqual(slug("project-c"), "PROJECT_C")


class ApplyTests(MigrationFixture):
    def test_a_dry_run_writes_nothing(self) -> None:
        self.assertEqual(
            migrate_command(argparse.Namespace(env=self.env, index=None, services_dir=self.tmp / "services", first_port=8800, apply=False)), 0
        )
        self.assertFalse((self.alpha / ENV_FILE_NAME).exists())
        self.assertFalse((self.tmp / "services").exists())

    def test_apply_builds_the_workspaces_the_services_env_and_leaves_the_legacy_files_alone(self) -> None:
        before = self.env.read_text(encoding="utf-8")
        self.assertEqual(self.apply(), 0)
        self.assertEqual(self.env.read_text(encoding="utf-8"), before)
        for root in (self.alpha, self.beta):
            self.assertEqual(stat.S_IMODE((root / ENV_FILE_NAME).stat().st_mode), 0o600)
            self.assertTrue((root / ".harness" / "harness.sqlite3").is_file())
        alpha_env = parse_env_file(self.alpha / ENV_FILE_NAME)
        self.assertNotIn("HARNESS_ROLE_PROFILES_FILE", alpha_env)  # found by its place
        self.assertEqual((self.alpha / ".harness" / "roles.toml").read_text(encoding="utf-8"), "[profiles.controller]\n")
        # environment and preferences are two files
        self.assertEqual(set(alpha_env), {"MEMTRACE_MCP_URL", "MEMTRACE_API_TOKEN", "HARNESS_CLAUDE_COMMAND", "HARNESS_API_PORT", "HARNESS_API_TOKEN"})
        preferences = read_model_settings(self.alpha / ".harness" / "models.toml")
        self.assertEqual(preferences["HARNESS_CHAT_MODEL"], "sonnet")
        self.assertEqual(preferences["HARNESS_CHAT_FALLBACKS"], "antigravity/gemini")
        self.assertEqual(preferences["HARNESS_MEMORY_WORKSPACE_ID"], "ws_shared_mem")
        self.assertEqual(read_model_settings(self.beta / ".harness" / "models.toml")["HARNESS_MEMORY_WORKSPACE_ID"], "ws_beta_mem")
        telegram = parse_env_file(self.tmp / "services" / "alpha" / "telegram.env")
        self.assertEqual(telegram["TELEGRAM_BOT_TOKEN"], "alpha-bot")
        self.assertEqual(telegram["HARNESS_API_TOKEN"], alpha_env["HARNESS_API_TOKEN"])
        web = (self.tmp / "services" / "web.toml").read_text(encoding="utf-8")
        self.assertIn(f'token = "{alpha_env["HARNESS_API_TOKEN"]}"', web)
        self.assertEqual(web.count("[[harness]]"), 2)
        self.assertEqual(
            sorted(p.name for p in (self.tmp / "services" / "launchd").iterdir()),
            ["com.memtraceharness.alpha.plist", "com.memtraceharness.alpha.telegram.plist",
             "com.memtraceharness.beta-project.plist", "com.memtraceharness.web.plist"],
        )

    def test_each_store_holds_only_its_own_project(self) -> None:
        self.apply()
        for root, mine, other_text, my_text in (
            (self.alpha, "alpha", "只有 beta", "只有 alpha"), (self.beta, "beta-project", "只有 alpha", "只有 beta"),
        ):
            store = TraceStore(root / ".harness" / "harness.sqlite3")
            with store._connection() as conn:
                runs = [r[0] for r in conn.execute("SELECT id FROM runs")]
                turns = [r[0] for r in conn.execute("SELECT run_id FROM turns")]
                logged = [r[0] for r in conn.execute("SELECT content FROM primary_sessions_hot_log")]
                prefs = {r[0] for r in conn.execute("SELECT text FROM preference_rules")}
                tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertEqual(runs, [f"run_{mine[:3]}"])
            self.assertEqual(turns, [f"run_{mine[:3]}"])
            self.assertEqual(logged, [f"hello {mine}"])
            self.assertIn("全域", prefs)  # the operator's global preference is everyone's
            self.assertIn(my_text, prefs)
            self.assertNotIn(other_text, prefs)
            self.assertNotIn("telegram_gateway_state", tables)  # the offset lives in the Telegram service now

    def test_raw_traces_follow_their_store_and_the_rows_point_at_the_new_place(self) -> None:
        self.apply()
        store = TraceStore(self.alpha / ".harness" / "harness.sqlite3")
        with store._connection() as conn:
            [ref] = [r[0] for r in conn.execute("SELECT raw_trace_ref FROM turns")]
        self.assertEqual(ref, f"{self.alpha / '.harness' / 'traces'}/run_alp/out.log")
        self.assertEqual(Path(ref).read_text(encoding="utf-8"), "trace of alpha")
        self.assertFalse((self.alpha / ".harness" / "traces" / "run_bet").exists())

    def test_it_refuses_to_overwrite_a_workspace_that_already_has_settings(self) -> None:
        (self.alpha / ENV_FILE_NAME).write_text("HARNESS_CHAT_MODEL=mine\n", encoding="utf-8")
        with self.assertRaises(RuntimeError):
            self.apply()
        self.assertEqual((self.alpha / ENV_FILE_NAME).read_text(encoding="utf-8"), "HARNESS_CHAT_MODEL=mine\n")


class LegacyStoreTests(MigrationFixture):
    def test_old_chat_and_message_ids_become_the_opaque_references_services_use(self) -> None:
        self.apply()
        store = TraceStore(self.alpha / ".harness" / "harness.sqlite3")
        self.assertEqual(store.get_approval_request("appr_old")["message_ref"], "tg:111:42")
        self.assertEqual(store.get_approval_request_by_message_ref("tg:111:42")["id"], "appr_old")
        [schedule] = store.list_schedules("alpha")
        self.assertEqual(schedule["audience"], "tg:111")


class SecondInstanceTests(MigrationFixture):
    def test_the_telegram_offset_is_carried_so_nothing_is_replayed(self) -> None:
        self.apply()
        state = json.loads((self.tmp / "services" / "alpha" / "telegram.state.json").read_text(encoding="utf-8"))
        self.assertEqual(state, {"offset": 77})
        # and the Telegram service starts from it
        from memtrace_services.telegram.service import TelegramSettings

        settings = TelegramSettings.from_values(
            {**parse_env_file(self.tmp / "services" / "alpha" / "telegram.env")}, env_file=self.tmp / "services" / "alpha" / "telegram.env"
        )
        self.assertEqual(json.loads(settings.state_file.read_text(encoding="utf-8"))["offset"], 77)

    def test_a_second_migration_keeps_the_first_ones_harnesses_on_the_status_page(self) -> None:
        self.apply()
        other = self.tmp / "other"
        other.mkdir()
        (other / "harness-scope.md").write_text("# s\n\n- workspace_id: ws_other\n", encoding="utf-8")
        index2 = self.tmp / "index2.txt"
        index2.write_text(f"{other / 'harness-scope.md'}\n", encoding="utf-8")
        env2 = self.tmp / "second.env"
        env2.write_text(f"MEMTRACE_MCP_URL=https://kb.example/mcp\nHARNESS_PROJECT_INDEX={index2}\n", encoding="utf-8")
        migrate_command(argparse.Namespace(env=env2, index=None, services_dir=self.tmp / "services", first_port=8805, apply=True))
        web = tomllib.loads((self.tmp / "services" / "web.toml").read_text(encoding="utf-8"))
        self.assertEqual([h["name"] for h in web["harness"]], ["alpha", "beta-project", "other"])
        ports = [int(h["url"].rsplit(":", 1)[1]) for h in web["harness"]]
        self.assertEqual(len(set(ports)), 3)
        self.assertGreaterEqual(ports[2], 8805)  # no clash with the first run's ports
        self.assertEqual(web["port"], 8787)


class SharedKnowledgeBaseTests(MigrationFixture):
    """Two legacy projects on one MemTrace workspace are one Harness with two repos."""

    def setUp(self) -> None:
        super().setUp()
        (self.beta / "harness-scope.md").write_text(
            "# scope\n\n- workspace_id: ws_alpha\n- verify_command: ./gradlew test\n- github_repo: acme/beta\n"
            "- off_limits: google-services.json\n",
            encoding="utf-8",
        )
        with sqlite3.connect(self.db) as conn:
            for table in ("runs", "conversations"):  # the second project's work was recorded against the shared workspace
                conn.execute(f"UPDATE {table} SET workspace_id = 'ws_alpha' WHERE workspace_id = 'ws_beta'")
            conn.execute(
                "INSERT INTO schedules (id, project, workspace_id, goal, kind, active, created_at, next_run_at) "
                "VALUES ('sched_beta', 'beta-project', 'ws_alpha', '每天跑測試', 'daily', 1, 't', 't')"
            )
            conn.execute(
                "INSERT INTO approval_requests (id, conversation_id, workspace, working_directory, reason, proposed_action, "
                "status, created_at) VALUES ('appr_beta', 'chat_bet', 'ws_alpha', ?, 'git_push', 'x', 'pending', 't')",
                (str(self.beta),),
            )
            conn.execute(
                "INSERT INTO decision_records (created_at, project, kind, situation, outcome) "
                "VALUES ('t', 'beta-project', 'task_claim', 's', 'claim_accepted')"
            )
            conn.commit()

    def test_they_become_one_plan_with_the_second_as_a_repo(self) -> None:
        plans = plan_workspaces(parse_env_file(self.env), load_project_index(self.index))
        self.assertEqual([p.scope.name for p in plans], ["alpha"])
        [plan] = plans
        repo = plan.repos["beta-project"]
        self.assertEqual((repo.path, repo.verify_command, repo.github_repo, repo.off_limits),
                         (self.beta, "./gradlew test", "acme/beta", ["google-services.json"]))
        self.assertTrue(any("also serves beta-project as repo 'beta-project'" in n for n in plan.notes))
        by_choice = plan_workspaces(parse_env_file(self.env), load_project_index(self.index), primaries=("beta-project",))
        self.assertEqual(by_choice[0].scope.name, "beta-project")
        self.assertEqual(list(by_choice[0].repos), ["alpha"])

    def test_apply_writes_one_workspace_a_repos_file_and_one_bot(self) -> None:
        self.apply()
        self.assertFalse((self.beta / ENV_FILE_NAME).exists())  # no workspace of its own
        from memtrace_harness.scope import load_repos

        repos = load_repos(self.alpha / ".harness" / "repos.toml")
        self.assertEqual(list(repos), ["beta-project"])
        self.assertEqual(repos["beta-project"].path, self.beta)
        self.assertEqual(
            sorted(p.name for p in (self.tmp / "services" / "launchd").iterdir()),
            ["com.memtraceharness.alpha.plist", "com.memtraceharness.alpha.telegram.plist", "com.memtraceharness.web.plist"],
        )
        self.assertEqual((self.tmp / "services" / "web.toml").read_text(encoding="utf-8").count("[[harness]]"), 1)

    def test_the_merged_projects_history_joins_the_primary_without_its_transcript(self) -> None:
        self.apply()
        store = TraceStore(self.alpha / ".harness" / "harness.sqlite3")
        with store._connection() as conn:
            projects = {r[0] for r in conn.execute("SELECT project FROM primary_sessions_hot_log")}
            decisions = [r[0] for r in conn.execute("SELECT project FROM decision_records")]
            runs = sorted(r[0] for r in conn.execute("SELECT id FROM runs"))
        self.assertEqual(projects, {"alpha"})  # its chat transcript is not merged
        self.assertEqual(decisions, ["alpha"])  # its decisions are, under the primary's name
        self.assertEqual(runs, ["run_alp", "run_bet"])  # both repos' runs, as they share the workspace
        [schedule] = [s for s in store.list_schedules("alpha") if s["id"] == "sched_beta"]
        self.assertEqual(schedule["goal"], "repo=beta-project::每天跑測試")
        self.assertEqual(store.get_conversation_repo("chat_bet"), "beta-project")  # a pending approval resumes in its repo
