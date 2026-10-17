"""Contact authorization uses real local APIs and injected model output; no model quota."""
import json
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from memtrace_harness.contacts import ContactBook
from memtrace_harness.ops_mcp import call_tool, OpsContext
from memtrace_harness.peer_links import LinkError, FILE_NAME
from memtrace_services.a2a.client import PeerClient, PeerCallError
from memtrace_services.a2a.config import LiveConfig, A2aConfig, OutboundPeer
from memtrace_services.a2a.protocol import Dispatcher
from memtrace_services.a2a.server import start_server
from memtrace_services.client import HarnessClient, HarnessError
from memtrace_services.web.aggregate import HarnessTarget
from memtrace_services.web.contacts import ContactRelay
from tests.test_peer_calling import Side, free_port
from tests.test_peer_api import reply


class ContactBookTests(TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.a = ContactBook(Path(temp.name) / 'a.json')
        self.b = ContactBook(Path(temp.name) / 'b.json')
        self.aid, self.bid = self.a.initialize(), self.b.initialize()
        self.entries = [{'id': self.aid, 'name': 'alpha', 'url': 'http://127.0.0.1:9001'},
                        {'id': self.bid, 'name': 'beta', 'url': 'http://127.0.0.1:9002'}]
        self.a.set_directory(self.entries)
        self.b.set_directory(self.entries)

    def receive(self):
        self.a.request('beta', '想了解規格', 'alpha')
        return self.b.receive(self.a.relay_view()['outgoing'][0])

    def test_identity_is_persistent_and_independent_of_name(self):
        self.assertEqual(ContactBook(self.a.path).initialize(), self.aid)
        self.entries[1]['name'] = 'renamed'
        self.a.set_directory(self.entries)
        row = self.a.request('renamed', 'reason', 'alpha')
        self.assertEqual(row['target_id'], self.bid)
        self.assertNotEqual(self.aid, self.bid)

    def test_public_views_and_relay_never_export_a_connection_token(self):
        row, _ = self.receive()
        raw = self.a._load()['contacts']['outgoing'][row['request_id']]
        for view in (self.a.view(), self.b.view(), self.a.relay_view(), self.b.relay_view()):
            self.assertNotIn(raw['token'], json.dumps(view))
        for view in (self.a.view(), self.b.view()):
            self.assertNotIn(raw['token_sha256'], json.dumps(view))
        self.assertEqual(self.a.path.stat().st_mode & 0o777, 0o600)

    def test_no_access_before_approval_and_repeated_approval_is_refused(self):
        row, _ = self.receive()
        self.assertFalse(self.a.authorized_peer('beta'))
        self.assertFalse(self.b._load()['inbound'])
        approved = self.b.resolve(row['request_id'], 'approved')
        result = {**approved, 'token_sha256': self.a.relay_view()['outgoing'][0]['token_sha256']}
        self.a.apply_result(result)
        self.assertEqual(self.a.authorized_peer('beta'), self.bid)
        self.assertIn(self.aid, self.b._load()['inbound'])
        self.assertFalse(self.b._load()['outbound'])  # no reverse grant
        with self.assertRaises(LinkError):
            self.b.resolve(row['request_id'], 'approved')
        self.b.resolve(row['request_id'], 'revoked')
        self.assertFalse(self.b._load()['inbound'])
        result['state'] = 'revoked'
        self.a.apply_result(result)
        self.assertIsNone(self.a.authorized_peer('beta'))
        result['state'] = 'approved'
        self.a.apply_result(result)
        self.assertIsNone(self.a.authorized_peer('beta'))  # late approval cannot revive

    def test_duplicate_delivery_does_not_create_another_request(self):
        row, _ = self.receive()
        self.b.mark_notified(row['request_id'])
        self.assertFalse(self.b.receive(self.a.relay_view()['outgoing'][0])[1])
        self.assertEqual(len(self.b.view()['incoming']), 1)
        self.assertEqual(self.a.request('beta', 'another reason', 'alpha')['request_id'], row['request_id'])
        changed = self.a.relay_view()['outgoing'][0]
        changed['token_sha256'] = '0' * 64
        with self.assertRaises(LinkError):
            self.b.receive(changed)

    def test_rejected_expired_and_cancelled_requests_cannot_grant_access(self):
        row, _ = self.receive()
        self.b.resolve(row['request_id'], 'rejected')
        with self.assertRaises(LinkError):
            self.b.resolve(row['request_id'], 'approved')
        self.a.cancel(row['request_id'])
        result = {**row, 'state': 'approved', 'token_sha256': self.a.relay_view()['outgoing'][0]['token_sha256']}
        self.a.apply_result(result)
        self.assertIsNone(self.a.authorized_peer('beta'))
        fresh = self.a.request('beta', 'new request', 'alpha')
        raw = self.a._load()
        raw['contacts']['outgoing'][fresh['request_id']]['expires_at'] = '2000-01-01T00:00:00+00:00'
        self.a._save(raw)
        self.assertEqual(self.a.view()['outgoing'][-1]['state'], 'expired')
        self.assertIsNone(self.a.authorized_peer('beta'))

    def test_same_names_need_an_id_and_an_unknown_source_is_rejected(self):
        entries = self.entries + [{'id': 'h_' + 'c' * 30, 'name': 'beta', 'url': 'http://127.0.0.1:9003'}]
        self.a.set_directory(entries)
        with self.assertRaises(LinkError):
            self.a.request('beta', 'reason', 'alpha')
        self.a.request(self.bid, 'reason', 'alpha')
        incoming = self.a.relay_view()['outgoing'][0]
        incoming['source_id'] = 'h_' + 'd' * 30
        with self.assertRaises(LinkError):
            self.b.receive(incoming)


class ContactFlowTests(TestCase):
    def setUp(self):
        self.a, self.b = Side(self, 'a'), Side(self, 'b')
        self.ca = HarnessClient(self.a.url, self.a.main_token)
        self.cb = HarnessClient(self.b.url, self.b.main_token)
        self.books = [ContactBook(side.store.db_path.parent / FILE_NAME) for side in (self.a, self.b)]
        self.aid, self.bid = [book.initialize() for book in self.books]
        self.servers = []
        for side in (self.a, self.b):
            port = free_port()
            config = A2aConfig(name='test', description='test', host='127.0.0.1', port=port,
                              public_url=f'http://127.0.0.1:{port}', harness_url=side.url,
                              harness_peer_token=side.peer_token)
            live = LiveConfig(config, side.gateway, ttl=9999)
            server = start_server(live, Dispatcher(live, side.gateway))
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
            side.gateway.register_endpoint(config.public_url)
            self.servers.append((config, live))
        self.ca.admin('peer-name', {'name': 'alpha'})
        self.cb.admin('peer-name', {'name': 'beta'})
        self.relay = ContactRelay([HarnessTarget('a', self.a.url, self.a.main_token),
                                   HarnessTarget('b', self.b.url, self.b.main_token)])
        self.relay.run_once()

    def request(self):
        return self.ca.admin('contact-request', {'peer': 'beta', 'reason': '請教規格'})['request']

    def test_request_waits_for_a_tap_then_readonly_questions_work_and_revocation_is_immediate(self):
        from memtrace_services.a2a.carrier import Carrier
        carrier = Carrier(self.servers[0][1], self.a.gateway)
        self.assertIsNone(carrier._client(self.bid))  # warm the caller cache before contact approval
        row = self.request()
        self.relay.run_once()
        self.relay.run_once()
        incoming = self.books[1].view()['incoming']
        self.assertEqual(len(incoming), 1)
        self.assertEqual(incoming[0]['state'], 'pending')
        cards = [e for e in self.b.service.fetch_events(limit=100, wait_seconds=0) if e.get('actions')]
        self.assertEqual(len(cards), 1)
        self.assertIn('申請與你聯絡', cards[0]['text'])
        self.assertNotIn('report_issue', self.a.ctx().tool_names())
        toast = self.cb.post_action('contact_approve:' + row['request_id'], audience=None)
        self.assertIn('已授權', toast)
        self.relay.run_once()
        self.assertEqual(self.books[0].view()['outgoing'][0]['state'], 'approved')
        self.assertIsNotNone(carrier._client(self.bid))  # newly granted contacts work without waiting for TTL
        self.assertIsNone(self.books[1].authorized_peer('alpha'))
        # A new approved contact queues directly, without a per-question confirmation card.
        output = call_tool(self.a.ctx(), 'ask_peer', {'peer': 'beta', 'question': '問題'})
        self.assertIn('ask_', output)
        self.assertEqual(self.a.store.count_peer_outbound_unfinished(), 1)
        config, live = self.servers[1]
        token = self.books[0]._load()['outbound'][self.bid]['token']
        client = PeerClient(OutboundPeer('beta', config.public_url, token), poll_seconds=.01)
        with patch('memtrace_harness.peer.CliProcessRunner.run', return_value=reply('規格回覆')) as model:
            self.assertEqual(client.ask('規格', 'c', 0), ('completed', '規格回覆', None))
            self.assertEqual(model.call_count, 1)
        self.assertIsNotNone(live.authenticate(token))
        self.cb.admin('contact-resolve', {'request_id': row['request_id'], 'decision': 'revoked'})
        self.assertIsNone(live.authenticate(token))  # no wait for the long TTL
        with self.assertRaises(PeerCallError) as caught:
            client._rpc('GetTask', {'id': 'anything'})
        self.assertEqual(str(caught.exception), 'peer answered HTTP 401')
        self.relay.run_once()
        self.assertIsNone(self.books[0].authorized_peer('beta'))
        self.assertIsNone(carrier._client(self.bid))
        self.assertIsNotNone(self.books[1].view()['incoming'][0]['last_contact_at'])

    def test_rename_preserves_grants_and_readonly_peer_context_cannot_request_or_ask(self):
        row = self.request()
        self.relay.run_once()
        self.cb.admin('contact-resolve', {'request_id': row['request_id'], 'decision': 'approved'})
        self.relay.run_once()
        self.cb.admin('peer-name', {'name': 'renamed'})
        self.relay.run_once()
        self.assertEqual(self.books[0].authorized_peer('renamed'), self.bid)
        self.assertEqual(len(self.books[0].view()['outgoing']), 1)
        ctx = self.a.ctx()
        ctx.read_only = True
        for tool in ('request_contact', 'list_harnesses', 'ask_peer', 'report_issue'):
            self.assertNotIn(tool, ctx.tool_names())

    def test_outgoing_cancellation_withdraws_the_receivers_grant_and_survives_restart(self):
        row = self.request()
        self.relay.run_once()
        self.cb.admin('contact-resolve', {'request_id': row['request_id'], 'decision': 'approved'})
        self.relay.run_once()
        self.ca.admin('contact-cancel', {'request_id': row['request_id']})
        self.relay.run_once()
        self.assertEqual(self.books[1].view()['incoming'][0]['state'], 'revoked')
        self.assertFalse(self.books[1]._load()['inbound'])
        recreated = ContactBook(self.books[0].path)
        self.assertEqual(recreated.initialize(), self.aid)
        self.assertEqual(recreated.view()['outgoing'][0]['state'], 'revoked')

    def test_peer_gateway_token_cannot_manage_contacts_and_no_secrets_reach_public_api(self):
        self.request()
        self.relay.run_once()
        gateway_client = HarnessClient(self.a.url, self.a.peer_token)
        for method, path, body in [('GET', '/v1/contacts', None), ('GET', '/v1/contacts/relay', None),
                                  ('POST', '/v1/admin/contact-request', {'peer': 'beta', 'reason': 'x'})]:
            with self.assertRaises(HarnessError) as caught:
                gateway_client._request(method, path, body)
            self.assertEqual(caught.exception.status, 401)
        private = self.books[0]._load()['contacts']['outgoing']
        token = next(iter(private.values()))['token']
        for view in (self.ca._request('GET', '/v1/contacts'), self.ca.status()):
            self.assertNotIn(token, json.dumps(view))
            self.assertNotIn('token_sha256', json.dumps(view.get('contacts', view.get('projects', []))))

    def test_plan_waits_for_contact_approval_then_for_the_actual_remote_answer(self):
        from memtrace_harness.plan_runner import PlanRunner
        service = self.a.service
        runner = service.plan_runner
        payload = {'goal': 'ask beta with authorization', 'steps': [
            {'title': 'contact', 'tool': 'request_contact', 'args': {'peer': 'beta', 'reason': 'question'}},
            {'title': 'ask', 'tool': 'ask_peer', 'args': {'peer': 'beta', 'question': 'question'},
             'expect': {'kind': 'contains', 'value': 'actual remote answer'}},
        ]}
        self.assertIsNone(runner.create(self.a.scope, None, json.dumps(payload)))
        pid = self.a.store.list_active_plans()[0]['plan_id']
        runner.tick()
        steps = self.a.store.list_plan_steps(pid)
        self.assertEqual([s['status'] for s in steps], ['awaiting', 'pending'])
        rid = steps[0]['request_id'].removeprefix('contact:')
        self.relay.run_once()
        PlanRunner(service).tick()  # reconstruction cannot pretend pending approval is success
        self.assertEqual(self.a.store.list_plan_steps(pid)[0]['status'], 'awaiting')
        self.cb.admin('contact-resolve', {'request_id': rid, 'decision': 'approved'})
        self.relay.run_once()
        runner.tick()
        self.assertEqual(self.a.store.list_plan_steps(pid)[0]['status'], 'done')
        runner.tick()
        second = self.a.store.list_plan_steps(pid)[1]
        self.assertEqual(second['status'], 'awaiting')
        self.assertTrue(second['request_id'].startswith('peer:'))
        self.a.store.claim_peer_outbound()
        PlanRunner(service).tick()
        self.assertEqual(self.a.store.list_plan_steps(pid)[1]['status'], 'awaiting')
        self.a.store.finish_peer_outbound(second['request_id'][5:], 'completed', result_text='actual remote answer')
        runner.tick()
        self.assertEqual(self.a.store.list_plan_steps(pid)[1]['status'], 'done')


class ContactWebsiteTests(TestCase):
    def setUp(self):
        from tests.test_peer_pairing import Harnesses
        self.w = Harnesses(self)
        self.relay = ContactRelay([h['target'] for h in self.w.h.values()])
        self.relay.run_once()

    def test_website_requests_require_receiving_approval_and_support_history_and_revocation(self):
        status, response = self.w.post('/api/contact-request', {'project': 'alpha', 'peer': 'beta', 'reason': '了解規格'})
        self.assertEqual(status, 200)
        rid = response['request']['request_id']
        self.assertEqual(response['request']['state'], 'pending')
        self.relay.run_once()
        self.assertFalse(self.w.links('beta')['inbound'])
        status, response = self.w.post('/api/contact-resolve', {'project': 'beta', 'request_id': rid,
                                                             'decision': 'approved', 'trust': 'collaborate'})
        self.assertEqual(status, 200)
        self.relay.run_once()
        self.w.aggregator._invalidate()
        project = next(p for p in self.w.aggregator.collect()['projects'] if p['name'] == 'beta')
        self.assertEqual(project['peer_links']['contacts']['incoming'][0]['state'], 'approved')
        status, _ = self.w.post('/api/contact-resolve', {'project': 'beta', 'request_id': rid, 'decision': 'revoked'})
        self.assertEqual(status, 200)
        self.assertFalse(self.w.links('beta')['inbound'])
        self.relay.run_once()
        status, response = self.w.post('/api/contact-resolve', {'project': 'beta', 'request_id': rid, 'decision': 'approved'})
        self.assertEqual(status, 400)

    def test_manual_pairing_and_invites_are_removed_and_no_relay_privilege_reaches_browser(self):
        for path in ('/api/peer-pair', '/api/peer-invite', '/api/peer-accept'):
            self.assertEqual(self.w.post(path, {'project': 'alpha'})[0], 410)
        for path in ('/api/contact-directory', '/api/contact-incoming', '/api/contact-result', '/api/contact-withdraw'):
            self.assertEqual(self.w.post(path, {'project': 'alpha'})[0], 404)
        for setting in ('peer-link-inbound-create', 'peer-link-outbound-set'):
            with self.assertRaises(HarnessError) as caught:
                self.w.h['alpha']['target'].client().admin(setting, {'name': 'beta', 'trust': 'ask'})
            self.assertEqual(caught.exception.status, 410)
        self.assertFalse(self.w.links('alpha')['inbound'])
        self.assertFalse(self.w.links('alpha')['outbound'])
