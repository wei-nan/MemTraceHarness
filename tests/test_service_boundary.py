"""The split is a rule, not a habit: services and the Harness never import each other, and the
Harness carries nothing of any chat transport."""

import ast
import re
from pathlib import Path
from unittest import TestCase

SRC = Path(__file__).resolve().parent.parent / "src"


def imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module)
    return names


class BoundaryTests(TestCase):
    def test_services_never_import_the_harness(self) -> None:
        offenders = {
            str(path.relative_to(SRC)): sorted(m for m in imported_modules(path) if m.split(".")[0] == "memtrace_harness")
            for path in (SRC / "memtrace_services").rglob("*.py")
        }
        self.assertEqual({k: v for k, v in offenders.items() if v}, {})

    def test_the_harness_never_imports_a_service(self) -> None:
        offenders = {
            str(path.relative_to(SRC)): sorted(m for m in imported_modules(path) if m.split(".")[0] == "memtrace_services")
            for path in (SRC / "memtrace_harness").rglob("*.py")
        }
        self.assertEqual({k: v for k, v in offenders.items() if v}, {})

    def test_the_harness_has_no_chat_transport_in_it(self) -> None:
        # No Bot API calls, no bot token. (The words appear in workspace.py/migrate.py only to
        # refuse or move a token that is in the wrong place.)
        forbidden = re.compile(r"api\.telegram\.org|getUpdates|sendMessage|answerCallbackQuery|setMyCommands")
        hits = {
            path.name: forbidden.findall(path.read_text(encoding="utf-8"))
            for path in (SRC / "memtrace_harness").rglob("*.py")
        }
        self.assertEqual({k: v for k, v in hits.items() if v}, {})

    def test_the_harness_config_has_no_telegram_field(self) -> None:
        from dataclasses import fields

        from memtrace_harness.config import HarnessConfig

        self.assertEqual([f.name for f in fields(HarnessConfig) if "telegram" in f.name.lower()], [])

    def test_a_child_process_does_not_inherit_the_harness_secrets(self) -> None:
        from memtrace_harness.cli_process import child_env

        environ = {
            "PATH": "/bin", "HOME": "/h", "LANG": "C", "LC_ALL": "C", "CODEX_HOME": "/c",
            "MEMTRACE_API_TOKEN": "mt-secret", "HARNESS_API_TOKEN": "api-secret",
            "TELEGRAM_BOT_TOKEN": "bot-secret", "HARNESS_PASSTHROUGH_ENV": "KEEP_ME", "KEEP_ME": "yes",
            "AWS_SECRET_ACCESS_KEY": "aws-secret",
        }
        env = child_env(["claude", "--print", "x"], environ)
        self.assertEqual(
            env, {"PATH": "/bin", "HOME": "/h", "LANG": "C", "LC_ALL": "C", "CODEX_HOME": "/c", "KEEP_ME": "yes"}
        )

    def test_only_a_child_wired_to_the_memtrace_server_gets_its_token(self) -> None:
        from memtrace_harness.cli_process import child_env

        environ = {"PATH": "/bin", "MEMTRACE_API_TOKEN": "mt-secret"}
        wired = [
            "codex", "exec", "--config", 'mcp_servers.memtrace.url="https://x"',
            "--config", 'mcp_servers.memtrace.bearer_token_env_var="MEMTRACE_API_TOKEN"',
        ]
        self.assertEqual(child_env(wired, environ)["MEMTRACE_API_TOKEN"], "mt-secret")
        self.assertNotIn("MEMTRACE_API_TOKEN", child_env(["codex", "exec", "p"], environ))
        self.assertNotIn(
            "MEMTRACE_API_TOKEN", child_env(["codex", "--config", 'mcp_servers.memtrace.url="https://x"'], environ)
        )
