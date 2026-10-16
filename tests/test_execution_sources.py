from dataclasses import replace
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import MagicMock, patch

from memtrace_harness import admin
from memtrace_harness.adapter_factory import build_provider_adapter
from memtrace_harness.cli_process import CliProcessRunner, ProcessResult
from memtrace_harness.execution_sources import SourceStore, SourceBinding, SourceCommand, SourceUnavailable
from memtrace_harness.primary_agent import PrimaryAgent
from memtrace_harness.role_profiles import ModelCandidate, load_role_profiles
from memtrace_harness.schemas import TaskEnvelope
from memtrace_harness.workspace import models_file, update_model_settings
from tests.workspace_support import workspace


def register(store, source_id='work', provider='codex', bucket=None, maximum=1):
    return store.change(dict(action='save', id=source_id, provider=provider, label=source_id,
        command='/opt/tools/' + source_id, quota_bucket=bucket or source_id, max_concurrency=maximum))


def bind(store, row):
    return SourceBinding(store.path, row['id'], row['provider'], row['command'], row['quota_bucket'], row['revision'])


class SourceStoreTests(TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = SourceStore(Path(self.tmp.name) / 'sources.sqlite3')

    def test_two_harnesses_share_capacity_by_real_quota_pool_and_other_accounts_stay_available(self):
        work = register(self.store, 'work', bucket='same-account', maximum=2)
        alias = register(self.store, 'alias', bucket='same-account', maximum=1)
        personal = register(self.store, 'personal')
        other = SourceStore(self.store.path)
        lease = self.store.acquire(bind(self.store, work), 10)
        with self.assertRaises(SourceUnavailable) as error:
            other.acquire(bind(other, alias), 10)
        self.assertEqual(error.exception.category, 'source_busy')
        second = other.acquire(bind(other, personal), 10)
        other.release(second)
        self.store.release(lease)
        next_lease = other.acquire(bind(other, alias), 10)
        other.release(next_lease)

    def test_quota_cools_same_pool_but_authentication_only_cools_affected_source(self):
        work = register(self.store, 'work', bucket='pool')
        alias = register(self.store, 'alias', bucket='pool')
        self.store.record(bind(self.store, work), 'authentication')
        with self.assertRaises(SourceUnavailable) as error:
            self.store.acquire(bind(self.store, work), 10)
        self.assertEqual(error.exception.category, 'authentication')
        lease = self.store.acquire(bind(self.store, alias), 10)
        self.store.release(lease)
        self.store.record(bind(self.store, work), 'quota_exhausted')
        with self.assertRaises(SourceUnavailable) as error:
            self.store.acquire(bind(self.store, alias), 10)
        self.assertEqual(error.exception.category, 'quota_exhausted')
        self.assertTrue(all(entry['cooldown'] for entry in SourceStore(self.store.path).list()))

    def test_revision_conflicts_disabling_and_command_changes_fail_before_invocation(self):
        work = register(self.store)
        binding = bind(self.store, work)
        disabled = self.store.change(dict(action='toggle', id='work', revision=work['revision'], enabled=False))
        with self.assertRaisesRegex(ValueError, 'refresh'):
            self.store.change(dict(action='toggle', id='work', revision=work['revision'], enabled=True))
        with self.assertRaises(SourceUnavailable):
            self.store.acquire(binding, 10)
        enabled = self.store.change(dict(action='toggle', id='work', revision=disabled['revision'], enabled=True))
        self.store.change(dict(action='save', id='work', revision=enabled['revision'], provider='codex', label='work', command='/opt/new-wrapper'))
        with self.assertRaisesRegex(SourceUnavailable, 'changed before'):
            self.store.acquire(binding, 10)

    def test_provider_is_immutable_and_shell_commands_and_credentials_are_not_configuration(self):
        register(self.store)
        for update in [dict(command='codex --token secret'), dict(command='/bin/codex --token secret'), dict(provider='claude')]:
            data = dict(action='save', id='work', revision=1, provider='codex', label='work', command='/opt/tools/work')
            data.update(update)
            with self.assertRaises(ValueError):
                self.store.change(data)

    def test_expired_lease_retains_orphaned_live_child_but_releases_dead_child(self):
        work = register(self.store)
        lease = self.store.acquire(bind(self.store, work), 1)
        with self.store.connection() as conn:
            conn.execute('UPDATE source_leases SET expires_at=0,child_pid=? WHERE id=?', (os.getpid(), lease))
        with self.assertRaises(SourceUnavailable):
            self.store.acquire(bind(self.store, work), 1)
        with self.store.connection() as conn:
            conn.execute('UPDATE source_leases SET child_pid=NULL WHERE id=?', (lease,))
        replacement = self.store.acquire(bind(self.store, work), 1)
        self.store.release(replacement)


class SourceRoutingTests(TestCase):
    def setUp(self):
        self.ctx = workspace({'HARNESS_CHAT_PROVIDER':'codex', 'HARNESS_CHAT_MODEL':'model-a'})
        self.root, self.config = self.ctx.__enter__()
        self.addCleanup(self.ctx.__exit__, None, None, None)
        self.store = SourceStore(self.config.execution_sources_path)
        register(self.store, 'work')
        register(self.store, 'personal')
        register(self.store, 'reviewer', provider='claude')
        admin.update_execution_source_allowlist(self.config, ['work', 'personal', 'reviewer'])

    def test_model_chain_roundtrip_covers_primary_agent_and_every_memory_duty(self):
        for target in ['chat', 'digest', 'archive', 'audit', 'recall']:
            admin.update_model_chain(self.config, target, [
                dict(provider='codex', source='work', model='model-a'),
                dict(provider='codex', source='personal', model='model-a'),
                dict(provider='claude', source='reviewer', model='sonnet')])
            self.assertEqual(getattr(self.config, target+'_candidates_for')(),
                (('codex@work','model-a'), ('codex@personal','model-a'), ('claude@reviewer','sonnet')))
        profiles = PrimaryAgent(self.config).profiles(load_role_profiles(), 'proj')
        self.assertEqual(profiles['controller'].source, 'work')
        self.assertEqual(profiles['controller'].fallbacks[0].source, 'personal')

    def test_plain_command_keeps_canonical_provider_flags_and_routing_metadata(self):
        command = self.config.chat_command('codex@work', 'model-a', 'private prompt')
        self.assertIsInstance(command, SourceCommand)
        self.assertEqual(command[0], '/opt/tools/work')
        self.assertIn('exec', command)
        self.assertIn('--sandbox', command)
        self.assertEqual(command.execution_source.source_id, 'work')
        self.assertNotIn('work', command[1:])

    def test_role_chain_roundtrip_and_adapter_permission_are_independent_of_account(self):
        admin.create_dedicated_role_profiles_file(self.config)
        admin.update_role_profile_chain(self.config, 'review', [
            dict(provider='codex', source='work', model='model-a', reasoning_effort='high'),
            dict(provider='codex', source='personal', model='model-a', reasoning_effort='high')])
        profile = load_role_profiles(self.config.role_profiles_file_for())['review']
        self.assertEqual([candidate.source for candidate in profile.candidates], ['work','personal'])
        adapter = build_provider_adapter(provider='codex', config=self.config, working_directory=self.root,
            timeout_seconds=10, profile=profile, candidate=profile.candidates[0], write_directories=(self.root / 'other',))
        self.assertEqual(adapter.permission, 'read-only')
        self.assertEqual(adapter.write_directories, ())
        self.assertEqual(adapter.executable, '/opt/tools/work')
        self.assertEqual(adapter.quota_bucket, 'work')

    def test_unknown_unallowed_and_wrong_provider_sources_cannot_be_selected(self):
        before = models_file(self.root).read_text()
        for provider, source in [('codex','unknown'), ('codex','reviewer')]:
            with self.assertRaises(ValueError):
                admin.update_model_chain(self.config, 'chat', [dict(provider=provider, source=source, model='model')])
        admin.update_execution_source_allowlist(self.config, [])
        with self.assertRaisesRegex(ValueError, 'not allowed'):
            self.config.source_binding('codex', 'work')
        self.assertEqual(self.config.chat_candidates_for()[0], ('codex','model-a'))

    def test_revoking_permission_after_command_construction_stops_next_call(self):
        command = self.config.chat_command('codex@work', 'model-a', 'prompt')
        admin.update_execution_source_allowlist(self.config, [])
        with patch('memtrace_harness.cli_process.subprocess.Popen') as popen:
            result = CliProcessRunner().run(command, cwd=self.root, timeout_seconds=5)
        popen.assert_not_called()
        self.assertEqual(result.source_failure, 'source_unavailable')
        self.assertEqual(result.execution_source_id, 'work')

    def test_process_lease_releases_on_completion_and_preserves_output_without_inventing_usage(self):
        command = self.config.chat_command('codex@work', 'model-a', 'prompt')
        process = MagicMock(pid=os.getpid(), returncode=0)
        process.communicate.return_value = ('raw provider output', '')
        with patch('memtrace_harness.cli_process.resolve_executable', return_value='/opt/tools/work'), \
             patch('memtrace_harness.cli_process.subprocess.Popen', return_value=process):
            result = CliProcessRunner().run(command, cwd=self.root, timeout_seconds=5)
        self.assertEqual(result.stdout, 'raw provider output')
        self.assertEqual(result.execution_source_id, 'work')
        self.assertEqual(self.store.get('work')['active'], 0)
        self.assertEqual(process.communicate.call_count, 1)

    def test_old_provider_only_settings_keep_original_commands(self):
        command = self.config.chat_command('codex', 'model-a', 'prompt')
        self.assertEqual(command[0], self.config.codex_command)
        self.assertNotIsInstance(command, SourceCommand)

    def test_adapter_raw_json_quota_error_cools_only_its_named_pool(self):
        profile = replace(load_role_profiles()['controller'], provider='codex', source='work', model='model-a')
        adapter = build_provider_adapter(provider='codex', config=self.config, profile=profile,
            working_directory=self.root, timeout_seconds=5)
        output = json.dumps({'type':'error', 'message':'usage limit reached'})
        process = ProcessResult([], 1, output, '', 'start', 'end', 1)
        adapter.process_runner = MagicMock()
        adapter.process_runner.run.return_value = process
        result = adapter.run(TaskEnvelope('t','ws','task'), 'source-quota')
        self.assertEqual(result.execution.execution_source_id, 'work')
        self.assertEqual(result.execution.usage.completeness, 'unavailable')
        self.assertEqual(Path(result.execution.raw_trace_ref).read_text(), output)
        self.assertIsNotNone(self.store.get('work')['cooldown'])
        self.assertIsNone(self.store.get('personal')['cooldown'])

    def test_disabled_first_account_falls_back_to_second_account_and_traces_real_source(self):
        from memtrace_harness.loop import AgentLoopRunner
        from memtrace_harness.trace_store import TraceStore
        from tests.test_loop import _stub_adapters
        profiles = load_role_profiles()
        profile = replace(profiles['controller'], provider='codex', model='model-a', source='work',
            fallbacks=(ModelCandidate('codex','model-a','medium','personal','personal'),), max_fallback_hops=1,
            fallback_on=('quota_exhausted',))
        disabled = self.store.get('work')
        self.store.change(dict(action='toggle', id='work', revision=disabled['revision'], enabled=False))
        adapters = [build_provider_adapter(provider='codex', config=self.config, profile=profile, candidate=candidate,
            working_directory=self.root, timeout_seconds=5) for candidate in profile.candidates]
        output = json.dumps({'type':'item.completed', 'item': {'type':'agent_message', 'text':json.dumps({'action':'no_action_needed','reason':'already done'})}})
        process = MagicMock(pid=os.getpid(), returncode=0)
        process.communicate.return_value = (output, '')
        all_adapters = _stub_adapters()
        all_adapters['controller'] = adapters[0]
        trace = TraceStore(self.config.trace_db_path)
        with patch('memtrace_harness.cli_process.resolve_executable', return_value='/opt/tools/personal'), \
             patch('memtrace_harness.cli_process.subprocess.Popen', return_value=process) as popen, \
             patch('memtrace_harness.adapters.cli._git_status_snapshot', return_value=None):
            summary = AgentLoopRunner(adapters=all_adapters, fallback_adapters={'controller':[adapters[1]]},
                role_profiles={**profiles,'controller':profile}, trace_store=trace).run(TaskEnvelope('t','ws','check'), conversation_id='sources')
        self.assertEqual(summary.status, 'succeeded')
        self.assertEqual(popen.call_count, 1)
        self.assertEqual([stage.response.execution.execution_source_id for stage in summary.stages], ['work','personal'])
        self.assertEqual([stage['execution_source_id'] for stage in trace.get_conversation_pipeline('sources')], ['work','personal'])
        self.assertEqual(trace.get_turn_detail(trace.get_conversation_pipeline('sources')[-1]['turn_id'])['execution_source_id'], 'personal')
        self.assertEqual(self.store.get('personal')['active'], 0)

    def test_override_preserves_its_account_and_backups_but_provider_change_resets_it(self):
        from memtrace_harness.model_override import resolve_model_override, with_skill_override
        original = replace(load_role_profiles()['develop'], provider='codex', source='work')
        candidate = resolve_model_override('codex/model-new', original, self.config)
        self.assertEqual(candidate.source, 'work')
        profiles = with_skill_override({'develop':original}, 'develop', candidate)
        self.assertEqual(profiles['develop'].fallbacks[0].source, 'work')
        candidate = resolve_model_override('claude/sonnet', original, self.config)
        self.assertIsNone(candidate.source)
        candidate = resolve_model_override('codex@personal/model-a', original, self.config)
        self.assertEqual(candidate.source, 'personal')

    def test_busy_source_never_starts_another_cli_or_cools_the_account(self):
        binding = self.config.source_binding('codex', 'work')
        lease = self.store.acquire(binding, 10)
        try:
            command = self.config.chat_command('codex@work', 'model-a', 'prompt')
            with patch('memtrace_harness.cli_process.subprocess.Popen') as popen:
                result = CliProcessRunner().run(command, cwd=self.root, timeout_seconds=5)
            popen.assert_not_called()
            self.assertEqual(result.source_failure, 'source_busy')
            self.assertIsNone(self.store.get('work')['cooldown'])
        finally:
            self.store.release(lease)

    def test_model_change_proposal_preserves_or_explicitly_changes_authorized_source(self):
        from memtrace_harness.model_changes import apply_model_change, validate_model_change
        payload = validate_model_change(dict(target='chat', model='codex@work/model-a'))
        self.assertEqual(payload['source'], 'work')
        apply_model_change(self.config, payload)
        self.assertEqual(self.config.chat_candidates_for()[0], ('codex@work','model-a'))
        apply_model_change(self.config, dict(target='chat', provider='codex', model='model-b'))
        self.assertEqual(self.config.chat_candidates_for()[0], ('codex@work','model-b'))
        apply_model_change(self.config, dict(target='chat', provider='codex', source='personal', model='model-b'))
        self.assertEqual(self.config.chat_candidates_for()[0], ('codex@personal','model-b'))
        apply_model_change(self.config, dict(target='chat', provider='claude', model='sonnet'))
        self.assertEqual(self.config.chat_candidates_for()[0], ('claude','sonnet'))

    def test_interrupted_cleanup_recognizes_only_leased_custom_wrapper_in_the_task_directory(self):
        from memtrace_harness.cli_process import kill_provider_clis_in_directory
        import signal
        binding = self.config.source_binding('codex', 'work')
        lease = self.store.acquire(binding, 10)
        self.store.attach(lease, 43210)
        try:
            with patch('memtrace_harness.cli_process._lsof_cwds', return_value=[(43210,str(self.root)),(43211,str(self.root))]), \
                 patch('memtrace_harness.cli_process._command_of', return_value='/bin/sh /opt/tools/work'), \
                 patch('memtrace_harness.cli_process.os.getpgid', side_effect=lambda pid: 99 if pid == 0 else pid), \
                 patch('memtrace_harness.cli_process._group_alive', return_value=False), \
                 patch('memtrace_harness.cli_process.os.killpg') as kill:
                groups = kill_provider_clis_in_directory(self.root)
            self.assertEqual(groups, [43210])
            kill.assert_called_once_with(43210, signal.SIGTERM)
        finally:
            self.store.release(lease)

    def test_new_calls_to_a_revoked_first_source_return_unavailable_so_fallback_can_continue(self):
        admin.update_execution_source_allowlist(self.config, ['personal'])
        command = self.config.chat_command('codex@work', 'model-a', 'prompt')
        with patch('memtrace_harness.cli_process.subprocess.Popen') as popen:
            result = CliProcessRunner().run(command, cwd=self.root, timeout_seconds=5)
        popen.assert_not_called()
        self.assertEqual(result.source_failure, 'source_unavailable')
        alternative = self.config.chat_command('codex@personal', 'model-a', 'prompt')
        self.assertEqual(alternative.execution_source.source_id, 'personal')
        self.assertTrue(alternative.execution_source.authorized)

    def test_exceptional_process_failure_stops_the_child_before_releasing_capacity(self):
        command = self.config.chat_command('codex@work', 'model-a', 'prompt')
        process = MagicMock(pid=os.getpid(), returncode=-15)
        process.communicate.side_effect = UnicodeError('bad decoder state')
        with patch('memtrace_harness.cli_process.resolve_executable', return_value='/opt/tools/work'), \
             patch('memtrace_harness.cli_process.subprocess.Popen', return_value=process), \
             patch.object(CliProcessRunner, '_terminate_process_group') as terminate:
            with self.assertRaises(UnicodeError):
                CliProcessRunner().run(command, cwd=self.root, timeout_seconds=5)
            terminate.assert_called_once_with(process)
        self.assertEqual(self.store.get('work')['active'], 0)
