from dataclasses import replace
import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from unittest import TestCase

from memtrace_harness.delivery import DeliveryRunner, DeliveryStore, new_job, restore_summary, task_issues
from memtrace_harness.schemas import TaskEnvelope, LoopSummary, TokenUsage
from memtrace_harness.scope import ProjectScope
from memtrace_harness.trace_store import TraceStore
from memtrace_harness.worktrees import RepoTaskWorkspace, TaskWorktree


class FakeRemote:
    def __init__(self, rows):
        self.calls = []
        self.states = {row['github_repo']: 'OPEN' for row in rows}
        self.checks = {row['github_repo']: 'SUCCESS' for row in rows}
        self.heads = {row['github_repo']: 'abc123' for row in rows}
        self.issues = {}
        self.on_call = None
        self.dirty = False
        self.merge_failure = None
        self.pr_exists = True
        self.pr_body = None

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        if self.on_call:
            self.on_call(argv)
        repo = argv[-1] if argv[0] == 'gh' else 'acme/' + Path(kwargs['cwd']).name
        output = ''
        if argv[:3] == ['git', 'symbolic-ref', '--short']:
            output = 'harness/chat_1'
        elif argv[:3] == ['git', 'status', '--porcelain']:
            output = ' M changed.txt' if self.dirty else ''
        elif argv[:3] == ['git', 'rev-parse', 'HEAD']:
            output = self.heads[repo]
        elif argv[:4] == ['git', 'remote', 'get-url', 'origin']:
            output = 'git@github.com:' + repo + '.git'
        elif argv[:3] == ['gh', 'pr', 'list']:
            output = json.dumps([{'number': 12, 'state': self.states[repo], 'headRefOid': self.heads[repo]}] if self.pr_exists else [])
        elif argv[:3] == ['gh', 'pr', 'create']:
            self.pr_body = Path(argv[argv.index('--body-file') + 1]).read_text()
            self.pr_exists = True
        elif argv[:3] == ['gh', 'pr', 'view']:
            conclusion = self.checks[repo]
            output = json.dumps(dict(number=12, url='https://github.com/' + repo + '/pull/12', state=self.states[repo],
                headRefName='harness/chat_1', headRefOid=self.heads[repo], baseRefName='main', baseRefOid='base',
                mergeable='CONFLICTING' if conclusion == 'CONFLICT' else 'MERGEABLE', mergeStateStatus='CLEAN',
                mergedAt='now' if self.states[repo] == 'MERGED' else None,
                statusCheckRollup=[] if conclusion == 'NONE' else [dict(status='COMPLETED', conclusion=conclusion)]))
        elif argv[:3] == ['gh', 'pr', 'merge']:
            if repo == self.merge_failure:
                return subprocess.CompletedProcess(argv, 1, '', '')
            self.states[repo] = 'MERGED'
        elif argv[:3] == ['gh', 'run', 'list']:
            output = '[]'
        elif argv[:3] == ['gh', 'issue', 'view']:
            output = json.dumps({'state': self.issues.get((repo, argv[3]), 'OPEN')})
        elif argv[:3] == ['gh', 'issue', 'close']:
            self.issues[(repo, argv[3])] = 'CLOSED'
        return subprocess.CompletedProcess(argv, 0, output, '')


