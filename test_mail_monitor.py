import copy
import json
import os
import ssl
import sys
import urllib.error
import tempfile
import unittest
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import Mock, patch

import build_pages
import mail_monitor as monitor
import mail_credentials
import api_transport
import patch_status_dashboard as dashboard
from test_analysis import message
from test_series import numbered, rows


def raw_mail(mid, subject='Re: [PATCH] net: example', sender='Reviewer <reviewer@example.org>', parent='root', body='Please clarify the locking rationale.'):
    mail = EmailMessage()
    mail['Message-ID'] = '<' + mid + '>'
    mail['Subject'] = subject
    mail['From'] = sender
    mail['To'] = dashboard.DEFAULT_EMAIL
    mail['Date'] = 'Mon, 14 Sep 2026 10:00:00 +0800'
    if parent:
        mail['In-Reply-To'] = '<' + parent + '>'
        mail['References'] = '<' + parent + '>'
    mail.set_content(body)
    return mail.as_bytes()


class FakeIMAP:
    def __init__(self, mails=None, validity=1, next_uid=None, internal='14-Sep-2026 10:00:00 +0800'):
        self.mails = mails or {}
        self.validity = validity
        self.next_uid = next_uid if next_uid is not None else max(self.mails, default=0) + 1
        self.internal = internal
        self.body_fetches = []
        self.calls = []
        self.fail_body = False

    def response(self, name):
        return name, [str(self.validity if name == 'UIDVALIDITY' else self.next_uid).encode()]

    def uid(self, command, *args):
        self.calls.append((command, args))
        if command == 'SEARCH':
            # Deliberately return stale UIDs too: the collector must enforce its range.
            return 'OK', [b' '.join(str(uid).encode() for uid in self.mails)]
        uid, fields = int(args[0]), args[1]
        if 'HEADER.FIELDS' in fields:
            raw = self.mails[uid].split(b'\n\n', 1)[0] + b'\n\n'
        else:
            self.body_fetches.append(uid)
            if self.fail_body:
                return 'NO', [b'private error must not be printed']
            raw = self.mails[uid]
        meta = f'1 (UID {uid} RFC822.SIZE {len(self.mails[uid])} INTERNALDATE "{self.internal}"'.encode()
        return 'OK', [(meta, raw), b')']

    def logout(self):
        pass


