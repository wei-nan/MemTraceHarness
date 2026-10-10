from __future__ import annotations

from unittest import TestCase

from memtrace_harness.github_actions import ACTIONS, build_argv, detail_lines, validate_action

ALLOWED = ["wei-nan/Beri", "wei-nan/beri-android"]


class GithubActionTests(TestCase):
    def test_every_action_builds_a_gh_argument_list(self) -> None:
        cases = {
            "issue_create": ({"repo": "wei-nan/Beri", "title": "T", "body": "B", "labels": ["a", "b"]},
                             ["gh", "issue", "create", "--repo", "wei-nan/Beri", "--title", "T", "--body", "B",
                              "--label", "a", "--label", "b"]),
            "issue_close": ({"repo": "wei-nan/Beri", "number": "#5"},
                            ["gh", "issue", "close", "5", "--repo", "wei-nan/Beri"]),
            "issue_reopen": ({"repo": "wei-nan/Beri", "number": 5, "comment": "again"},
                             ["gh", "issue", "reopen", "5", "--repo", "wei-nan/Beri", "--comment", "again"]),
            "issue_comment": ({"repo": "wei-nan/Beri", "number": 5, "comment": "hi"},
                              ["gh", "issue", "comment", "5", "--repo", "wei-nan/Beri", "--body", "hi"]),
            "issue_label": ({"repo": "wei-nan/Beri", "number": 5, "labels": ["x"], "remove_labels": ["y"]},
                            ["gh", "issue", "edit", "5", "--repo", "wei-nan/Beri", "--add-label", "x", "--remove-label", "y"]),
            "pr_comment": ({"repo": "wei-nan/Beri", "number": 7, "comment": "lgtm"},
                           ["gh", "pr", "comment", "7", "--repo", "wei-nan/Beri", "--body", "lgtm"]),
        }
        self.assertEqual(set(cases), set(ACTIONS))
        for name, (args, argv) in cases.items():
            with self.subTest(action=name):
                self.assertEqual(build_argv(validate_action(ALLOWED, name, args)), argv)

    def test_unsafe_or_out_of_scope_requests_are_refused(self) -> None:
        for action, args in [
            ("pr_merge", {"repo": "wei-nan/Beri", "number": 1}),
            ("issue_close", {"repo": "someone/else", "number": 1}),
            ("issue_close", {"repo": "wei-nan/Beri", "number": 0}),
            ("issue_close", {"repo": "wei-nan/Beri", "number": "abc"}),
            ("issue_comment", {"repo": "wei-nan/Beri", "number": 1, "comment": ""}),
            ("issue_comment", {"repo": "wei-nan/Beri", "number": 1, "comment": "x" * 4001}),
            ("issue_create", {"repo": "wei-nan/Beri", "title": "a\nb", "body": "B"}),
            ("issue_create", {"repo": "wei-nan/Beri", "title": "x" * 201, "body": "B"}),
            ("issue_create", {"repo": "wei-nan/Beri", "title": "T", "body": ""}),
            ("issue_create", {"repo": "wei-nan/Beri", "title": "T", "body": "B", "labels": ["a,b"]}),
            ("issue_create", {"repo": "wei-nan/Beri", "title": "T", "body": "B", "labels": [str(i) for i in range(11)]}),
            ("issue_label", {"repo": "wei-nan/Beri", "number": 1}),
        ]:
            with self.subTest(action=action, args=args), self.assertRaises(ValueError):
                validate_action(ALLOWED, action, args)
        with self.assertRaises(ValueError):
            validate_action([], "issue_close", {"repo": "wei-nan/Beri", "number": 1})

    def test_repo_is_matched_case_insensitively_and_normalized(self) -> None:
        payload = validate_action(ALLOWED, "issue_close", {"repo": "WEI-NAN/BERI-ANDROID", "number": 3})
        self.assertEqual(payload["repo"], "wei-nan/beri-android")

    def test_details_show_every_free_text_field(self) -> None:
        payload = validate_action(ALLOWED, "issue_create", {"repo": "wei-nan/Beri", "title": "T", "body": "B", "labels": ["x"]})
        self.assertEqual(detail_lines(payload), ["標題：T", "加上標籤：x", "內文：B"])
