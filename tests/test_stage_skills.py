from __future__ import annotations

from unittest import TestCase

from memtrace_harness import loop, stage_skills
from memtrace_harness.output_contracts import PROFILE_SCHEMAS
from memtrace_harness.role_profiles import REQUIRED_PROFILES


class StageSkillTests(TestCase):
    def test_every_packaged_skill_loads_and_maps_to_a_profile_and_schema(self) -> None:
        skills = stage_skills.load_skills()
        self.assertEqual(set(skills), set(stage_skills.SKILL_NAMES))
        for name, item in skills.items():
            self.assertIn(item.profile_id, REQUIRED_PROFILES, name)
            self.assertEqual(PROFILE_SCHEMAS[item.profile_id], item.schema, name)
        self.assertEqual(skills["develop"].permission, "workspace-write")
        self.assertEqual(skills["plan"].permission, "read-only")
        self.assertEqual(skills["review"].permission, "read-only")

    def test_only_develop_may_write(self) -> None:
        writers = [n for n, s in stage_skills.load_skills().items() if s.permission == "workspace-write"]
        self.assertEqual(writers, ["develop"])

    def test_paragraphs_are_reflowed_and_blank_lines_become_newlines(self) -> None:
        item = stage_skills.parse_skill(
            "---\nname: x\ndescription: d\nprofile: p\nschema: s.json\npermission: read-only\n---\n"
            "body line\n\n## a\n\nfirst line\nsecond line\n\nnew paragraph\n"
        )
        self.assertEqual(item.section("a"), "first line second line\nnew paragraph")
        self.assertEqual(item.body, "body line")

    def test_malformed_skill_files_are_refused(self) -> None:
        with self.assertRaisesRegex(ValueError, "front matter"):
            stage_skills.parse_skill("no front matter")
        with self.assertRaisesRegex(ValueError, "missing"):
            stage_skills.parse_skill("---\nname: x\n---\n")
        with self.assertRaisesRegex(ValueError, "twice"):
            stage_skills.parse_skill(
                "---\nname: x\ndescription: d\nprofile: p\nschema: s\npermission: read-only\n---\n## a\n\nx\n## a\n\ny\n"
            )

    def test_prompt_policy_comes_from_the_skill_files_and_keeps_its_load_bearing_wording(self) -> None:
        self.assertIn("PASS BY DEFAULT", loop._GATE_PASS_POLICY)
        self.assertIn(loop.TIER_REQUIRED, loop._TIER_RULE)
        self.assertIn(loop.TIER_OPTIONAL, loop._TIER_RULE)
        self.assertIn(loop.DEFER_SEVERITY, loop._G1_TIER_POLICY)
        self.assertTrue(loop._G1_SCOPE.startswith("\n"))
        self.assertTrue(loop._TIER_RULE.endswith("\n"))

    def test_catalog_lists_every_skill_for_the_controller(self) -> None:
        text = stage_skills.catalog()
        for name in stage_skills.SKILL_NAMES:
            self.assertIn(f"- {name} (", text)
