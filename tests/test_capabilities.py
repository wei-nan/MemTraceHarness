from __future__ import annotations

import hashlib
import json
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from memtrace_harness import capabilities as cap

SHA = "a" * 64


class ValidationTests(unittest.TestCase):
    def test_a_reason_is_required_and_kinds_are_closed(self) -> None:
        with self.assertRaises(cap.InstallError):
            cap.validate_install({"kind": "pip", "name": "requests"})
        with self.assertRaises(cap.InstallError):
            cap.validate_install({"kind": "rpm", "name": "x", "reason": "r"})

    def test_package_names_cannot_be_paths_urls_or_options(self) -> None:
        for kind, name in [
            ("pip", "../evil"), ("pip", "git+https://x/y"), ("pip", "-r requirements.txt"), ("pip", "a b"),
            ("npm", "user/repo"), ("npm", "https://x/y.tgz"), ("npm", "--global"), ("brew", "-v"), ("brew", "a;b"),
        ]:
            with self.assertRaises(cap.InstallError, msg=f"{kind} {name}"):
                cap.validate_install({"kind": kind, "name": name, "reason": "r"})
        for kind, name in [("pip", "requests"), ("pip", "uvicorn[standard]"), ("npm", "@scope/pkg"), ("npm", "left-pad"), ("brew", "ffmpeg"), ("brew", "owner/tap/tool")]:
            self.assertEqual(cap.validate_install({"kind": kind, "name": name, "reason": "r"})["name"], name)

    def test_a_model_file_needs_https_and_a_sha256(self) -> None:
        good = {"kind": "model_file", "url": "https://example.com/m.pt", "filename": "m.pt", "sha256": SHA, "reason": "r"}
        self.assertEqual(cap.validate_install(good)["sha256"], SHA)
        for patch in ({"url": "http://example.com/m.pt"}, {"sha256": "abc"}, {"filename": "../m.pt"}, {"url": "https://x/y z"}):
            with self.assertRaises(cap.InstallError, msg=str(patch)):
                cap.validate_install({**good, **patch})

    def test_script_and_git_sources_are_allowed_but_still_checked(self) -> None:
        self.assertEqual(cap.validate_install({"kind": "script", "url": "https://x.sh/i.sh", "reason": "r"})["kind"], "script")
        with self.assertRaises(cap.InstallError):
            cap.validate_install({"kind": "script", "url": "http://x.sh/i.sh", "reason": "r"})
        git = {"kind": "git", "url": "https://github.com/o/r.git", "name": "r", "reason": "r"}
        self.assertEqual(cap.validate_install(git)["name"], "r")
        self.assertEqual(cap.validate_install({**git, "url": "git@github.com:o/r.git"})["url"], "git@github.com:o/r.git")
        with self.assertRaises(cap.InstallError):
            cap.validate_install({**git, "url": "file:///etc"})
        for command in ("sudo make install", "rm -rf build", "make && sudo cp x /usr/local/bin"):
            with self.assertRaises(cap.InstallError, msg=command):
                cap.validate_install({**git, "install_command": command})
        self.assertEqual(cap.validate_install({**git, "install_command": "make"})["install_command"], "make")

    def test_a_skill_needs_content_or_a_fetchable_url(self) -> None:
        self.assertEqual(cap.validate_install({"kind": "skill", "name": "my-skill", "content": "# x", "reason": "r"})["name"], "my-skill")
        got = cap.validate_install(
            {"kind": "skill", "name": "s", "url": "https://x/SKILL.md", "reason": "r"}, fetch=lambda url, limit: "# fetched"
        )
        self.assertEqual(got["content"], "# fetched")
        with self.assertRaises(cap.InstallError):
            cap.validate_install({"kind": "skill", "name": "Bad Name", "content": "x", "reason": "r"})
        with self.assertRaises(cap.InstallError):
            cap.validate_install({"kind": "skill", "name": "s", "reason": "r"})

    def test_the_card_says_what_runs_and_warns(self) -> None:
        brew = "\n".join(cap.card_lines(cap.validate_install({"kind": "brew", "name": "ffmpeg", "reason": "轉檔"})))
        self.assertIn("安裝到系統", brew)
        script = "\n".join(cap.card_lines(cap.validate_install({"kind": "script", "url": "https://x/i.sh", "reason": "r"})))
        self.assertIn("無法事先審查", script)
        self.assertIn("sudo", script)


class FakeRun:
    def __init__(self, code: int = 0, out: str = "") -> None:
        self.calls: list[tuple] = []
        self.code, self.out = code, out

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        return SimpleNamespace(returncode=self.code, stdout=self.out, stderr="")