class DeliveryTests(TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.trace = TraceStore(self.root / 'trace.sqlite3')
        self.store = DeliveryStore(self.trace)
        self.scope = ProjectScope(name='proj', workspace_id='ws', working_directory=self.root / 'main',
                                  scope_file_path=self.root / 'harness-scope.md', github_repo='acme/proj', verify_command='test')
        self.task = TaskEnvelope('task', 'ws', 'fix #143', context_refs=['gh:acme/proj#143'])
        self.trace.create_conversation(self.task, 'chat_1')
        run = self.trace.create_run(self.task, conversation_id='chat_1')
        self.summary = LoopSummary(self.task, [], 'delivering', 'local evidence', run, TokenUsage(), conversation_id='chat_1')
        path = self.root / 'proj'
        path.mkdir()
        tree = TaskWorktree(path, 'harness/chat_1', 'main', 'base')
        self.workspaces = (RepoTaskWorkspace('proj', self.root / 'main', path, 'test', 1200, (), tree),)
        self.job = new_job(self.summary, self.scope, self.workspaces)
        self.remote = FakeRemote(self.job['repos'])
        self.notes = []

    def runner(self, **kwargs):
        return DeliveryRunner(self.store, enabled=lambda: True, repair=lambda *_: self.fail('unexpected model call'),
                              notify=self.notes.append, run=self.remote, sleep=lambda _: None, **kwargs)

    def test_actual_merge_then_issue_close_and_cleanup_and_persisted_success(self):
        self.remote.pr_exists = False
        self.summary = replace(self.summary, recommendation='Closes #999')
        self.job['summary'] = self.summary.to_dict()
        result = self.runner().run(self.job)
        self.assertEqual(result.status, 'succeeded')
        self.assertEqual(self.store.get('chat_1')['status'], 'done')
        self.assertEqual(self.trace.conversation_outcome('chat_1')['status'], 'succeeded')
        merge = next(i for i, argv in enumerate(self.remote.calls) if argv[:3] == ['gh', 'pr', 'merge'])
        close = next(i for i, argv in enumerate(self.remote.calls) if argv[:3] == ['gh', 'issue', 'close'])
        cleanup = next(i for i, argv in enumerate(self.remote.calls) if argv[:3] == ['git', 'worktree', 'remove'])
        self.assertLess(merge, close)
        self.assertLess(close, cleanup)
        self.assertIn('--match-head-commit', self.remote.calls[merge])
        self.assertNotIn('Closes', self.remote.pr_body)
        self.assertFalse(any('--force' in argv or '--admin' in argv for argv in self.remote.calls))
        self.assertEqual(result.total_usage.completeness, 'unavailable')
        self.assertEqual(restore_summary(result.to_dict()), result)

    def test_no_ci_never_merges_or_closes_and_times_out(self):
        self.remote.checks['acme/proj'] = 'NONE'
        self.job['wait_started'] = 0
        result = self.runner(now=lambda: 3601).run(self.job)
        self.assertEqual(result.status, 'needs_human')
        self.assertFalse(any(argv[:3] in (['gh', 'pr', 'merge'], ['gh', 'issue', 'close']) for argv in self.remote.calls))

    def test_head_changed_after_ci_never_merges(self):
        def change(argv):
            if argv[:3] == ['gh', 'pr', 'view']:
                self.remote.heads['acme/proj'] = 'someone-else'
        self.remote.on_call = change
        result = self.runner().run(self.job)
        self.assertEqual(result.status, 'needs_human')
        self.assertIn('提交已改變', result.recommendation)

    def test_failure_repairs_same_job_then_pushes_rechecks_and_merges(self):
        self.remote.checks['acme/proj'] = 'FAILURE'
        runner = self.runner()
        repairs = []
        def repair(job, evidence):
            repairs.append(evidence)
            self.assertEqual(job['repos'][0]['path'], str(self.workspaces[0].working_directory))
            self.remote.heads['acme/proj'] = 'fixed'
            self.remote.checks['acme/proj'] = 'SUCCESS'
            return self.summary
        runner.repair = repair
        result = runner.run(self.job)
        self.assertEqual(result.status, 'succeeded')
        self.assertEqual(len(repairs), 1)
        self.assertEqual(len([argv for argv in self.remote.calls if argv[:2] == ['git', 'push']]), 2)
        self.assertEqual(self.job['repos'][0]['head'], 'fixed')

    def test_disabled_policy_pauses_and_restart_rechecks_without_duplicate_push(self):
        enabled = [True]
        def disable(argv):
            if argv[:3] == ['gh', 'pr', 'list']:
                enabled[0] = False
        self.remote.on_call = disable
        runner = self.runner()
        runner.enabled = lambda: enabled[0]
        result = runner.run(self.job)
        self.assertEqual(result.status, 'delivering')
        self.assertEqual(self.store.pending()[0]['conversation_id'], 'chat_1')
        enabled[0] = True
        self.remote.on_call = None
        self.assertEqual(self.runner().run(self.store.get('chat_1')).status, 'succeeded')
        self.assertEqual(len([argv for argv in self.remote.calls if argv[:2] == ['git', 'push']]), 1)

    def test_interrupted_repair_does_not_replay_model_work(self):
        self.job['repos'][0]['repair_running'] = True
        self.remote.checks['acme/proj'] = 'FAILURE'
        result = self.runner().run(self.job)
        self.assertEqual(result.status, 'needs_human')
        self.assertIn('上次修正被中斷', result.recommendation)

    def test_post_verification_edits_are_preserved(self):
        self.remote.dirty = True
        result = self.runner().run(self.job)
        self.assertEqual(result.status, 'needs_human')
        self.assertFalse(any(argv[:2] == ['git', 'push'] for argv in self.remote.calls))

    def test_only_task_issues_are_selected_never_unrelated_history(self):
        task = replace(self.task, goal='參考 #888 修復問題', context_refs=['gh:acme/proj#143', 'gh:other/repo#9'])
        self.assertEqual(task_issues(task, ['acme/proj']), {'acme/proj': [143]})
        self.assertEqual(task_issues(replace(task, context_refs=[]), ['acme/proj']), {'acme/proj': []})
        self.assertEqual(task_issues(replace(task, goal='fix #143; follow #888', context_refs=[]), ['acme/proj']), {'acme/proj': [143]})
        self.assertEqual(task_issues(replace(task, goal='診斷並修復 beri-android #143 單元測試', context_refs=[]), ['acme/proj']), {'acme/proj': [143]})
        self.assertEqual(task_issues(replace(task, goal='修復 bug，參考 #888', context_refs=[]), ['acme/proj']), {'acme/proj': []})

    def _second_repo(self):
        import copy
        row = copy.deepcopy(self.job['repos'][0])
        row.update(name='other', github_repo='acme/other', path=str(self.root / 'other'), issues=[144])
        Path(row['path']).mkdir()
        self.job['repos'].append(row)
        self.remote = FakeRemote(self.job['repos'])

    def test_multiple_repos_are_preflighted_before_merging_and_partial_merge_does_not_close_issues(self):
        self._second_repo()
        self.remote.merge_failure = 'acme/other'
        result = self.runner().run(self.job)
        self.assertEqual(result.status, 'needs_human')
        self.assertEqual(result.delivery['state'], 'partial')
        self.assertTrue(result.delivery['repos'][0]['merged'])
        merge = next(i for i, argv in enumerate(self.remote.calls) if argv[:3] == ['gh', 'pr', 'merge'])
        checked = {argv[-1] for argv in self.remote.calls[:merge] if argv[:3] == ['gh', 'pr', 'view']}
        self.assertEqual(checked, {'acme/proj', 'acme/other'})
        self.assertFalse(any(argv[:3] == ['gh', 'issue', 'close'] for argv in self.remote.calls))

    def test_three_unsuccessful_repairs_stop_without_looping_or_merging(self):
        self.remote.checks['acme/proj'] = 'CONFLICT'
        runner = self.runner()
        repairs = []
        runner.repair = lambda *args: repairs.append(args) or self.summary
        result = runner.run(self.job)
        self.assertEqual(result.status, 'needs_human')
        self.assertEqual(len(repairs), 3)
        self.assertEqual(self.job['repos'][0]['repairs'], 3)
        self.assertFalse(any(argv[:3] == ['gh', 'pr', 'merge'] for argv in self.remote.calls))

    def test_one_failing_repo_prevents_any_merge(self):
        self._second_repo()
        self.remote.checks['acme/other'] = 'NONE'
        self.job['wait_started'] = 0
        result = self.runner(now=lambda: 3601).run(self.job)
        self.assertEqual(result.status, 'needs_human')
        self.assertFalse(any(argv[:3] == ['gh', 'pr', 'merge'] for argv in self.remote.calls))

    def test_remote_merge_before_crash_is_reconciled_without_merging_again(self):
        row = self.job['repos'][0]
        row.update(head='abc123', pr=12, ready=True)
        self.remote.states['acme/proj'] = 'MERGED'
        self.store.save(self.job)
        result = self.runner().run(self.store.get('chat_1'))
        self.assertEqual(result.status, 'succeeded')
        self.assertFalse(any(argv[:3] == ['gh', 'pr', 'merge'] for argv in self.remote.calls))

    def test_skipped_checks_alone_do_not_pass_but_success_with_optional_skip_does(self):
        row = self.job['repos'][0]
        row['head'] = 'abc123'
        view = dict(state='OPEN', headRefName=row['branch'], headRefOid='abc123', baseRefName='main',
                    mergeable='MERGEABLE', mergeStateStatus='CLEAN', statusCheckRollup=[dict(status='COMPLETED', conclusion='SKIPPED')])
        self.assertEqual(DeliveryRunner.check(view, row), 'wait')
        view['statusCheckRollup'].append(dict(status='COMPLETED', conclusion='SUCCESS'))
        self.assertEqual(DeliveryRunner.check(view, row), 'ready')

    def test_service_drainer_resumes_durable_job_once_and_keeps_peer_pending_until_finished(self):
        import threading
        from unittest.mock import patch
        from tests.test_service import _build_schedule_gateway
        from memtrace_harness.shared_memory import SharedMemoryStore
        gateway, trace, scope = _build_schedule_gateway(self.root / 'gateway')
        gateway.config = replace(gateway.config, shared_memory_path=self.root / 'shared.sqlite3')
        SharedMemoryStore(gateway.config.shared_memory_path).change(dict(action='create', title='交付', body='x', policy='worktree-pr-ci-main'))
        summary = replace(self.summary, task=replace(self.task, workspace_id=scope.workspace_id))
        trace.create_conversation(summary.task, summary.conversation_id)
        run = trace.create_run(summary.task, conversation_id=summary.conversation_id)
        summary = replace(summary, trace_id=run)
        job = {**self.job, 'project': scope.name, 'summary': summary.to_dict()}
        delivery_store = DeliveryStore(trace)
        delivery_store.save(job)
        complete = threading.Event()
        def finish(*args):
            job['status'] = 'done'
            delivery_store.save(job)
            complete.set()
            return replace(summary, status='succeeded')
        with patch.object(gateway, '_run_delivery', side_effect=finish) as execute, \
             patch.object(gateway, '_report_run_outcome') as report:
            gateway.drain_deliveries()
            self.assertTrue(complete.wait(3))
            from memtrace_harness.inflight import default_tracker
            self.assertTrue(default_tracker.wait_for_drain(3, poll_interval=.02))
            gateway.drain_deliveries()
            self.assertEqual(execute.call_count, 1)
            self.assertEqual(report.call_count, 1)
        self.assertEqual(trace.list_workspace_locks(scope.workspace_id), [])
