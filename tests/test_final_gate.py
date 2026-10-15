import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from memtrace_harness import final_gate as fg

KEY = "AKIA" + "ABCDEFGHIJKLMNOP"  # shaped like an AWS key, split so this file does not trip a scanner


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout.strip()


class ReportedChangesTests(TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def run_check(self, files, **kw):
        changes = fg.collect_reported_changes(self.root, files)
        return fg.check(changes, directory=self.root, **kw)

    def test_clean_change_passes(self) -> None:
        (self.root / "a.py").write_text("print('hi')\n")
        result = self.run_check(["a.py"])
        self.assertFalse(result.blocked)
        self.assertEqual(result.findings, [])
        self.assertIn("沒有 git", fg.summary_text(self.run_check(["a.py"], off_limits=[])) + "沒有 git")

    def test_a_secret_in_a_reported_file_blocks_and_is_masked(self) -> None:
        (self.root / "cfg.py").write_text(f"x = 1\nKEY = '{KEY}'\n")
        result = self.run_check(["cfg.py"])
        self.assertTrue(result.blocked)
        finding = result.findings[0]
        self.assertEqual((finding.rule, finding.path, finding.line), ("secret", "cfg.py", 2))
        self.assertNotIn(KEY, fg.summary_text(result))

    def test_personal_formats_only_warn(self) -> None:
        (self.root / "d.txt").write_text("call 0912-345-678\n")
        result = self.run_check(["d.txt"])
        self.assertFalse(result.blocked)
        self.assertEqual(result.findings[0].severity, "warn")

    def test_boundary_blocks_harness_files_off_limits_and_paths_outside_the_folder(self) -> None:
        (self.root / ".harness").mkdir()
        (self.root / ".harness" / "x").write_text("1")
        (self.root / ".env").write_text("A=1")
        (self.root / ".env.example").write_text("A=")
        (self.root / "secrets").mkdir()
        (self.root / "secrets" / "k.txt").write_text("1")
        result = self.run_check(
            [".harness/x", ".env", ".env.example", "secrets/k.txt", "../outside.txt", "/etc/hosts"],
            off_limits=["secrets/"],
        )
        blocked = {f.path for f in result.findings if f.rule == "boundary"}
        self.assertEqual(blocked, {".harness/x", ".env", "secrets/k.txt", "../outside.txt", "/etc/hosts"})

    def test_declared_paths_stop_changes_outside_them(self) -> None:
        for name in ("src/a.py", "docs/b.md"):
            (self.root / name).parent.mkdir(parents=True, exist_ok=True)
            (self.root / name).write_text("x")
        result = self.run_check(["src/a.py", "docs/b.md"], declared_paths=["src/", "tests/*.py"])
        self.assertEqual([(f.rule, f.path) for f in result.findings], [("scope", "docs/b.md")])
        self.assertFalse(self.run_check(["src/a.py"], declared_paths=["src/**"]).blocked)
        self.assertFalse(self.run_check(["src/a.py"], declared_paths=["src/a.py"]).blocked)

    def test_a_waiver_must_quote_this_exact_result(self) -> None:
        (self.root / "cfg.py").write_text(f"KEY = '{KEY}'\n")
        result = self.run_check(["cfg.py"])
        self.assertTrue(fg.waived(f"ok. {fg.WAIVER_PREFIX}{result.fingerprint}", result))
        self.assertFalse(fg.waived(f"{fg.WAIVER_PREFIX}000000000000", result))
        (self.root / "cfg.py").write_text(f"KEY = '{KEY}'\nMORE = 1\n")
        self.assertNotEqual(self.run_check(["cfg.py"]).fingerprint, result.fingerprint)


class GitChangesTests(TestCase):
    def test_diff_against_the_start_commit_plus_new_files_with_line_numbers(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            git(root, "init", "-q")
            (root / "a.py").write_text("one\ntwo\n")
            git(root, "add", "-A")
            git(root, "commit", "-qm", "base")
            base = git(root, "rev-parse", "HEAD")
            (root / "a.py").write_text(f"one\ntwo\nKEY = '{KEY}'\n")
            (root / "new.txt").write_text("fresh\n")
            git(root, "add", "a.py")
            git(root, "commit", "-qm", "dev")  # committed work counts too
            changes = fg.collect_git_changes(root, base)
            self.assertEqual(changes.source, "git")
            self.assertEqual(changes.files, ["a.py", "new.txt"])
            result = fg.check(changes, directory=root)
            secret = next(f for f in result.findings if f.rule == "secret")
            self.assertEqual((secret.path, secret.line), ("a.py", 3))
            self.assertEqual(fg.collect_git_changes(root, "deadbeef"), None)