class TLSTrustTests(unittest.TestCase):
    def test_empty_default_store_loads_bundle_and_keeps_verification_enabled(self):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        bundle = Mock()
        bundle.where.return_value = 'trusted-bundle.pem'
        with patch.dict(os.environ, {}, clear=True), \
                patch.dict(sys.modules, {'certifi': bundle}), \
                patch.object(ssl, 'create_default_context', return_value=context), \
                patch.object(ssl, 'get_default_verify_paths', return_value=Mock(capath=None)), \
                patch.object(context, 'load_verify_locations') as load:
            self.assertIs(monitor.tls_context(), context)
        load.assert_called_once_with(cafile='trusted-bundle.pem')
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)

    def test_existing_roots_and_lazy_ca_directory_remain_authoritative(self):
        for roots, capath in ((10, None), (0, 'system-ca-directory')):
            with self.subTest(roots=roots, capath=capath):
                context = Mock()
                context.cert_store_stats.return_value = {'x509_ca': roots}
                with patch.dict(os.environ, {}, clear=True), \
                        patch.dict(sys.modules, {'certifi': None}), \
                        patch.object(ssl, 'create_default_context', return_value=context), \
                        patch.object(ssl, 'get_default_verify_paths', return_value=Mock(capath=capath)):
                    self.assertIs(monitor.tls_context(), context)
                context.load_verify_locations.assert_not_called()

    def test_explicit_ca_settings_never_add_extra_trusted_roots(self):
        for name in ('PATCH_MONITOR_CA_FILE', 'SSL_CERT_FILE', 'SSL_CERT_DIR'):
            with self.subTest(name=name):
                context = Mock()
                context.cert_store_stats.return_value = {'x509_ca': 0}
                with patch.dict(os.environ, {name: 'selected-ca'}, clear=True), \
                        patch.dict(sys.modules, {'certifi': None}), \
                        patch.object(ssl, 'create_default_context', return_value=context) as create:
                    self.assertIs(monitor.tls_context(), context)
                    create.assert_called_once_with(cafile='selected-ca' if name == 'PATCH_MONITOR_CA_FILE' else None)
                context.load_verify_locations.assert_not_called()

    def test_invalid_explicit_ca_file_fails_closed(self):
        with tempfile.TemporaryDirectory() as folder, \
                patch.dict(os.environ, {'PATCH_MONITOR_CA_FILE': str(Path(folder) / 'missing.pem')}, clear=True):
            with self.assertRaises(OSError):
                monitor.tls_context()

    def test_missing_bundle_explains_fix_without_disabling_validation(self):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        with patch.dict(os.environ, {}, clear=True), \
                patch.dict(sys.modules, {'certifi': None}), \
                patch.object(ssl, 'create_default_context', return_value=context), \
                patch.object(ssl, 'get_default_verify_paths', return_value=Mock(capath=None)):
            with self.assertRaisesRegex(RuntimeError, 'certifi'):
                monitor.tls_context()
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)

    def test_certificate_error_explains_pre_login_failure_without_raw_details(self):
        text = monitor.mailbox_error(ssl.SSLCertVerificationError('synthetic-private-server-text'))
        self.assertIn('尚未验证授权码', text)
        self.assertNotIn('synthetic-private-server-text', text)

    @unittest.skipUnless(os.name == 'nt', 'Windows HTTPS fallback')
    def test_explicit_trust_policy_prevents_native_https_fallback(self):
        for name in ('PATCH_MONITOR_CA_FILE', 'SSL_CERT_FILE', 'SSL_CERT_DIR'):
            with self.subTest(name=name):
                opener = Mock()
                opener.open.side_effect = urllib.error.URLError(ssl.SSLCertVerificationError('chain'))
                with patch.dict(os.environ, {name: 'selected-ca'}, clear=True), \
                        patch.object(monitor, 'tls_context', return_value=ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)), \
                        patch.object(monitor.urllib.request, 'build_opener', return_value=opener), \
                        patch.object(monitor, 'windows_json') as native:
                    with self.assertRaises(monitor.ServiceFailure):
                        monitor.post_json('https://example.org/api', {}, 'GLM', 'synthetic-key')
                    native.assert_not_called()


