from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from memtrace_harness.role_profiles import load_role_profiles


class RoleProfileTests(TestCase):
    def test_default_profiles_match_the_accepted_role_policy(self) -> None:
        profiles = load_role_profiles()

        self.assertEqual(profiles["controller"].model, "gpt-5.6-luna")
        self.assertEqual(profiles["planner"].model, "sonnet")
        self.assertEqual(profiles["planner-escalation"].model, "opus")
        self.assertEqual(profiles["red-team"].model, "gpt-5.6-sol")
        self.assertTrue(profiles["developer"].model.startswith("gemini-"))
        self.assertEqual(
            [(item.provider, item.model) for item in profiles["controller"].fallbacks],
            [
                ("claude", "sonnet"),
                ("antigravity", "gemini-3.6-flash-high"),
            ],
        )
        self.assertEqual(profiles["red-team"].fallbacks, ())
        self.assertEqual(profiles["planner-escalation"].fallbacks, ())
        self.assertEqual(
            [
                profile.profile_id
                for profile in profiles.values()
                if profile.permission == "workspace-write"
            ],
            ["developer"],
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
                '[profiles.red-team]\nrole = "Red Team"\nprovider = "codex"\nmodel = "gpt-5.6-sol"',
                '[profiles.red-team]\nrole = "Red Team"\nprovider = "antigravity"\n'
                'model = "gemini-3.1-pro-high"',
                1,
            )
            path.write_text(text, encoding="utf-8")

            profiles = load_role_profiles(path)

        self.assertEqual(profiles["red-team"].provider, "antigravity")
        self.assertEqual(profiles["red-team"].model, "gemini-3.1-pro-high")
        self.assertEqual(profiles["red-team"].permission, "read-only")

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
                '[profiles.red-team]\nrole = "Red Team"\nprovider = "codex"\nmodel = "gpt-5.6-sol"\n'
                'reasoning_effort = "high"\npermission = "read-only"',
                '[profiles.red-team]\nrole = "Red Team"\nprovider = "codex"\nmodel = "gpt-5.6-sol"\n'
                'reasoning_effort = "high"\npermission = "workspace-write"',
                1,
            )
            path.write_text(text, encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "red-team"):
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
                    'model = "gemini-3.6-flash-high"',
                    1,
                )
                .replace(
                    '[profiles.developer]\nrole = "Developer"\nprovider = "antigravity"\n'
                    'model = "gemini-3.1-pro-high"',
                    '[profiles.developer]\nrole = "Developer"\nprovider = "claude"\n'
                    'model = "sonnet"',
                    1,
                )
            )
            path.write_text(text, encoding="utf-8")

            profiles = load_role_profiles(path)

        self.assertEqual(profiles["controller"].provider, "antigravity")
        self.assertEqual(profiles["developer"].provider, "claude")
        self.assertEqual(profiles["developer"].permission, "workspace-write")
        self.assertEqual(
            [
                profile.profile_id
                for profile in profiles.values()
                if profile.permission == "workspace-write"
            ],
            ["developer"],
        )

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
                '[profiles.planner]\nrole = "Planner"\nprovider = "claude"\nmodel = "sonnet"',
                '[profiles.planner]\nrole = "Planner"\nprovider = "claude"\n'
                'model = "claude-sonnet-pinned"',
                1,
            )
            path.write_text(text, encoding="utf-8")

            profiles = load_role_profiles(path)

        self.assertEqual(profiles["planner"].model, "claude-sonnet-pinned")
