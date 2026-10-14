from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from memtrace_harness.role_profiles import load_role_profiles


class RoleProfileTests(TestCase):
    def test_default_profiles_match_the_accepted_role_policy(self) -> None:
        profiles = load_role_profiles()

        self.assertEqual(profiles["controller"].model, "gpt-5.6-luna")
        self.assertEqual(profiles["plan"].model, "sonnet")
        self.assertEqual(profiles["plan-escalation"].model, "opus")
        self.assertEqual(profiles["review"].model, "gpt-5.6-sol")
        self.assertTrue(profiles["develop"].model.startswith("gemini-"))
        self.assertEqual(
            [(item.provider, item.model) for item in profiles["controller"].fallbacks],
            [
                ("claude", "sonnet"),
                ("antigravity", "gemini-3.8-flash-high"),
            ],
        )
        self.assertEqual(profiles["review"].fallbacks, ())
        self.assertEqual(profiles["plan-escalation"].fallbacks, ())
        self.assertEqual(
            [
                profile.profile_id
                for profile in profiles.values()
                if profile.permission == "workspace-write"
            ],
            ["develop"],
        )

    def test_only_develop_has_a_longer_timeout_and_only_the_controller_a_shorter_one(self) -> None:
        profiles = load_role_profiles()

        self.assertEqual(profiles["develop"].timeout_seconds, 3600)
        self.assertEqual(profiles["controller"].timeout_seconds, 180)
        self.assertEqual(
            sorted(p.profile_id for p in profiles.values() if p.timeout_seconds),
            ["controller", "develop"],
        )

    def test_custom_profile_cannot_grant_controller_write_access(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "profiles.toml"
            original = (
                Path(__file__).parents[1]
                / "src"
                / "memtrace_harness"
                / "default-role-profiles.toml"
            )
            text = original.read_text(encoding="utf-8").replace(
                'permission = "read-only"',
                'permission = "workspace-write"',
                1,
            )
            path.write_text(text, encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "controller"):
                load_role_profiles(path)

    def test_custom_profile_can_point_red_team_at_developers_own_vendor(self) -> None:
        # Red Team's vendor/model are project-configurable — the "independent reviewer"
        # guarantee comes from each role stage being a fresh, separate CLI invocation,
        # not from a forced cross-vendor split. A project may reuse Developer's own
        # provider/model for Red Team.
        with TemporaryDirectory() as directory:
            path = Path(directory) / "profiles.toml"
            original = (
                Path(__file__).parents[1]
                / "src"
                / "memtrace_harness"
                / "default-role-profiles.toml"
            )
            text = original.read_text(encoding="utf-8").replace(
                '[profiles.review]\nrole = "Review"\nprovider = "codex"\nmodel = "gpt-5.6-sol"',
                '[profiles.review]\nrole = "Review"\nprovider = "antigravity"\n'
                'model = "gemini-3.8-flash-high"',
                1,
            )
            path.write_text(text, encoding="utf-8")

            profiles = load_role_profiles(path)

        self.assertEqual(profiles["review"].provider, "antigravity")
        self.assertEqual(profiles["review"].model, "gemini-3.8-flash-high")
        self.assertEqual(profiles["review"].permission, "read-only")

    def test_custom_profile_cannot_grant_red_team_write_access(self) -> None:
        # The vendor/model relaxation must not have loosened the write-access guarantee.
        with TemporaryDirectory() as directory:
            path = Path(directory) / "profiles.toml"
            original = (
                Path(__file__).parents[1]
                / "src"
                / "memtrace_harness"
                / "default-role-profiles.toml"
            )
            text = original.read_text(encoding="utf-8").replace(
                '[profiles.review]\nrole = "Review"\nprovider = "codex"\nmodel = "gpt-5.6-sol"\n'
                'reasoning_effort = "high"\npermission = "read-only"',
                '[profiles.review]\nrole = "Review"\nprovider = "codex"\nmodel = "gpt-5.6-sol"\n'
                'reasoning_effort = "high"\npermission = "workspace-write"',
                1,
            )
            path.write_text(text, encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "review"):
                load_role_profiles(path)

    def test_custom_profile_can_reassign_controller_and_developer_vendor(self) -> None:
        # Every role's provider/model is project-configurable, including Controller
        # (the dispatcher) and Developer (the only writer). What must survive the swap
        # is the permission/context_policy shape, not any particular vendor.
        with TemporaryDirectory() as directory:
            path = Path(directory) / "profiles.toml"
            original = (
                Path(__file__).parents[1]
                / "src"
                / "memtrace_harness"
                / "default-role-profiles.toml"
            )
            text = (
                original.read_text(encoding="utf-8")
                .replace(
                    '[profiles.controller]\nrole = "Controller"\nprovider = "codex"\nmodel = "gpt-5.6-luna"',
                    '[profiles.controller]\nrole = "Controller"\nprovider = "antigravity"\n'
                    'model = "gemini-3.8-flash-high"',
                    1,
                )
                .replace(
                    '[profiles.develop]\nrole = "Develop"\nprovider = "antigravity"\n'
                    'model = "gemini-3.8-flash-high"',
                    '[profiles.develop]\nrole = "Develop"\nprovider = "claude"\n'
                    'model = "sonnet"',
                    1,
                )
            )
            path.write_text(text, encoding="utf-8")

            profiles = load_role_profiles(path)

        self.assertEqual(profiles["controller"].provider, "antigravity")
        self.assertEqual(profiles["develop"].provider, "claude")
        self.assertEqual(profiles["develop"].permission, "workspace-write")
        self.assertEqual(
            [
                profile.profile_id
                for profile in profiles.values()
                if profile.permission == "workspace-write"
            ],
            ["develop"],
        )

    def test_custom_profile_can_give_red_team_and_planner_escalation_a_fallback(self) -> None:
        # Previously hard-blocked ("must fail closed"). Relaxed once fallback usage
        # became visible in the completion summary — no longer a silent downgrade.
        with TemporaryDirectory() as directory:
            path = Path(directory) / "profiles.toml"
            original = (
                Path(__file__).parents[1]
                / "src"
                / "memtrace_harness"
                / "default-role-profiles.toml"
            )
            # [profiles.review] is redefined below — tomllib rejects duplicate
            # tables, so strip the original block first rather than appending.
            import re

            original_text = original.read_text(encoding="utf-8")
            stripped = re.sub(
                r'\[profiles\.review\]\n(?:(?!\[profiles\.).)*', "", original_text, count=1, flags=re.DOTALL
            )
            text = stripped + (
                '[profiles.review]\nrole = "Review"\nprovider = "antigravity"\n'
                'model = "claude-sonnet-4-6"\nreasoning_effort = "high"\n'
                'permission = "read-only"\ncontext_policy = "gate-evidence-only"\n'
                'max_attempts = 1\nquota_bucket = "antigravity-account"\n'
                'fallback_on = ["quota_exhausted", "rate_limit", "provider_overloaded"]\n'
                'max_fallback_hops = 1\n\n'
                '[[profiles.review.fallbacks]]\nprovider = "antigravity"\n'
                'model = "gemini-3.8-flash-high"\nreasoning_effort = "high"\n'
                'quota_bucket = "antigravity-account"\n'
            )
            path.write_text(text, encoding="utf-8")

            profiles = load_role_profiles(path)

        self.assertEqual(profiles["review"].provider, "antigravity")
        self.assertEqual(len(profiles["review"].fallbacks), 1)
        self.assertEqual(profiles["review"].fallbacks[0].model, "gemini-3.8-flash-high")

    def test_pre_skill_role_names_still_load_as_skills(self) -> None:
        import re

        legacy = (
            Path(__file__).parents[1] / "src" / "memtrace_harness" / "default-role-profiles.toml"
        ).read_text(encoding="utf-8")
        for skill, old in (
            ("plan-escalation", "planner-escalation"),
            ("plan", "planner"),
            ("review", "red-team"),
            ("develop", "developer"),
        ):
            legacy = re.sub(rf"profiles\.{skill}(?![\w-])", f"profiles.{old}", legacy)
        with TemporaryDirectory() as directory:
            path = Path(directory) / "profiles.toml"
            path.write_text(legacy, encoding="utf-8")
            profiles = load_role_profiles(path)

        self.assertEqual(set(profiles), {"controller", "plan", "plan-escalation", "review", "develop"})
        self.assertEqual(profiles["develop"].permission, "workspace-write")

    def test_a_profile_spelled_under_both_names_is_refused(self) -> None:
        text = (
            Path(__file__).parents[1] / "src" / "memtrace_harness" / "default-role-profiles.toml"
        ).read_text(encoding="utf-8")
        text += (
            '\n[profiles.developer]\nrole = "Develop"\nprovider = "claude"\nmodel = "sonnet"\n'
            'reasoning_effort = "high"\npermission = "workspace-write"\n'
            'context_policy = "accepted-plan-and-repo"\n'
        )
        with TemporaryDirectory() as directory:
            path = Path(directory) / "profiles.toml"
            path.write_text(text, encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "defined twice"):
                load_role_profiles(path)

    def test_plan_skill_escalation_subtable_becomes_the_escalation_profile(self) -> None:
        import re

        text = (
            Path(__file__).parents[1] / "src" / "memtrace_harness" / "default-role-profiles.toml"
        ).read_text(encoding="utf-8")
        text = re.sub(r"\[profiles\.plan-escalation\]\n(?:(?!\n\[).)*\n?", "", text, count=1, flags=re.DOTALL)
        text = text.replace(
            "[profiles.review]",
            '[profiles.plan.escalation]\nprovider = "claude"\nmodel = "opus"\nreasoning_effort = "high"\n\n[profiles.review]',
            1,
        )
        with TemporaryDirectory() as directory:
            path = Path(directory) / "profiles.toml"
            path.write_text(text, encoding="utf-8")
            profiles = load_role_profiles(path)

        escalation = profiles["plan-escalation"]
        self.assertEqual((escalation.provider, escalation.model), ("claude", "opus"))
        self.assertEqual(escalation.permission, "read-only")
        self.assertEqual(escalation.context_policy, "task-and-targeted-evidence")

    def test_custom_profile_can_pin_a_full_sonnet_model_id(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "profiles.toml"
            original = (
                Path(__file__).parents[1]
                / "src"
                / "memtrace_harness"
                / "default-role-profiles.toml"
            )
            text = original.read_text(encoding="utf-8").replace(
                '[profiles.plan]\nrole = "Plan"\nprovider = "claude"\nmodel = "sonnet"',
                '[profiles.plan]\nrole = "Plan"\nprovider = "claude"\n'
                'model = "claude-sonnet-pinned"',
                1,
            )
            path.write_text(text, encoding="utf-8")

            profiles = load_role_profiles(path)

        self.assertEqual(profiles["plan"].model, "claude-sonnet-pinned")


class OutputSchemaStrictModeTests(TestCase):
    def test_every_object_lists_all_of_its_properties_as_required(self) -> None:
        # OpenAI structured outputs (used by `codex exec --output-schema`) reject any
        # object whose `properties` aren't all in `required` (2026-09-29: Red Team's
        # gate.json failed every codex call with invalid_json_schema). Optional-ness is
        # expressed as an empty string, never by omitting the key.
        import json

        from memtrace_harness.output_contracts import PROFILE_SCHEMAS, output_schema_path

        def check(node, path, filename):
            if isinstance(node, dict):
                if "properties" in node:
                    missing = set(node["properties"]) - set(node.get("required", []))
                    self.assertFalse(missing, f"{filename} {path}: not required: {sorted(missing)}")
                for key, value in node.items():
                    check(value, f"{path}/{key}", filename)
            elif isinstance(node, list):
                for index, value in enumerate(node):
                    check(value, f"{path}[{index}]", filename)

        for profile_id in PROFILE_SCHEMAS:
            path = output_schema_path(profile_id)
            check(json.loads(path.read_text(encoding="utf-8")), "", path.name)
