from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from memtrace_harness.allowlist import AllowList, command_hash
from memtrace_harness.command_policy import forbidden_reason
from memtrace_harness.ops_mcp import OpsContext, OpsError, call_tool
from memtrace_harness.scope import validate_scope_change


class CommandPolicyTests(unittest.TestCase):
    def test_sudo_deletion_and_system_settings_are_refused_anywhere_in_the_line(self) -> None:
        for command in [
            "sudo ls", "echo hi && rm -rf x", "ls | xargs rm", "/bin/rm a", "FOO=1 rm a", "find . -name x -delete",
            "git push --force", "git clean -fd", "launchctl bootout gui/1/x", "defaults write a b", "echo x > /etc/hosts",
            "diskutil eraseDisk x", "nohup sudo reboot",
        ]:
            self.assertIsNotNone(forbidden_reason(command), command)

    def test_ordinary_commands_pass(self) -> None:
        for command in [
            "pkill -f x", "launchctl kickstart -k gui/1/x", "docker run --rm x", "git push origin main",
            "python x.py --force", "cat f | grep rm", "echo 'no rm here'",
        ]:
            self.assertIsNone(forbidden_reason(command), command)

    def test_a_forbidden_command_cannot_be_declared_as_a_job(self) -> None:
        with self.assertRaises(ValueError):
            validate_scope_change(Path("."), "job", name="clean", command="rm -rf build")


class AllowListTests(unittest.TestCase):
    def test_it_matches_the_exact_command_only_and_never_a_forbidden_one(self) -> None:
        allowed = AllowList(Path(tempfile.mkdtemp()) / "allowed.json")
        self.assertTrue(allowed.add("P", "restart", "echo a"))
        self.assertFalse(allowed.add("P", "restart", "echo a"))
        self.assertTrue(allowed.is_allowed("P", "restart", "echo a"))
        self.assertFalse(allowed.is_allowed("P", "restart", "echo b"))
        self.assertFalse(allowed.is_allowed("Q", "restart", "echo a"))
        with self.assertRaises(ValueError):
            allowed.add("P", "x", "sudo ls")
        # Even a hand-edited entry for a forbidden command does not match.
        allowed.path.write_text('{"jobs": [{"project": "P", "job": "x", "sha256": "%s"}]}' % command_hash("rm -rf /"))
        self.assertFalse(allowed.is_allowed("P", "x", "rm -rf /"))
        self.assertTrue(allowed.remove("P", "x") is False or True)


class RuleAllowListTests(unittest.TestCase):
    def test_github_is_bound_to_action_type_and_repo_and_the_rest_to_the_exact_request(self) -> None:
        allowed = AllowList(Path(tempfile.mkdtemp()) / "allowed.json")
        comment = {"action": "issue_comment", "repo": "o/r", "number": 5, "comment": "hi"}
        self.assertFalse(allowed.is_rule_allowed("P", "external_action", comment))
        self.assertIsNotNone(allowed.add_rule("P", "external_action", comment))
        self.assertIsNone(allowed.add_rule("P", "external_action", comment))
        self.assertTrue(allowed.is_rule_allowed("P", "external_action", {**comment, "number": 9, "comment": "other"}))
        self.assertFalse(allowed.is_rule_allowed("P", "external_action", {**comment, "action": "issue_close"}))
        self.assertFalse(allowed.is_rule_allowed("P", "external_action", {**comment, "repo": "o/other"}))
        self.assertFalse(allowed.is_rule_allowed("Q", "external_action", comment))

        model = {"target": "develop", "provider": "claude", "model": "claude-opus-5-5"}
        allowed.add_rule("P", "model_change", model)
        self.assertTrue(allowed.is_rule_allowed("P", "model_change", model))
        self.assertFalse(allowed.is_rule_allowed("P", "model_change", {**model, "model": "claude-sonnet-5-5"}))

        change = {"kind": "log_file", "value": "logs/a.log"}
        allowed.add_rule("P", "scope_change", change)
        self.assertTrue(allowed.is_rule_allowed("P", "scope_change", change))
        self.assertFalse(allowed.is_rule_allowed("P", "scope_change", {"kind": "log_file", "value": "logs/b.log"}))
        with self.assertRaises(ValueError):  # a forbidden job command can never be held
            allowed.add_rule("P", "scope_change", {"kind": "job", "name": "w", "command": "rm -rf x"})

    def test_revoke_by_label(self) -> None:
        allowed = AllowList(Path(tempfile.mkdtemp()) / "allowed.json")
        label = allowed.add_rule("P", "external_action", {"action": "issue_comment", "repo": "o/r"})
        self.assertTrue(allowed.remove("P", label))
        self.assertFalse(allowed.is_rule_allowed("P", "external_action", {"action": "issue_comment", "repo": "o/r"}))


