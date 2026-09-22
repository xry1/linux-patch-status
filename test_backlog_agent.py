import copy
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

import backlog_agent as agent
import build_pages
import mail_monitor
import patch_status_dashboard as dashboard
from test_analysis import message
from test_series import numbered, rows


class BacklogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.original = message('v1', '[PATCH net] driver: example', 'Patch rationale.', author=True)
        self.review = message('r1', 'Re: [PATCH net] driver: example', 'Please explain the error path and update the commit message.', day=2, parent='v1')
        self.payload = self.payload_for(self.original, self.review)
        self.config = {**mail_monitor.DEFAULTS, 'glm_api_url': 'https://gateway.example/v1/chat/completions', 'glm_model': 'glm-test'}
        self.post = Mock(side_effect=lambda *args: self.response())

    def tearDown(self):
        self.temp.cleanup()

    def payload_for(self, *messages):
        return dashboard.upgrade_payload({'records': rows(*messages), 'author_email': dashboard.DEFAULT_EMAIL, 'private_mailbox': True})

    def response(self, **updates):
        value = {'status': 'needs_work', 'summary': '归档未见修改后的投递；需补充说明。', 'priority': 'normal',
                 'action_items': ['核对失败路径并完善提交说明。'], 'reply_draft': 'I will clarify the commit message.', 'evidence_ids': ['r1']}
        value.update(updates)
        return {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(value)}}]}

    def process(self, payload=None, limit=5):
        return agent.process(payload or self.payload, self.directory, self.config, 'synthetic', self.post, limit)

    def item(self, payload=None):
        return agent.view(payload or self.payload, self.directory)['items'][0]

    def action(self, action, **extra):
        item = self.item()
        return agent.action(self.payload, self.directory, {'action': action, 'id': item['id'], 'fingerprint': item['fingerprint'], **extra})

    def test_unanalyzed_discussion_is_not_claimed_as_unanswered(self):
        self.assertEqual(self.item()['status'], 'unassessed')
        self.assertNotIn(self.item()['status'], agent.ACTIVE)

    def test_explicit_retry_preserves_completed_and_reopens_exhausted_error(self):
        self.process()
        before = agent.read(self.directory)
        agent.process(self.payload, self.directory, self.config, 'synthetic', self.post, retry_failed=True)
        self.assertEqual(self.post.call_count, 1)
        self.assertEqual(agent.read(self.directory)['entries'], before['entries'])
        with agent.transaction(self.directory) as data:
            entry = next(iter(data['entries'].values()))
            entry.update(ai_state='error', attempts=3, error='old error')
        agent.process(self.payload, self.directory, self.config, 'synthetic', self.post, retry_failed=True)
        self.assertEqual(self.post.call_count, 2)
        entry = next(iter(agent.read(self.directory)['entries'].values()))
        self.assertEqual(entry['ai_state'], 'complete')
        self.assertEqual(entry['retry_history'][-1]['attempts'], 3)
        self.assertEqual(self.post.call_args.args[1]['max_tokens'], 8192)

    def test_acknowledgment_only_does_not_need_llm_or_reply(self):
        p = self.payload_for(self.original, message('ack', 'Re: [PATCH net] driver: example', 'Thanks!\nReviewed-by: R <r@example.org>', day=2, parent='v1'))
        self.process(p)
        self.post.assert_not_called()
        self.assertEqual(self.item(p)['status'], 'waiting')

    def test_authors_ack_does_not_close_requested_work_and_full_context_sent(self):
        reply = message('ack', 'Re: [PATCH net] driver: example', 'Thanks, I will do that.', day=3, author=True, parent='r1')
        self.process(self.payload_for(self.original, self.review, reply))
        request = self.post.call_args.args[1]
        context = json.loads(request['messages'][-1]['content'])
        self.assertEqual([m['message_id'] for m in context['messages']], ['v1', 'r1', 'ack'])
        self.assertTrue(context['messages'][-1]['is_author'])
        self.assertIn('历史私信', context['coverage'])

    def test_new_version_replaces_older_feedback_but_new_late_review_is_analyzed(self):
        v2 = message('v2', '[PATCH net v2] driver: example', 'Revised.', day=3, author=True)
        p = self.payload_for(self.original, self.review, v2)
        self.process(p)
        self.post.assert_not_called()
        self.assertEqual(self.item(p)['status'], 'waiting')
        late = message('late', 'Re: [PATCH net] driver: example', 'Could you explain this?', day=4, parent='v1')
        self.process(self.payload_for(self.original, self.review, v2, late))
        self.post.assert_called_once()

    def test_applied_series_is_not_a_mail_todo_even_with_late_feedback(self):
        accepted = message('accept', 'Re: [PATCH net] driver: example', 'Applied to net.', day=3)
        p = self.payload_for(self.original, self.review, accepted)
        self.process(p)
        self.assertEqual(agent.topics(p), {})
        self.post.assert_not_called()
        late = message('late', 'Re: [PATCH net] driver: example', 'Please send a follow-up to fix this regression.', day=4)
        late_payload = self.payload_for(self.original, self.review, accepted, late)
        self.assertEqual(agent.topics(late_payload), {})
        self.process(late_payload)
        self.post.assert_not_called()

    def test_applied_series_is_excluded_from_mail_todo(self):
        accepted = message('accept', 'Re: [PATCH net] driver: example', 'Applied to net.', day=3)
        p = self.payload_for(self.original, self.review, accepted)
        self.assertEqual(agent.topics(p), {})
        report = agent.view(p, self.directory)
        self.assertEqual(report['items'], [])
        self.assertEqual(report['counts'], {label: 0 for label in agent.LABELS})
        agent.process(p, self.directory, self.config, 'synthetic', self.post, limit=5)
        self.post.assert_not_called()

    def test_acceptance_mail_with_followup_request_is_not_silently_closed(self):
        accepted = message('accept', 'Re: [PATCH net] driver: example', 'Applied to net.\nPlease add tests in a follow-up.', day=3)
        p = self.payload_for(self.original, self.review, accepted)
        self.assertEqual(agent.topics(p), {})

    def test_series_is_one_task_including_review_of_member(self):
        p = self.payload_for(numbered('c', '0/2', 'net: series'), numbered('a', '1/2', 'net: one', parent='c'),
                             numbered('b', '2/2', 'net: two', parent='c'),
                             message('r1', 'Re: [PATCH 1/2] net: one', 'Please explain.', day=2, parent='a'))
        self.assertEqual(len(agent.topics(p)), 1)
        self.process(p)
        self.assertEqual(len(agent.view(p, self.directory)['items']), 1)

    def test_cache_ignores_read_status_but_invalidates_new_mail_or_body_change(self):
        self.process()
        p = copy.deepcopy(self.payload)
        p['last_checked_at'] = 'later'
        self.process(p)
        self.post.assert_called_once()
        p['records'][0]['messages'][-1]['body'] += '\nPlease confirm.'
        self.process(p)
        self.assertEqual(self.post.call_count, 2)

    def test_done_is_persistent_and_new_external_feedback_reopens(self):
        self.process()
        self.action('done')
        self.process()
        self.assertEqual(self.item()['status'], 'done')
        reply = message('own', 'Re: [PATCH net] driver: example', 'I will review.', day=3, author=True, parent='r1')
        p = self.payload_for(self.original, self.review, reply)
        self.assertEqual(self.item(p)['status'], 'done')
        p = self.payload_for(self.original, self.review, message('next', 'Re: [PATCH net] driver: example', 'Any update?', day=4, parent='r1'))
        self.assertNotEqual(self.item(p)['status'], 'done')
        self.process(p)
        self.assertEqual(self.item(p)['status'], 'needs_work')

    def test_ignore_snooze_reopen_and_stale_request(self):
        self.process()
        self.action('ignore')
        self.assertEqual(self.item()['status'], 'ignored')
        self.action('reopen')
        self.assertEqual(self.item()['status'], 'needs_work')
        with patch.object(agent, 'clock', return_value=datetime.fromisoformat('2026-09-15T08:00:00+00:00')):
            self.action('snooze')
            self.assertEqual(self.item()['status'], 'snoozed')
        with patch.object(agent, 'clock', return_value=datetime.fromisoformat('2026-09-16T08:00:00+00:00')):
            self.assertEqual(self.item()['status'], 'needs_work')
        before = (self.directory / agent.STORE).read_bytes()
        with self.assertRaises(ValueError):
            agent.action(self.payload, self.directory, {'action': 'done', 'id': self.item()['id'], 'fingerprint': 'stale'})
        self.assertEqual((self.directory / agent.STORE).read_bytes(), before)

    def test_model_evidence_must_exist_and_action_cites_external_message(self):
        for updates in ({'evidence_ids': ['invented']}, {'evidence_ids': ['v1']}, {'status': 'done'}, {'action_items': 'bad'}):
            post = Mock(return_value=self.response(**updates))
            with self.assertRaises(ValueError):
                agent.analyze(next(iter(agent.topics(self.payload).values())), self.config, 'fake', post)

    def test_budget_retry_errors_and_no_secret_logging(self):
        self.config['backlog_max_calls_per_day'] = 2
        self.post.side_effect = RuntimeError('private-secret-body')
        for _ in range(5):
            self.process()
        self.assertEqual(self.post.call_count, 2)
        self.assertNotIn('private-secret-body', (self.directory / agent.STORE).read_text(encoding='utf-8'))

    def test_model_cannot_mutate_authoritative_state_or_network_tools(self):
        original = copy.deepcopy(self.payload)
        self.process()
        self.assertEqual(self.payload, original)
        self.assertNotIn('tools', self.post.call_args.args[1])
        self.assertNotIn('response_format', self.post.call_args.args[1])

    def test_null_daily_limit_continues_past_recorded_usage_and_keeps_counting(self):
        day = agent.clock().astimezone(agent.SHANGHAI).date().isoformat()
        with agent.transaction(self.directory) as data:
            data['usage'][day] = 10000
        self.config['backlog_max_calls_per_day'] = None
        self.process()
        self.post.assert_called_once()
        self.assertEqual(agent.read(self.directory)['usage'][day], 10001)
        self.assertEqual(self.item()['status'], 'needs_work')

    def test_continuous_mode_finishes_more_than_cycle_budget_and_retains_retry_bound(self):
        self.config.update(backlog_max_calls_per_day=None, backlog_max_calls_per_cycle=1)
        self.post.side_effect = [RuntimeError('temporary'), self.response()]
        progress = Mock()
        agent.process(self.payload, self.directory, self.config, 'fake', self.post, continuous=True, progress=progress)
        self.assertEqual(self.post.call_count, 2)
        self.assertEqual(self.item()['status'], 'needs_work')
        self.assertEqual(progress.call_count, 2)
        self.action('reanalyze')
        self.post.reset_mock()
        self.post.side_effect = RuntimeError('persistent')
        agent.process(self.payload, self.directory, self.config, 'fake', self.post, continuous=True)
        self.assertEqual(self.post.call_count, 3)

    def test_continuous_mode_still_honors_explicit_daily_quota(self):
        self.config['backlog_max_calls_per_day'] = 1
        self.post.side_effect = RuntimeError('temporary')
        agent.process(self.payload, self.directory, self.config, 'fake', self.post, continuous=True)
        self.post.assert_called_once()

    def test_zero_daily_limit_disables_calls_and_invalid_limit_fails_before_calling(self):
        self.config['backlog_max_calls_per_day'] = 0
        self.process()
        self.post.assert_not_called()
        for value in (-1, True, 'unlimited'):
            self.config['backlog_max_calls_per_day'] = value
            with self.assertRaises(ValueError):
                self.process()
        self.post.assert_not_called()

    def test_inline_review_keeps_quoted_metadata_needed_for_version_request(self):
        review = copy.deepcopy(self.review)
        review['body'] = '> Assisted-by: LLM Codex\nPlease add the version.\n> diff --git a/private b/private\n> omitted code\nThe diff looks good.'
        self.process(self.payload_for(self.original, review))
        context = json.loads(self.post.call_args.args[1]['messages'][-1]['content'])
        text = context['messages'][-1]['text']
        self.assertIn('> Assisted-by: LLM Codex\nPlease add the version.', text)
        self.assertNotIn('diff --git', text)
        self.assertNotIn('omitted code', text)
        self.assertIn('The diff looks good.', text)

    def test_ui_update_during_model_request_preserves_user_override(self):
        def model(*args):
            self.action('done')
            return self.response()
        self.post.side_effect = model
        self.process()
        self.assertEqual(self.item()['status'], 'done')

    def test_long_context_is_bounded_and_missing_evidence_cannot_slip_through(self):
        mails = [self.original] + [message('r' + str(n), 'Re: [PATCH net] driver: example', 'review ' * 5000, day=2, parent='v1') for n in range(50)]
        topic = next(iter(agent.topics(self.payload_for(*mails)).values()))
        captured = []
        def model(url, request, *args):
            context = json.loads(request['messages'][-1]['content'])
            captured.append(context)
            return self.response(evidence_ids=[context['messages'][-1]['message_id']])
        result = agent.analyze(topic, self.config, 'fake', model)
        self.assertTrue(result['context_limited'])
        self.assertLessEqual(sum(len(m['text']) for m in captured[0]['messages']), 28000)
        self.assertLessEqual(len(captured[0]['messages']), 36)

    def test_messages_protocol_and_safe_correspondence_draft(self):
        self.config['glm_protocol'] = 'anthropic_messages'
        value = self.response(reply_draft='I will describe a failure injection setup.')['choices'][0]['message']['content']
        self.post.side_effect = lambda *args: {'stop_reason': 'end_turn', 'content': [{'type': 'text', 'text': value}]}
        self.process()
        self.assertIn('system', self.post.call_args.args[1])
        self.assertNotIn('failure injection', self.item()['reply_draft'])

    def test_correspondence_advice_does_not_expand_failure_trigger_setup(self):
        self.post.side_effect = lambda *args: self.response(action_items=['说明 request_irq() 注入方式。', '核对实际使用的模型版本。'])
        self.process()
        actions = self.item()['action_items']
        self.assertNotIn('request_irq', actions[0])
        self.assertEqual(actions[1], '核对实际使用的模型版本。')

    def send_at(self, time, send):
        with patch.object(agent, 'clock', return_value=datetime.fromisoformat(time)):
            return agent.send_digest(self.payload, self.directory, self.config, send)

    def test_daily_digest_not_early_and_once_per_shanghai_date(self):
        self.process()
        send = Mock()
        self.assertEqual(self.send_at('2026-09-16T00:59:59+00:00', send)['status'], 'not_due')
        self.assertEqual(self.send_at('2026-09-16T01:00:00+00:00', send)['status'], 'sent')
        self.send_at('2026-09-16T16:10:00+00:00', send)  # next date, before 9
        self.send_at('2026-09-16T12:00:00+00:00', send)
        send.assert_called_once()
        self.send_at('2026-09-17T01:00:00+00:00', send)
        self.assertEqual(send.call_count, 2)
        self.assertIn('同步已过期', send.call_args.args[0])

    def test_start_tomorrow_and_no_digest_for_done_task(self):
        self.process()
        agent.register_schedule(self.directory, '2026-09-16', 'linux-patch')
        send = Mock()
        self.send_at('2026-09-15T08:00:00+00:00', send)
        send.assert_not_called()
        self.action('done')
        self.assertEqual(self.send_at('2026-09-16T01:00:00+00:00', send)['status'], 'empty')
        send.assert_not_called()

    def test_ignored_unanalyzed_discussion_does_not_keep_sending_progress(self):
        self.action('ignore')
        self.assertEqual(agent.view(self.payload, self.directory)['pending_analysis'], 0)
        send = Mock()
        self.assertEqual(self.send_at('2026-09-16T01:00:00+00:00', send)['status'], 'empty')
        send.assert_not_called()

    def test_claim_before_send_crash_never_resends(self):
        self.process()
        def crash(text):
            self.assertEqual(agent.read(self.directory)['digests']['2026-09-16']['status'], 'sending')
            raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.send_at('2026-09-16T01:00:00+00:00', crash)
        send = Mock()
        self.assertEqual(self.send_at('2026-09-16T01:05:00+00:00', send)['status'], 'uncertain')
        send.assert_not_called()

    def test_definite_failure_three_attempts_but_ambiguous_not_retried(self):
        self.process()
        send = Mock(side_effect=mail_monitor.ServiceFailure('飞书', 'denied', uncertain=False))
        for _ in range(5):
            result = self.send_at('2026-09-16T01:00:00+00:00', send)
        self.assertEqual(send.call_count, 3)
        self.assertEqual(result['status'], 'failed')
        send = Mock(side_effect=TimeoutError())
        for _ in range(2):
            result = self.send_at('2026-09-17T01:00:00+00:00', send)
        self.assertEqual(send.call_count, 1)
        self.assertEqual(result['status'], 'uncertain')

    def test_publish_rejects_backlog_even_without_mailbox_marker(self):
        p = copy.deepcopy(self.payload)
        p.pop('private_mailbox')
        p['backlog_agent'] = {}
        source = self.directory / 'source.html'
        dashboard.save_report(source, p)
        with patch.object(build_pages, 'SITE', self.directory / 'public.html'):
            with self.assertRaisesRegex(ValueError, 'private'):
                build_pages.build(report=source)

    def test_local_api_actions_and_cross_origin_rejection(self):
        self.process()
        report = self.directory / 'report.html'
        dashboard.save_report(report, self.payload)
        state = dashboard.DashboardState(self.directory / 'public.html', None, dashboard.DEFAULT_EMAIL)
        with patch.object(dashboard, 'monitor_report_path', return_value=report):
            server = dashboard.make_server(state, 0)
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            base = f'http://127.0.0.1:{server.server_port}'
            try:
                def post(extra=None):
                    item = self.item()
                    headers = {'Content-Type': 'application/json', 'X-Patch-Backlog': '1', **(extra or {})}
                    req = urllib.request.Request(base + '/api/mail-monitor/backlog', headers=headers,
                        data=json.dumps({'action': 'done', 'id': item['id'], 'fingerprint': item['fingerprint']}).encode())
                    with urllib.request.urlopen(req, timeout=5) as response:
                        return json.load(response)
                for headers in ({'Origin': 'https://example.org'}, {'X-Patch-Backlog': '0'}):
                    with self.assertRaises(urllib.error.HTTPError) as exc:
                        post(headers)
                    self.assertEqual(exc.exception.code, 403)
                self.assertEqual(post()['items'][0]['status'], 'done')
                with urllib.request.urlopen(base + '/api/mail-monitor') as response:
                    self.assertEqual(json.load(response)['backlog_agent']['items'][0]['status'], 'done')
            finally:
                server.shutdown()
                server.server_close()
                worker.join()


if __name__ == '__main__':
    unittest.main()