class RunTests(unittest.TestCase):
    def setUp(self) -> None:
        self.state = Path(tempfile.mkdtemp()) / ".harness"
        self.state.mkdir()

    def test_pip_installs_into_the_workspace_with_a_fixed_argv_and_is_recorded(self) -> None:
        run = FakeRun()
        payload = cap.validate_install({"kind": "pip", "name": "requests", "version": "2.32.0", "reason": "打 API"})
        ok, _text, _meta = cap.run_install(payload, self.state, runner=run)
        self.assertTrue(ok)
        argv = run.calls[0][0]
        self.assertEqual(argv[1:3], ["-m", "pip"])
        self.assertIn("requests==2.32.0", argv)
        self.assertEqual(argv[argv.index("--target") + 1], str(self.state / "capabilities" / "python"))
        entry = cap.load_manifest(self.state)[0]
        self.assertEqual((entry["kind"], entry["name"], entry["reason"]), ("pip", "requests", "打 API"))

    def test_a_failed_install_is_not_recorded(self) -> None:
        ok, text, _ = cap.run_install(
            cap.validate_install({"kind": "npm", "name": "left-pad", "reason": "r"}), self.state, runner=FakeRun(1, "boom")
        )
        self.assertFalse(ok)
        self.assertIn("boom", text)
        self.assertEqual(cap.load_manifest(self.state), [])

    def test_a_sudo_shim_is_first_on_the_path_of_everything_it_runs(self) -> None:
        run = FakeRun()
        cap.run_install(cap.validate_install({"kind": "brew", "name": "ffmpeg", "reason": "r"}), self.state, runner=run)
        first = run.calls[0][1]["env"]["PATH"].split(":")[0]
        self.assertTrue(first.startswith(tempfile.gettempdir()) or "harness-shim" in first)
        # the shim itself refuses
        shim = cap._sudo_shim_dir()
        try:
            done = subprocess.run([str(shim / "sudo"), "ls"], capture_output=True, text=True)
            self.assertEqual(done.returncode, 1)
            self.assertTrue((shim / "sudo").stat().st_mode & stat.S_IXUSR)
        finally:
            import shutil

            shutil.rmtree(shim, ignore_errors=True)

    def test_a_model_file_is_kept_only_when_its_hash_matches(self) -> None:
        data = b"model-bytes"
        sha = hashlib.sha256(data).hexdigest()

        def download(url: str, dest: Path, limit: int) -> str:
            dest.write_bytes(data)
            return hashlib.sha256(dest.read_bytes()).hexdigest()

        payload = cap.validate_install({"kind": "model_file", "url": "https://x/m.pt", "filename": "m.pt", "sha256": sha, "reason": "r"}, )
        ok, _t, _m = cap.run_install(payload, self.state, download=download)
        self.assertTrue(ok)
        self.assertEqual((self.state / "capabilities" / "models" / "m.pt").read_bytes(), data)
        bad = {**payload, "filename": "n.pt", "sha256": SHA}
        ok, text, _m = cap.run_install(bad, self.state, download=download)
        self.assertFalse(ok)
        self.assertIn("SHA-256 不符", text)
        self.assertFalse((self.state / "capabilities" / "models" / "n.pt").exists())
        self.assertFalse((self.state / "capabilities" / "models" / "n.pt.part").exists())

    def test_a_script_with_sudo_is_not_run_and_a_clean_one_is_hashed_and_run(self) -> None:
        run = FakeRun()

        def serve(body: str):
            def download(url: str, dest: Path, limit: int) -> str:
                dest.write_text(body)
                return hashlib.sha256(body.encode()).hexdigest()

            return download

        payload = cap.validate_install({"kind": "script", "url": "https://x/i.sh", "reason": "r"})
        ok, text, _ = cap.run_install(payload, self.state, runner=run, download=serve("echo hi\nsudo cp a /usr/bin\n"))
        self.assertFalse(ok)
        self.assertIn("sudo", text)
        self.assertEqual(run.calls, [])
        ok, _t, meta = cap.run_install(payload, self.state, runner=run, download=serve("echo hi\n"))
        self.assertTrue(ok)
        self.assertEqual(run.calls[0][0][0], "sh")
        self.assertEqual(meta["sha256"], hashlib.sha256(b"echo hi\n").hexdigest())

    def test_git_will_not_overwrite_an_existing_checkout_and_a_skill_is_saved_for_the_chat(self) -> None:
        run = FakeRun()
        (self.state / "capabilities" / "src" / "r").mkdir(parents=True)
        ok, text, _ = cap.run_install(
            cap.validate_install({"kind": "git", "url": "https://github.com/o/r.git", "name": "r", "reason": "r"}), self.state, runner=run
        )
        self.assertFalse(ok)
        self.assertEqual(run.calls, [])
        ok, _t, _m = cap.run_install(
            cap.validate_install({"kind": "skill", "name": "triage", "content": "---\ndescription: 分類工單\n---\n# Triage", "reason": "r"}),
            self.state,
        )
        self.assertTrue(ok)
        [(name, desc, path)] = cap.skill_entries(self.state)
        self.assertEqual((name, desc), ("triage", "分類工單"))
        notice = cap.prompt_notice(self.state)
        self.assertIn("triage", notice)
        self.assertIn("request_install", notice)

    def test_installed_things_are_put_on_the_path_of_jobs(self) -> None:
        env = cap.capabilities_env({"PATH": "/usr/bin"}, self.state)
        self.assertIn(str(self.state / "capabilities" / "node" / "node_modules" / ".bin"), env["PATH"])
        self.assertTrue(env["PATH"].endswith("/usr/bin"))


if __name__ == "__main__":
    unittest.main()
