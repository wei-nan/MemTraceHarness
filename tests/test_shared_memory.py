from dataclasses import replace
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from memtrace_harness.shared_memory import SharedMemoryStore, shared_status
from memtrace_harness.loop import gate_task
from memtrace_harness.schemas import ContextItem, TaskEnvelope
from memtrace_harness.runtime import build_harness
from memtrace_harness.scope import ProjectScope
from tests.workspace_support import workspace


class SharedMemoryTests(TestCase):
    def test_two_harnesses_see_live_crud_with_revision_conflicts_and_audit(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / 'shared.sqlite3'
            a, b = SharedMemoryStore(path), SharedMemoryStore(path)
            self.assertEqual(a.list(), [])
            entry = a.change(dict(action='create', title='交付', body='使用 worktree', policy='worktree-pr-ci-main'))
            self.assertIn('使用 worktree', b.context())
            self.assertTrue(b.delivery_enabled())
            updated = b.change(dict(action='update', id=entry['id'], revision=1, title='交付', body='測試後送 PR'))
            with self.assertRaisesRegex(ValueError, 'refresh'):
                a.change(dict(action='toggle', id=entry['id'], revision=1, enabled=False))
            disabled = a.change(dict(action='toggle', id=entry['id'], revision=updated['revision'], enabled=False))
            self.assertEqual(b.context(), '')
            self.assertFalse(b.delivery_enabled())
            b.change(dict(action='remove', id=entry['id'], revision=disabled['revision']))
            self.assertEqual(a.list(), [])
            with sqlite3.connect(path) as conn:
                self.assertEqual(conn.execute('SELECT count(*) FROM revisions').fetchone()[0], 4)

    def test_invalid_values_are_rejected_and_corruption_does_not_break_status(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / 'shared.sqlite3'
            store = SharedMemoryStore(path)
            for request in [dict(action='create', title='', body='x'), dict(action='create', title='x', body='x', policy='arbitrary'), dict(action='toggle', id={}, revision=1)]:
                with self.assertRaises(ValueError):
                    store.change(request)
            path.write_text('broken')
            config = type('Config', (), {'shared_memory_path': path})()
            self.assertFalse(shared_status(config)['available'])
            with self.assertRaises(sqlite3.Error):
                store.delivery_enabled()

    def test_shared_memory_reaches_chat_and_every_skill_but_private_chat_does_not_reach_review(self):
        with workspace() as (root, config):
            store = SharedMemoryStore(config.shared_memory_path)
            store.change(dict(action='create', title='共用指示', body='這是共用規則'))
            scope = ProjectScope.from_file(root / 'harness-scope.md')
            service = build_harness(config, scope).service
            self.assertIn('這是共用規則', service._identity_context(scope))
            items = service._project_context_items(scope)
            task = TaskEnvelope('t', scope.workspace_id, 'task', context_items=[*items, ContextItem('private:chat', body='private conversation')])
            review = gate_task(task, 'G2', {})
            self.assertTrue(any('這是共用規則' in (item.body or '') for item in review.context_items))
            self.assertFalse(any('private conversation' in (item.body or '') for item in review.context_items))

    def test_enabled_workflow_requires_worktree_even_with_one_worker_but_non_git_can_operate(self):
        from tests.test_worktrees import make_repo
        with workspace() as (root, config):
            store = SharedMemoryStore(config.shared_memory_path)
            store.change(dict(action='create', title='交付', body='使用 worktree', policy='worktree-pr-ci-main'))
            scope = ProjectScope.from_file(root / 'harness-scope.md')
            service = build_harness(config, scope).service
            directory, no_tree = service._provision_workdir(scope, 'ops')
            self.assertEqual(directory, scope.working_directory)
            self.assertIsNone(no_tree)
            repo = make_repo(root)
            scope = replace(scope, working_directory=repo, max_workers=1)
            path, worktree = service._provision_workdir(scope, 'c')
            self.assertIsNotNone(worktree)
            self.assertNotEqual(path, repo)
