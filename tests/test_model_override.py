"""A model chosen for one task only: `model=<model>::` in front of a task goal."""

from unittest import TestCase
from unittest.mock import MagicMock

from memtrace_harness.loop import AgentLoopRunner
from memtrace_harness.model_override import resolve_model_override, with_developer_override
from memtrace_harness.role_profiles import load_role_profiles
from memtrace_harness.scope import split_goal_directives, split_repo_goal


class GoalPrefixTests(TestCase):
    def test_model_and_repo_prefixes_come_off_in_either_order(self) -> None:
        self.assertEqual(
            split_goal_directives("model=claude-opus-5-5::repo=android::設計頁面"),
            ("android", "claude-opus-5-5", "設計頁面"),
        )
        self.assertEqual(
            split_goal_directives("repo=android::model=claude/opus::設計頁面"),
            ("android", "claude/opus", "設計頁面"),
        )
        self.assertEqual(split_goal_directives("設計頁面"), (None, None, "設計頁面"))
        self.assertEqual(split_repo_goal("model=opus::設計頁面"), (None, "設計頁面"))


class OverrideTests(TestCase):
    def setUp(self) -> None:
        self.profiles = load_role_profiles()
        self.developer = self.profiles["develop"]
        self.config = MagicMock(workspace_root=None)

    def test_provider_is_inferred_or_given(self) -> None:
        for spec, provider, model in [
            ("claude-opus-5-5", "claude", "claude-opus-5-5"),
            ("opus", "claude", "opus"),
            ("gpt-6-sol", "codex", "gpt-6-sol"),
            ("antigravity/some-model", "antigravity", "some-model"),
        ]:
            candidate = resolve_model_override(spec, self.developer, self.config)
            self.assertEqual((candidate.provider, candidate.model), (provider, model))
        with self.assertRaises(ValueError):
            resolve_model_override("nope/x", self.developer, self.config)

    def test_developer_tries_the_chosen_model_then_its_usual_chain(self) -> None:
        candidate = resolve_model_override("claude-opus-5-5", self.developer, self.config)
        profile = with_developer_override(self.profiles, candidate)["develop"]
        models = [(c.provider, c.model) for c in profile.candidates]
        self.assertEqual(models[0], ("claude", "claude-opus-5-5"))
        self.assertEqual(models[1], (self.developer.provider, self.developer.model))
        self.assertEqual(len(models), 1 + len(self.developer.candidates))
        self.assertEqual(profile.permission, "workspace-write")
        # the other roles and the stored defaults are untouched
        self.assertEqual(self.profiles["develop"], self.developer)

    def test_any_failure_of_the_chosen_model_falls_back_but_the_default_keeps_its_rules(self) -> None:
        runner = AgentLoopRunner.__new__(AgentLoopRunner)
        runner.role_profiles = self.profiles
        runner.max_total_tokens = None
        runner.model_override_profiles = frozenset({"develop"})
        candidates = [object(), object(), object()]
        kwargs = dict(profile_id="develop", candidates=candidates, stages=[])
        self.assertTrue(runner._may_fallback(category="unknown", attempt_index=0, **kwargs))
        self.assertFalse(runner._may_fallback(category="unknown", attempt_index=1, **kwargs))
        runner.model_override_profiles = frozenset()
        self.assertFalse(runner._may_fallback(category="unknown", attempt_index=0, **kwargs))


class PermanentChangeTests(TestCase):
    def test_validation_infers_provider_and_refuses_unknown_targets(self) -> None:
        from memtrace_harness.model_changes import validate_model_change

        self.assertEqual(
            validate_model_change({"target": "develop", "model": "claude-opus-5-5"}),
            {"target": "develop", "provider": "claude", "model": "claude-opus-5-5"},
        )
        self.assertEqual(validate_model_change({"target": "chat", "model": "codex/x-1"})["provider"], "codex")
        for bad in ({"target": "nope", "model": "opus"}, {"target": "chat", "model": "mystery"},
                    {"target": "chat", "model": 'a"b', "provider": "claude"}):
            with self.assertRaises(ValueError):
                validate_model_change(bad)

    def test_confirmed_change_rewrites_only_the_primary_model_and_a_bad_one_is_rolled_back(self) -> None:
        import shutil
        from pathlib import Path
        from tempfile import TemporaryDirectory

        from memtrace_harness.model_changes import apply_model_change

        with TemporaryDirectory() as tmp:
            roles = Path(tmp) / "roles.toml"
            shutil.copy(Path(__file__).parents[1] / "src/memtrace_harness/default-role-profiles.toml", roles)
            config = MagicMock(workspace_root=Path(tmp))
            config.role_profiles_file_for.return_value = roles
            before = load_role_profiles(roles)["develop"]
            apply_model_change(config, {"target": "develop", "provider": "claude", "model": "claude-opus-5-5"})
            after = load_role_profiles(roles)["develop"]
            self.assertEqual((after.provider, after.model), ("claude", "claude-opus-5-5"))
            self.assertEqual(after.fallbacks, before.fallbacks)
            self.assertEqual(after.permission, "workspace-write")
            # an effort the new provider does not accept: nothing changes
            head, dev = roles.read_text(encoding="utf-8").split("[profiles.develop]")
            text = head + "[profiles.develop]" + dev.replace('reasoning_effort = "high"', 'reasoning_effort = "max"', 1)
            roles.write_text(text, encoding="utf-8")
            snapshot = roles.read_text(encoding="utf-8")
            with self.assertRaises(ValueError):
                apply_model_change(config, {"target": "develop", "provider": "codex", "model": "gpt-6-sol"})
            self.assertEqual(roles.read_text(encoding="utf-8"), snapshot)
