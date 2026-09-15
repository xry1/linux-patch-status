import copy
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import build_pages
import mail_monitor
import patch_status_dashboard as dashboard
import revision_reminders as reminders
from test_analysis import message
from test_series import numbered, rows


class RevisionReminderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        original = message('v1', '[PATCH net] e1000e: example', 'original', day=14, author=True)
        original['date_iso'] = '2026-09-14T22:20:14+08:00'
        review = message('review', 'Re: [PATCH net] e1000e: example', 'Please adjust.', day=15, parent='v1')
        self.payload = dashboard.upgrade_payload({'records': rows(original, review), 'author_email': dashboard.DEFAULT_EMAIL})
        self.payload.update(private_mailbox=True, hosting_mode='mail_monitor')
        self.available = reminders.targets(self.payload)
        self.target = next(iter(self.available.values()))

    def tearDown(self):
        self.temp.cleanup()

    def schedule(self, hours=24):
        return reminders.action(self.payload, self.directory, {'action': 'schedule', 'target_id': self.target['id'],
                                  'source_id': self.target['source_id'], 'hours': hours})['items'][0]

    def process(self, when, send, payload=None):
        with patch.object(reminders, 'clock', return_value=datetime.fromisoformat(when)):
            reminders.process_due(payload or self.payload, self.directory, send)

    def test_uses_exact_original_date_and_timezone_not_review_time(self):
        item = self.schedule()
        self.assertEqual(item['submitted_at'], '2026-09-14T14:20:14+00:00')
        self.assertEqual(item['due_at'], '2026-09-15T14:20:14+00:00')
        self.assertEqual(item['version'], 1)
        self.assertEqual(item['message_id'], 'v1')

    def test_no_automatic_task_from_review_or_reviewed_by(self):
        self.assertEqual(reminders.view(self.payload, self.directory)['items'], [])
        send = Mock()
        self.process('2026-09-16T00:00:00+00:00', send)
        send.assert_not_called()
        self.assertFalse((self.directory / reminders.STORE).exists())

    def test_never_early_and_sends_once_at_or_after_deadline(self):
        item = self.schedule()
        send = Mock()
        self.process('2026-09-15T14:20:13+00:00', send)
        send.assert_not_called()
        def checked_send(value):
            self.assertEqual(value['id'], item['id'])
            self.assertEqual(reminders.read(self.directory)['items'][item['id']]['status'], 'sending')
        send.side_effect = checked_send
        self.process('2026-09-15T14:20:14+00:00', send)
        self.process('2026-09-15T23:00:00+00:00', send)
        send.assert_called_once()
        self.assertEqual(reminders.read(self.directory)['items'][item['id']]['status'], 'sent')

    def test_scheduling_twice_is_one_reminder_and_completed_delivery_cannot_rearm(self):
        self.schedule()
        self.schedule()
        self.assertEqual(len(reminders.read(self.directory)['items']), 1)
        self.process('2026-09-16T00:00:00+00:00', Mock())
        with self.assertRaises(ValueError):
            self.schedule()

    def test_new_version_cancels_old_reminder_without_arming_next(self):
        item = self.schedule()
        updated = copy.deepcopy(self.payload)
        updated['records'][0]['messages'].append(message('v2', '[PATCH net v2] e1000e: example', 'new', day=15, author=True))
        updated = dashboard.upgrade_payload(updated)
        send = Mock()
        self.process('2026-09-16T00:00:00+00:00', send, updated)
        send.assert_not_called()
        data = reminders.read(self.directory)['items']
        self.assertEqual(data[item['id']]['status'], 'superseded')
        self.assertEqual(len(data), 1)

    def test_cancel_and_done_are_persistent(self):
        for operation, status in (('cancel', 'cancelled'), ('done', 'done')):
            item = self.schedule()
            reminders.action(self.payload, self.directory, {'action': operation, 'id': item['id']})
            send = Mock()
            self.process('2026-09-16T00:00:00+00:00', send)
            send.assert_not_called()
            self.assertEqual(reminders.read(self.directory)['items'][item['id']]['status'], status)

    def test_crash_or_uncertain_delivery_never_automatically_resends(self):
        item = self.schedule()
        with self.assertRaises(KeyboardInterrupt):
            self.process('2026-09-16T00:00:00+00:00', Mock(side_effect=KeyboardInterrupt()))
        send = Mock()
        self.process('2026-09-16T00:05:00+00:00', send)
        send.assert_not_called()
        self.assertEqual(reminders.read(self.directory)['items'][item['id']]['status'], 'uncertain')
        reminders.action(self.payload, self.directory, {'action': 'retry', 'id': item['id']})
        self.process('2026-09-16T00:10:00+00:00', send)
        send.assert_called_once()

    def test_definite_rejection_has_three_attempts_and_sanitized_errors(self):
        item = self.schedule()
        send = Mock(side_effect=mail_monitor.ServiceFailure('飞书', 'synthetic-private-token', uncertain=False))
        for _ in range(5):
            self.process('2026-09-16T00:00:00+00:00', send)
        self.assertEqual(send.call_count, 3)
        data = reminders.read(self.directory)
        self.assertEqual(data['items'][item['id']]['status'], 'failed')
        self.assertNotIn('synthetic-private-token', json.dumps(data))

    def test_series_is_one_target_and_waits_from_last_member(self):
        mails = [numbered('c', '0/2', 'net: series', day=14),
                 numbered('a', '1/2', 'net: one', day=14, parent='c'),
                 numbered('b', '2/2', 'net: two', day=14, parent='c')]
        mails[-1]['date_iso'] = '2026-09-14T10:00:05+00:00'
        p = dashboard.upgrade_payload({'records': rows(*mails), 'author_email': dashboard.DEFAULT_EMAIL})
        available = reminders.targets(p)
        self.assertEqual(len(available), 1)
        target = next(iter(available.values()))
        self.assertEqual(target['kind'], 'series')
        self.assertEqual(target['submitted_at'], '2026-09-14T10:00:05+00:00')

    def test_no_guess_when_submission_has_only_day_or_foreign_author(self):
        for change in ({'date_iso': '2026-09-14'}, {'sender': 'Other <other@example.org>'}):
            p = copy.deepcopy(self.payload)
            p['records'][0]['messages'][0].update(change)
            self.assertEqual(reminders.targets(p), {})

    def test_acceptance_ends_active_reminder(self):
        item = self.schedule()
        updated = copy.deepcopy(self.payload)
        updated['records'][0]['signals']['applied'] = True
        send = Mock()
        self.process('2026-09-16T00:00:00+00:00', send, updated)
        send.assert_not_called()
        self.assertEqual(reminders.read(self.directory)['items'][item['id']]['status'], 'done')

    def test_validates_stale_source_and_hours_without_mutating_state(self):
        self.schedule()
        before = (self.directory / reminders.STORE).read_bytes()
        for update in ({'source_id': 'stale'}, {'hours': 0}, {'hours': 169}, {'hours': True}, {'target_id': []}):
            request = {'action': 'schedule', 'target_id': self.target['id'], 'source_id': self.target['source_id'], 'hours': 24}
            request.update(update)
            with self.assertRaises(ValueError):
                reminders.action(self.payload, self.directory, request)
            self.assertEqual((self.directory / reminders.STORE).read_bytes(), before)

    def test_publication_rejects_reminder_data_even_without_mailbox_flags(self):
        public = copy.deepcopy(self.payload)
        public.pop('private_mailbox')
        public.pop('hosting_mode')
        public['revision_reminders'] = reminders.view(self.payload, self.directory)
        source = self.directory / 'source.html'
        dashboard.save_report(source, public)
        with patch.object(build_pages, 'SITE', self.directory / 'index.html'):
            with self.assertRaisesRegex(ValueError, 'private'):
                build_pages.build(report=source)

    def test_local_http_schedule_cancel_and_cross_origin_rejection(self):
        report = self.directory / 'report.html'
        dashboard.save_report(report, self.payload)
        state = dashboard.DashboardState(self.directory / 'public.html', None, dashboard.DEFAULT_EMAIL)
        with patch.object(dashboard, 'monitor_report_path', return_value=report):
            server = dashboard.make_server(state, 0)
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            base = f'http://127.0.0.1:{server.server_port}'
            try:
                def post(value, extra=None):
                    headers = {'Content-Type': 'application/json', 'X-Patch-Reminder': '1'}
                    headers.update(extra or {})
                    req = urllib.request.Request(base + '/api/mail-monitor/reminders', data=json.dumps(value).encode(), headers=headers)
                    with urllib.request.urlopen(req, timeout=5) as response:
                        return json.load(response)
                request = {'action': 'schedule', 'target_id': self.target['id'], 'source_id': self.target['source_id'], 'hours': 24}
                for extra in ({'Origin': 'https://example.org'}, {'X-Patch-Reminder': '0'}):
                    with self.assertRaises(urllib.error.HTTPError) as exc:
                        post(request, extra)
                    self.assertEqual(exc.exception.code, 403)
                result = post(request)
                self.assertEqual(result['items'][0]['status'], 'scheduled')
                with urllib.request.urlopen(base + '/api/mail-monitor') as response:
                    self.assertEqual(json.load(response)['revision_reminders']['items'][0]['due_at'], '2026-09-15T14:20:14+00:00')
                result = post({'action': 'cancel', 'id': result['items'][0]['id']})
                self.assertEqual(result['items'][0]['status'], 'cancelled')
            finally:
                server.shutdown()
                server.server_close()
                worker.join()


if __name__ == '__main__':
    unittest.main()