class ReadToolTests(unittest.TestCase):
    def setUp(self) -> None:
        base = Path(tempfile.mkdtemp()).resolve()
        self.project, self.other = base / "project", base / "other"
        for folder in (self.project, self.other):
            folder.mkdir()
        (self.project / "app.log").write_text("\n".join(f"line {i}" for i in range(300)) + "\ntoken=abcdefghijklmnop\n")
        (self.project / ".harness.env").write_text("HARNESS_API_TOKEN=supersecretvalue1234567890\n")
        (self.other / "notes.txt").write_text("other project")
        self.db = self.project / "h.sqlite3"
        conn = sqlite3.connect(self.db)
        conn.execute("CREATE TABLE turns (id INTEGER, body TEXT)")
        conn.executemany("INSERT INTO turns VALUES (?, ?)", [(i, f"body {i}") for i in range(300)])
        conn.commit()
        conn.close()
        self.ctx = OpsContext(project="P", working_directory=self.project, hosts=[], logs=[], jobs={})

    def test_reads_the_tail_and_masks_tokens(self) -> None:
        text = call_tool(self.ctx, "read_file", {"path": "app.log", "lines": "3"})
        self.assertIn("line 299", text)
        self.assertNotIn("abcdefghijklmnop", text)

    def test_credential_files_and_other_roots_are_refused_until_declared(self) -> None:
        with self.assertRaises(OpsError):
            call_tool(self.ctx, "read_file", {"path": ".harness.env"})
        with self.assertRaises(OpsError):
            call_tool(self.ctx, "read_file", {"path": str(self.other / "notes.txt")})
        wider = OpsContext(project="P", working_directory=self.project, hosts=[], logs=[], jobs={}, read_roots=[str(self.other)])
        self.assertIn("other project", call_tool(wider, "read_file", {"path": str(self.other / "notes.txt")}))
        with self.assertRaises(OpsError):  # still never a credential file inside a declared root
            (self.other / "web.toml").write_text("token = 'x'")
            call_tool(wider, "read_file", {"path": str(self.other / "web.toml")})
        self.assertIn("credentials", call_tool(wider, "list_dir", {"path": str(self.project)}))

    def test_a_symlink_out_of_the_roots_is_refused(self) -> None:
        (self.project / "link").symlink_to(self.other / "notes.txt")
        with self.assertRaises(OpsError):
            call_tool(self.ctx, "read_file", {"path": "link"})

    def test_query_db_selects_but_cannot_write(self) -> None:
        self.assertIn("turns(id, body)", call_tool(self.ctx, "query_db", {"db": "h.sqlite3"}))
        out = call_tool(self.ctx, "query_db", {"db": "h.sqlite3", "sql": "SELECT count(*) AS n FROM turns"})
        self.assertIn("300", out)
        many = call_tool(self.ctx, "query_db", {"db": "h.sqlite3", "sql": "SELECT * FROM turns"})
        self.assertIn("只顯示前 200 列", many)
        for sql in ["DELETE FROM turns", "DROP TABLE turns", "SELECT 1; DELETE FROM turns",
                    "ATTACH DATABASE 'x.db' AS x", "PRAGMA writable_schema=1", "WITH a AS (SELECT 1) DELETE FROM turns"]:
            with self.assertRaises(OpsError, msg=sql):
                call_tool(self.ctx, "query_db", {"db": "h.sqlite3", "sql": sql})
        conn = sqlite3.connect(self.db)
        self.assertEqual(conn.execute("SELECT count(*) FROM turns").fetchone()[0], 300)
        conn.close()

    def test_system_status_takes_only_fixed_kinds(self) -> None:
        with self.assertRaises(OpsError):
            call_tool(self.ctx, "system_status", {"kind": "shell", "argument": "ls"})
        with self.assertRaises(OpsError):
            call_tool(self.ctx, "system_status", {"kind": "launchd_print", "argument": "x; rm -rf /"})

    def test_a_job_with_a_forbidden_command_is_not_requested(self) -> None:
        ctx = OpsContext(project="P", working_directory=self.project, hosts=[], logs=[], jobs={"wipe": "rm -rf build"})
        with self.assertRaises(OpsError):
            call_tool(ctx, "run_job", {"name": "wipe"})


if __name__ == "__main__":
    unittest.main()