class MailMonitorTests(unittest.TestCase):
    def setUp(self):
        self.config = copy.deepcopy(monitor.DEFAULTS)
        self.config['auto_folders'] = False
        self.seed = dashboard.upgrade_payload({'records': rows(message('root', '[PATCH] net: example', 'Patch rationale', author=True))})
        self.state = monitor.new_state(self.config)
        self.state.update(uidvalidity=1, cursor=10, initialized_at='2026-09-01T00:00:00+08:00')

    def batch(self):
        client = FakeIMAP({11: raw_mail('reply')})
        additions = monitor.collect(client, self.config, self.seed, self.state)
        payload = monitor.combined_payload(self.seed, self.state, self.config)
        monitor.enqueue(additions, payload, self.state, self.config)
        return next(iter(self.state['batches'].values()))

    def test_first_connection_establishes_baseline_without_fetching_or_notifying(self):
        state = monitor.new_state(self.config)
        client = FakeIMAP({10: raw_mail('historical')})
        self.assertEqual(monitor.collect(client, self.config, self.seed, state), [])
        self.assertEqual(state['cursor'], 10)
        self.assertEqual(client.calls, [])
        self.assertEqual(state['batches'], {})

    def test_headers_filter_unrelated_bodies_and_do_not_mark_read(self):
        client = FakeIMAP({9: raw_mail('old'), 11: raw_mail('new'),
                           12: raw_mail('private', 'Family plans', parent=None)})
        result = monitor.collect(client, self.config, self.seed, self.state)
        self.assertEqual([m['message_id'] for m in result], ['new'])
        self.assertEqual(client.body_fetches, [11])
        self.assertEqual(self.state['cursor'], 12)
        self.assertTrue(all('BODY.PEEK' in args[1] for cmd, args in client.calls if cmd == 'FETCH'))
        self.assertNotIn('private', self.state['messages'])

    def test_changed_subject_reply_and_new_own_submission_are_linked(self):
        client = FakeIMAP({11: raw_mail('reply', 'A different heading'),
            12: raw_mail('self', '[PATCH] gpio: new patch', dashboard.DEFAULT_EMAIL, None),
            13: raw_mail('reply2', 'Comments', parent='self'),
            14: raw_mail('sibling', '[PATCH 2/2] foreign patch', parent='root')})
        result = monitor.collect(client, self.config, self.seed, self.state)
        self.assertEqual({m['message_id'] for m in result}, {'reply', 'self', 'reply2'})
        payload = monitor.combined_payload(self.seed, self.state, self.config)
        monitor.enqueue(result, payload, self.state, self.config)
        mids = [mid for b in self.state['batches'].values() for mid in b['message_ids']]
        self.assertEqual(set(mids), {'reply', 'reply2'})

    def test_duplicate_message_ids_and_stale_uid_search_do_not_notify_twice(self):
        client = FakeIMAP({11: raw_mail('new'), 12: raw_mail('new'), 13: raw_mail('root')})
        first = monitor.collect(client, self.config, self.seed, self.state)
        self.assertEqual(len(first), 1)
        self.assertEqual(client.body_fetches, [11])
        self.assertEqual(monitor.collect(client, self.config, self.seed, self.state), [])

    def test_failed_body_fetch_does_not_advance_cursor(self):
        client = FakeIMAP({11: raw_mail('new')})
        client.fail_body = True
        with self.assertRaises(RuntimeError):
            monitor.collect(client, self.config, self.seed, self.state)
        self.assertEqual(self.state['cursor'], 10)
        self.assertEqual(self.state['messages'], {})

    def test_large_message_generates_header_only_notification_without_llm_body(self):
        self.config['max_message_bytes'] = 20
        client = FakeIMAP({11: raw_mail('large')})
        result = monitor.collect(client, self.config, self.seed, self.state)
        self.assertTrue(result[0]['body_omitted'])
        self.assertEqual(client.body_fetches, [])

    def test_uidvalidity_reset_rescans_new_mail_but_not_prebaseline_history(self):
        old = FakeIMAP({1: raw_mail('historical')}, validity=2, internal='01-Aug-2026 10:00:00 +0800')
        self.assertEqual(monitor.collect(old, self.config, self.seed, self.state), [])
        self.assertEqual(old.body_fetches, [])
        new = FakeIMAP({1: raw_mail('historical'), 2: raw_mail('new')}, validity=2)
        self.assertEqual([m['message_id'] for m in monitor.collect(new, self.config, self.seed, self.state)], ['new'])
        self.assertEqual(self.state['cursor_resets'], 1)

    def test_batch_limit_leaves_later_uids_for_next_check(self):
        self.config['batch_limit'] = 1
        client = FakeIMAP({11: raw_mail('one'), 12: raw_mail('two')})
        self.assertEqual(len(monitor.collect(client, self.config, self.seed, self.state)), 1)
        self.assertEqual(self.state['cursor'], 11)
        self.assertEqual(len(monitor.collect(client, self.config, self.seed, self.state)), 1)
        self.assertEqual(self.state['cursor'], 12)

    def test_two_series_replies_are_one_notification(self):
        seed = dashboard.upgrade_payload({'records': rows(numbered('c', '0/2', 'net: series'),
            numbered('a', '1/2', 'net: one', parent='c'), numbered('b', '2/2', 'net: two', parent='c'))})
        client = FakeIMAP({11: raw_mail('ra', 'Re: [PATCH 1/2] net: one', parent='a'),
                           12: raw_mail('rb', 'Re: [PATCH 2/2] net: two', parent='b')})
        additions = monitor.collect(client, self.config, seed, self.state)
        monitor.enqueue(additions, monitor.combined_payload(seed, self.state, self.config), self.state, self.config)
        self.assertEqual(len(self.state['batches']), 1)
        batch = next(iter(self.state['batches'].values()))
        self.assertEqual(batch['title'], 'net: series')
        self.assertEqual(set(batch['message_ids']), {'ra', 'rb'})

    def test_llm_failure_still_notifies_then_retries_summary_without_resending(self):
        batch = self.batch()
        with patch.object(monitor, 'summarize', side_effect=monitor.ServiceFailure('GLM', 'HTTP 429')), patch.object(monitor, 'send_feishu') as send:
            monitor.process_queue(self.state, self.config, lambda: None, 'fake-key', 'fake-webhook')
            send.assert_called_once()
        self.assertEqual(batch['delivery'], 'sent')
        self.assertEqual(batch['ai_state'], 'error')
        with patch.object(monitor, 'summarize', return_value={'summary': '建议补充理由'}) as llm, patch.object(monitor, 'send_feishu') as send:
            monitor.process_queue(self.state, self.config, lambda: None, 'fake-key', 'fake-webhook')
            llm.assert_called_once()
            send.assert_not_called()
        self.assertEqual(batch['ai_state'], 'complete')

    def test_sending_checkpoint_precedes_webhook_and_restart_does_not_resend(self):
        batch = self.batch()
        snapshots = []
        def save():
            snapshots.append(copy.deepcopy(self.state))
        def send(*args):
            self.assertEqual(snapshots[-1]['batches'][batch['id']]['delivery'], 'sending')
            raise monitor.ServiceFailure('飞书', 'timeout', uncertain=True)
        with patch.object(monitor, 'send_feishu', side_effect=send):
            monitor.process_queue(self.state, self.config, save, '', 'fake-webhook')
        self.assertEqual(batch['delivery'], 'uncertain')
        batch['delivery'] = 'sending'  # Simulate abrupt exit before the final checkpoint.
        with patch.object(monitor, 'send_feishu') as sender:
            monitor.process_queue(self.state, self.config, save, '', 'fake-webhook')
            sender.assert_not_called()
        self.assertEqual(batch['delivery'], 'uncertain')

    def test_definite_feishu_rejections_have_bounded_retries(self):
        batch = self.batch()
        with patch.object(monitor, 'send_feishu', side_effect=monitor.ServiceFailure('飞书', 'rejected')) as sender:
            for _ in range(5):
                monitor.process_queue(self.state, self.config, lambda: None, '', 'fake-webhook')
            self.assertEqual(sender.call_count, 3)
        self.assertEqual(batch['delivery'], 'failed')

    def test_glm_receives_bounded_text_and_bad_response_cannot_change_status(self):
        batch = self.batch()
        self.state['messages']['reply']['body'] = 'text ' * 10000 + '\ndiff --git secret'
        answer = {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps({
            'summary': '<script>untrusted</script>', 'intent': 'accepted', 'action_items': ['Check original'],
            'reply_draft': 'Could you clarify?'} )}}]}
        with patch.object(monitor, 'post_json', return_value=answer) as post:
            batch['ai'] = monitor.summarize(batch, self.state, self.config, 'fake-key')
            request = post.call_args.args[1]
            content = json.loads(request['messages'][1]['content'])
            self.assertLessEqual(len(content['messages'][0]['text']), 6000)
            self.assertNotIn('diff --git', content['messages'][0]['text'])
        payload = monitor.combined_payload(self.seed, self.state, self.config)
        self.assertEqual(payload['records'][0]['status'], 'Submitted')
        html = dashboard.render_report(payload)
        self.assertNotIn('<script>untrusted</script>', html)
        with patch.object(monitor, 'post_json', return_value={'choices': [{'message': {'content': '{"summary": []}'}}]}):
            with self.assertRaises(monitor.ServiceFailure):
                monitor.summarize(batch, self.state, self.config, 'fake-key')

    def test_feishu_signing_and_error_codes(self):
        batch = self.batch()
        import base64
        import hashlib
        # Independently construct HMAC-SHA256 from the RFC 2104 pads.
        key = b'123456\ntest-secret'.ljust(64, b'\0')
        inner = hashlib.sha256(bytes(x ^ 0x36 for x in key)).digest()
        digest = hashlib.sha256(bytes(x ^ 0x5c for x in key) + inner).digest()
        signed = monitor.feishu_payload('hello', 'test-secret', 123456)
        self.assertEqual(signed['sign'], base64.b64encode(digest).decode())
        self.assertEqual(signed['timestamp'], '123456')
        with patch.object(monitor, 'post_json', return_value={'code': 19024}):
            with self.assertRaises(monitor.ServiceFailure) as error:
                monitor.send_feishu(batch, self.state, self.config, 'https://open.feishu.cn/open-apis/bot/v2/hook/test')
            self.assertFalse(error.exception.uncertain)
        with patch.object(monitor, 'post_json') as post:
            with self.assertRaises(monitor.ServiceFailure):
                monitor.send_feishu(batch, self.state, self.config, 'https://other.example/hook/secret')
            post.assert_not_called()

    def test_private_report_preserves_public_seed_and_cannot_be_published(self):
        original = copy.deepcopy(self.seed)
        self.batch()
        private = monitor.combined_payload(self.seed, self.state, self.config)
        self.assertEqual(self.seed, original)
        self.assertTrue(private['private_mailbox'])
        self.assertEqual(private['stored_messages'], 2)
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            source, target = folder / 'private.html', folder / 'index.html'
            dashboard.save_report(target, self.seed)
            before = target.read_bytes()
            for flags in (True, False):
                candidate = copy.deepcopy(private)
                if not flags:
                    candidate.pop('private_mailbox')
                    candidate.pop('hosting_mode')
                dashboard.save_report(source, candidate)
                with patch.object(build_pages, 'SITE', target):
                    with self.assertRaisesRegex(ValueError, 'private'):
                        build_pages.build(report=source)
                self.assertEqual(target.read_bytes(), before)

    def test_imported_lore_copy_does_not_duplicate_private_mail_or_erase_ai(self):
        self.batch()
        private = monitor.combined_payload(self.seed, self.state, self.config)
        public = copy.deepcopy(private)
        public['records'][0]['messages'][-1]['provenance'] = 'public_archive'
        next_report = monitor.combined_payload(public, self.state, self.config)
        self.assertEqual(next_report['stored_messages'], 2)
        self.assertEqual(len(next_report['records'][0]['ai_updates']), 1)

    def test_run_once_commits_cursor_and_outbox_together(self):
        original = copy.deepcopy(self.state)
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
                'PATCH_IMAP_PASSWORD': 'fake', 'PATCH_GLM_API_KEY': 'fake', 'PATCH_FEISHU_WEBHOOK': 'fake'}), \
                patch.object(monitor, 'enqueue', side_effect=ValueError('parse failed')):
            with self.assertRaises(ValueError):
                monitor.run_once(Path(directory), self.config, self.state, self.seed,
                                 connect=lambda *_: FakeIMAP({11: raw_mail('reply')}))
        self.assertEqual(self.state, original)

    def test_private_files_not_in_publisher_allowlist_and_overlapping_processes_rejected(self):
        from publish_pages import FILES
        self.assertFalse(any(p.startswith('local/') for p in FILES))
        with tempfile.TemporaryDirectory() as directory:
            with monitor.exclusive_lock(Path(directory)):
                with self.assertRaises(RuntimeError):
                    with monitor.exclusive_lock(Path(directory)):
                        self.fail('acquired a second lock')

    @unittest.skipUnless(os.name == 'nt', 'Windows DPAPI only')
    def test_credentials_are_encrypted_and_roundtrip_under_current_windows_user(self):
        values = {'PATCH_IMAP_PASSWORD': 'synthetic-mail-password', 'PATCH_GLM_API_KEY': 'synthetic-glm-key',
                  'PATCH_FEISHU_WEBHOOK': 'https://open.feishu.cn/open-apis/bot/v2/hook/synthetic-test',
                  'PATCH_FEISHU_SECRET': ''}
        encrypted = mail_credentials.encode_credentials(values)
        self.assertNotIn('synthetic-mail-password', json.dumps(encrypted))
        self.assertNotIn('synthetic-glm-key', json.dumps(encrypted))
        with tempfile.TemporaryDirectory() as directory:
            monitor.save_json(Path(directory) / 'credentials.json', encrypted)
            self.assertEqual(mail_credentials.read_credentials(Path(directory)), values)

    def test_configuration_refuses_noninteractive_input_before_reading_secrets(self):
        with patch.object(mail_credentials.sys.stdin, 'isatty', return_value=False), \
                patch.object(mail_credentials, 'read_credentials') as reader:
            with self.assertRaises(RuntimeError):
                mail_credentials.interactive_config(Path('.'), self.config, lambda *_: None, lambda _: 'identity')
            reader.assert_not_called()

    def test_provider_endpoints_and_legacy_config(self):
        self.assertEqual(mail_credentials.glm_endpoint({}), mail_credentials.OFFICIAL_GLM_URL)
        for url in ('https://loliapi.org', 'https://loliapi.org/v1/', 'https://loliapi.org/v1/chat/completions'):
            self.assertEqual(mail_credentials.glm_endpoint({'glm_api_url': url}), 'https://loliapi.org/v1/chat/completions')
        self.assertEqual(mail_credentials.glm_endpoint({'glm_api_url': 'https://loliapi.org/v1',
            'glm_protocol': 'anthropic_messages'}), 'https://loliapi.org/v1/messages')
        for url in ('http://loliapi.org/v1', 'https://user:secret@loliapi.org/v1',
                    'https://loliapi.org/v1?key=secret', 'https://loliapi.org/v1#fragment'):
            with self.assertRaises(ValueError):
                mail_credentials.glm_endpoint({'glm_api_url': url})

    def test_deepseek_endpoint_and_provider_key_are_separated(self):
        self.assertEqual(mail_credentials.glm_endpoint({'glm_api_url': 'https://api.deepseek.com'}),
                         'https://api.deepseek.com/v1/chat/completions')
        self.assertEqual(mail_credentials.ai_provider({'glm_api_url': 'https://api.deepseek.com/v1'}), 'deepseek')
        with patch.dict(os.environ, {'PATCH_DEEPSEEK_API_KEY': 'deep-key', 'PATCH_GLM_API_KEY': 'old-key'}, clear=True):
            self.assertEqual(mail_credentials.ai_token({'glm_api_url': 'https://api.deepseek.com'}, {}), 'deep-key')
        with patch.dict(os.environ, {'PATCH_GLM_API_KEY': 'old-key'}, clear=True):
            self.assertEqual(mail_credentials.ai_token({'glm_api_url': 'https://api.deepseek.com'},
                                                       {'PATCH_GLM_API_KEY': 'stored-old-key'}), '')
        self.assertEqual(mail_credentials.ai_token({'glm_api_url': 'https://loliapi.org/v1'},
                                                   {'PATCH_GLM_API_KEY': 'stored-old-key'}), 'stored-old-key')
        with tempfile.TemporaryDirectory() as folder:
            config = copy.deepcopy(self.config)
            config.pop('glm_api_url')
            config.pop('glm_protocol')
            monitor.save_json(Path(folder) / 'config.json', config)
            self.assertEqual(monitor.load_config(Path(folder))['glm_api_url'], mail_credentials.OFFICIAL_GLM_URL)

    def test_custom_gateway_uses_selected_model_without_official_extensions(self):
        batch = self.batch()
        self.config.update(glm_api_url='https://loliapi.org/v1', glm_model='glm-5.3')
        answer = {'summary': '请补充说明', 'intent': 'review', 'action_items': [], 'reply_draft': ''}
        response = {'choices': [{'finish_reason': 'stop', 'message': {'content': '```json\n' + json.dumps(answer) + '\n```'}}]}
        with patch.object(monitor, 'post_json', return_value=response) as post:
            result = monitor.summarize(batch, self.state, self.config, 'synthetic-provider-key')
            self.assertEqual(post.call_args.args[0], 'https://loliapi.org/v1/chat/completions')
            request = post.call_args.args[1]
            self.assertEqual(request['model'], 'glm-5.3')
            self.assertNotIn('thinking', request)
            self.assertNotIn('response_format', request)
            self.assertEqual(post.call_args.args[3], 'synthetic-provider-key')
        self.assertEqual(result['summary'], answer['summary'])

    def test_messages_protocol_separates_system_and_extracts_text_only(self):
        batch = self.batch()
        self.config.update(glm_api_url='https://loliapi.org/v1', glm_protocol='anthropic_messages')
        answer = {'summary': '请补充说明', 'intent': 'review', 'action_items': [], 'reply_draft': ''}
        response = {'stop_reason': 'end_turn', 'content': [{'type': 'thinking', 'thinking': 'not an answer'},
                    {'type': 'text', 'text': json.dumps(answer)}]}
        with patch.object(monitor, 'post_json', return_value=response) as post:
            self.assertEqual(monitor.summarize(batch, self.state, self.config, 'fake')['summary'], answer['summary'])
            self.assertEqual(post.call_args.args[0], 'https://loliapi.org/v1/messages')
            self.assertIn('system', post.call_args.args[1])
            self.assertEqual(len(post.call_args.args[1]['messages']), 1)
        response['stop_reason'] = 'max_tokens'
        with patch.object(monitor, 'post_json', return_value=response):
            with self.assertRaises(monitor.ServiceFailure):
                monitor.summarize(batch, self.state, self.config, 'fake')

    @unittest.skipUnless(os.name == 'nt', 'Windows HTTPS fallback')
    def test_windows_tls_fallback_is_not_used_for_ambiguous_network_errors(self):
        for reason, fallback in ((ssl.SSLCertVerificationError('chain'), True), (TimeoutError('timeout'), False)):
            opener = Mock()
            opener.open.side_effect = urllib.error.URLError(reason)
            with patch.object(monitor.urllib.request, 'build_opener', return_value=opener), \
                    patch.object(monitor, 'windows_json', return_value={'ok': True}) as native, \
                    patch.dict(os.environ, {'PATCH_MONITOR_CA_FILE': ''}):
                if fallback:
                    self.assertEqual(monitor.post_json('https://loliapi.org/v1/chat/completions', {}, 'GLM', 'fake'), {'ok': True})
                    native.assert_called_once()
                else:
                    with self.assertRaises(monitor.ServiceFailure):
                        monitor.post_json('https://loliapi.org/v1/chat/completions', {}, 'GLM', 'fake')
                    native.assert_not_called()

    def test_native_transport_keeps_tokens_and_request_text_out_of_command_arguments(self):
        native_reply = Mock(returncode=0, stdout=json.dumps({'ok': True, 'status': 200, 'body': '{"choices": []}'}))
        with patch.object(api_transport.subprocess, 'run', return_value=native_reply) as run, \
                patch.object(api_transport.subprocess, 'CREATE_NO_WINDOW', 0, create=True):
            result = api_transport.windows_json('https://loliapi.org/v1/chat/completions', {'content': 'synthetic-private-text'}, 'synthetic-token')
            self.assertEqual(result, {'choices': []})
            arguments = run.call_args.args[0]
            self.assertNotIn('synthetic-token', ' '.join(arguments))
            self.assertNotIn('synthetic-private-text', ' '.join(arguments))
            self.assertEqual(json.loads(run.call_args.kwargs['input'])['token'], 'synthetic-token')
            self.assertIn('MaximumRedirection=0', api_transport.SCRIPT)

    @unittest.skipUnless(os.name == 'nt', 'Windows DPAPI only')
    def test_changing_provider_requires_its_key_before_replacing_saved_config(self):
        with tempfile.TemporaryDirectory() as folder:
            answers = iter(['', '', '', '', 'https://api.deepseek.com/v1', '', ''])
            with patch.object(mail_credentials, 'interactive_console', return_value=True), \
                    patch.object(mail_credentials, 'read_credentials', return_value={
                        'PATCH_IMAP_PASSWORD': 'synthetic-mail-password', 'PATCH_GLM_API_KEY': 'old-provider-key'}), \
                    patch('builtins.input', side_effect=lambda _: next(answers)), \
                    patch.object(mail_credentials.getpass, 'getpass', return_value=''), \
                    patch('builtins.print'), patch.object(monitor, 'save_json') as save:
                with self.assertRaisesRegex(RuntimeError, 'API Key'):
                    mail_credentials.interactive_config(Path(folder), self.config, save, lambda _: 'same-mailbox')
                save.assert_not_called()


if __name__ == '__main__':
    unittest.main(verbosity=2)
