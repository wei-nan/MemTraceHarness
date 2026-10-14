from __future__ import annotations

from unittest import TestCase

from memtrace_harness import loop, stage_skills
from memtrace_harness.output_contracts import PROFILE_SCHEMAS
from memtrace_harness.role_profiles import REQUIRED_PROFILES


class StageSkillTests(TestCase):
    def test_every_packaged_skill_loads_and_maps_to_a_profile_and_schema(self) -> None:
        skills = {n: s for n, s in stage_skills.load_skills().items() if s.kind == "stage"}
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

    def test_domain_skill_holds_no_profile_or_permission(self) -> None:
        item = stage_skills.parse_skill("---\nname: d\ndescription: x\nkind: domain\n---\n## a\n\ntext\n")
        self.assertEqual((item.kind, item.profile_id, item.permission), ("domain", "", ""))
        self.assertEqual(item.section("a"), "text")
        for extra in ("permission: workspace-write", "profile: develop", "schema: s.json", "modes: g1"):
            with self.assertRaisesRegex(ValueError, "holds no profile or permission"):
                stage_skills.parse_skill(f"---\nname: d\ndescription: x\nkind: domain\n{extra}\n---\n")
        with self.assertRaisesRegex(ValueError, "invalid kind"):
            stage_skills.parse_skill("---\nname: d\ndescription: x\nkind: other\n---\n")

    def test_domain_skills_are_not_stages(self) -> None:
        stages = [n for n, s in stage_skills.load_skills().items() if s.kind == "stage"]
        self.assertEqual(set(stages), set(stage_skills.SKILL_NAMES))
        for name, item in stage_skills.load_skills().items():
            if item.kind == "domain":
                self.assertNotIn(f"- {name} (", stage_skills.catalog())
                self.assertNotIn(name, stage_skills.PRECONDITIONS)
                with self.assertRaises(KeyError):
                    stage_skills.domain_section("plan", "g1_scope")

    def test_catalog_lists_every_skill_for_the_controller(self) -> None:
        text = stage_skills.catalog()
        for name in stage_skills.SKILL_NAMES:
            self.assertIn(f"- {name} (", text)
