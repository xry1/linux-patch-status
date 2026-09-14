import copy
import imaplib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import mail_monitor as m
from test_mail_monitor import FakeIMAP, raw_mail
from test_analysis import message
from test_series import rows


class FolderIMAP:
    def __init__(self, folders, listing=None):
        self.folders = folders
        self.listing = listing or [f'() "/" "{name}"'.encode() for name in folders]
        self.current = None
        self.selections = []
        self.status_calls = []
        self.searches = []
        self.fail_status = set()
        self.fail_body = set()

    def list(self):
        return 'OK', self.listing

    def select(self, name, readonly=False):
        name = name[1:-1].replace('\\"', '"').replace('\\\\', '\\')
        self.selections.append((name, readonly))
        self.current = name
        return 'OK', [str(len(self.folders[name].mails)).encode()]

    def status(self, name, fields):
        name = name[1:-1]
        self.status_calls.append(name)
        if name in self.fail_status:
            raise imaplib.IMAP4.error('synthetic-private-server-detail')
        folder = self.folders[name]
        return 'OK', [f'"{name}" (UIDVALIDITY {folder.validity} UIDNEXT {folder.next_uid})'.encode()]

    def response(self, name):
        return self.folders[self.current].response(name)

    def uid(self, command, *args):
        if command == 'SEARCH':
            self.searches.append((self.current, args))
        if command == 'FETCH' and 'HEADER.FIELDS' not in args[1] and self.current in self.fail_body:
            return 'NO', [b'synthetic-private-server-detail']
        return self.folders[self.current].uid(command, *args)

    def logout(self):
        pass


class FolderMonitorTests(unittest.TestCase):
    def setUp(self):
        self.config = copy.deepcopy(m.DEFAULTS)
        self.seed = m.dashboard.upgrade_payload({'records': rows(message('root', '[PATCH] net: example', 'Context', author=True))})
        self.state = m.new_state(self.config)
        self.state.update(initialized_at='2026-09-14T09:00:00+08:00', uidvalidity=1, cursor=10,
                          last_checked_at='2026-09-14T09:05:00+08:00')

    def scan(self, client):
        additions = m.collect_folders(client, self.config, self.seed, self.state)
        m.enqueue(additions, m.combined_payload(self.seed, self.state, self.config), self.state, self.config)
        return additions

    def test_discovers_nested_folders_and_excludes_special_use_and_aliases(self):
        listing = [b'() "/" "INBOX"', b'(\\Sent) "/" "sent-custom"', b'() "/" "sent-custom/child"',
                   b'(\\Drafts) "/" "draft-custom"', b'(\\Trash) "/" "trash-custom"',
                   b'(\\Junk) "/" "junk-custom"', b'() "/" "&XfJT0ZAB-"',
                   b'(\\Noselect) "/" "patch"', b'() "/" "patch/C0125"', b'() "/" "&YhF2hA-/C0125"',
                   (b'() "/" {11}', b'patch space'), b'() NIL "quoted\\"name"']
        inventory = m.discover_folders(FolderIMAP({}, listing), self.config)
        for name in ('sent-custom', 'sent-custom/child', 'draft-custom', 'trash-custom', 'junk-custom', '&XfJT0ZAB-', 'patch'):
            self.assertTrue(inventory[name]['excluded_reason'], name)
        for name in ('INBOX', 'patch/C0125', '&YhF2hA-/C0125', 'patch space', 'quoted"name'):
            self.assertFalse(inventory[name]['excluded_reason'], name)
        self.assertEqual(inventory['&YhF2hA-/C0125']['name'], '我的/C0125')
        self.assertEqual(m.quote_folder('quoted"name'), '"quoted\\"name"')

    def test_custom_exclusions_cover_children_and_never_read_bodies(self):
        self.config['folder_excludes'] = ['private']
        client = FolderIMAP({'INBOX': FakeIMAP(), 'private': FakeIMAP({1: raw_mail('x')}),
                             'private/sub': FakeIMAP({1: raw_mail('y')})})
        self.assertEqual(self.scan(client), [])
        self.assertEqual(client.status_calls, ['INBOX'])
        self.assertEqual(self.state['folder_scan']['excluded'], 2)

    def test_migration_preserves_inbox_cursor_and_backfills_original_start_time(self):
        client = FolderIMAP({'INBOX': FakeIMAP(next_uid=11),
                             'C0125': FakeIMAP({3: raw_mail('missed')}),
                             'older': FakeIMAP({3: raw_mail('old')}, internal='14-Sep-2026 08:00:00 +0800')})
        self.assertEqual([x['message_id'] for x in self.scan(client)], ['missed'])
        self.assertEqual(self.state['folders']['INBOX']['cursor'], 10)
        self.assertEqual(self.state['initialized_at'], '2026-09-14T09:00:00+08:00')
        self.assertEqual(self.state['messages']['missed']['mailbox_folder'], 'C0125')
        self.assertEqual(len(self.state['batches']), 1)
        self.assertEqual(client.folders['older'].body_fetches, [])
        self.assertTrue(all(readonly for _, readonly in client.selections))
        self.assertTrue(all('UNSEEN' not in args and 'SINCE' in args for _, args in client.searches))

    def test_moved_and_copied_mail_is_not_notified_twice_and_new_folder_is_discovered(self):
        client = FolderIMAP({'C0125': FakeIMAP({1: raw_mail('reply')})})
        self.assertEqual(len(self.scan(client)), 1)
        renamed = FolderIMAP({'new/path': FakeIMAP({41: raw_mail('reply')})})
        self.assertEqual(self.scan(renamed), [])
        self.assertFalse(self.state['folders']['C0125']['present'])
        self.assertEqual(renamed.folders['new/path'].body_fetches, [])
        self.assertEqual(len(self.state['batches']), 1)

    def test_reply_in_earlier_folder_matches_own_submission_in_later_folder(self):
        own = raw_mail('new-root', '[PATCH] e1000e: new work', self.config['author_email'], None)
        reply = raw_mail('reply', 'Changed heading', parent='new-root')
        client = FolderIMAP({'A-replies': FakeIMAP({1: reply}), 'Z-patches': FakeIMAP({1: own})})
        self.assertEqual({x['message_id'] for x in self.scan(client)}, {'new-root', 'reply'})
        self.assertEqual(next(iter(self.state['batches'].values()))['message_ids'], ['reply'])

    def test_failed_folder_keeps_cursor_and_other_folders_continue(self):
        client = FolderIMAP({'A': FakeIMAP({2: raw_mail('failed')}), 'B': FakeIMAP({3: raw_mail('ok')})})
        client.fail_body.add('A')
        self.assertEqual([x['message_id'] for x in self.scan(client)], ['ok'])
        self.assertEqual(self.state['folders']['A']['cursor'], 0)
        self.assertEqual(self.state['folder_scan']['failed'], 1)
        self.assertEqual(self.state['last_checked_at'], '2026-09-14T09:05:00+08:00')
        self.assertNotIn('synthetic-private-server-detail', json.dumps(self.state))
        client.fail_body.clear()
        self.assertEqual([x['message_id'] for x in self.scan(client)], ['failed'])
        self.assertEqual(self.state['folder_scan']['failed'], 0)
        self.assertEqual(self.state['last_error'], '')

    def test_uid_reset_only_rescans_that_folder_and_deduplicates_messages(self):
        client = FolderIMAP({'A': FakeIMAP({5: raw_mail('old')}), 'B': FakeIMAP({2: raw_mail('other')})})
        self.scan(client)
        client.folders['A'] = FakeIMAP({1: raw_mail('old'), 2: raw_mail('new')}, validity=2)
        client.selections.clear()
        self.assertEqual([x['message_id'] for x in self.scan(client)], ['new'])
        self.assertEqual(self.state['folders']['A']['cursor_resets'], 1)
        self.assertNotIn('B', [name for name, _ in client.selections])

    def test_global_budget_and_per_folder_limit_make_progress_without_skipping(self):
        self.config.update(batch_limit=2, folder_batch_limit=1)
        client = FolderIMAP({name: FakeIMAP({i: raw_mail(name+str(i)) for i in range(1, 4)}) for name in ('A', 'B', 'C')})
        for _ in range(6):
            self.scan(client)
            self.assertLessEqual(self.state['folder_scan']['headers_checked'], 2)
        self.assertEqual(len(self.state['messages']), 9)
        self.assertEqual(self.state['folder_scan']['pending'], 0)

    def test_unchanged_folders_only_use_status_without_opening_them(self):
        client = FolderIMAP({'A': FakeIMAP({1: raw_mail('a')})})
        self.scan(client)
        client.selections.clear()
        self.assertEqual(self.scan(client), [])
        self.assertEqual(client.selections, [])
        self.assertEqual(self.state['folders']['A']['phase'], 'unchanged')

    def test_copied_history_with_rewritten_arrival_date_is_not_notified(self):
        old = raw_mail('old').replace(b'14 Sep 2026', b'10 Sep 2026')
        client = FolderIMAP({'new': FakeIMAP({1: old})})
        self.assertEqual(self.scan(client), [])
        self.assertEqual(client.folders['new'].body_fetches, [])

    def test_new_install_sets_one_shared_baseline_and_skips_history(self):
        self.state = m.new_state(self.config)
        client = FolderIMAP({'A': FakeIMAP({1: raw_mail('old')}), 'B': FakeIMAP({2: raw_mail('old2')})})
        with patch.object(m, 'now', return_value='2026-09-14T12:00:00+08:00'):
            self.assertEqual(self.scan(client), [])
        self.assertEqual(self.state['initialized_at'], '2026-09-14T12:00:00+08:00')
        self.assertEqual(self.state['batches'], {})

    def test_list_failure_preserves_previous_inventory(self):
        client = FolderIMAP({'A': FakeIMAP()})
        self.scan(client)
        before = copy.deepcopy(self.state)
        client.listing = [b'malformed list']
        with self.assertRaises(RuntimeError):
            self.scan(client)
        self.assertEqual(self.state, before)

    def test_cursor_and_outbox_transaction_survives_failed_enqueue(self):
        original = copy.deepcopy(self.state)
        client = FolderIMAP({'C0125': FakeIMAP({1: raw_mail('reply')})})
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
                'PATCH_IMAP_PASSWORD': 'fake', 'PATCH_GLM_API_KEY': 'fake', 'PATCH_FEISHU_WEBHOOK': 'fake'}), \
                patch.object(m, 'enqueue', side_effect=ValueError('failed')):
            with self.assertRaises(ValueError):
                m.run_once(Path(directory), self.config, self.state, self.seed, connect=lambda *_: client)
        self.assertEqual(self.state, original)


if __name__ == '__main__':
    unittest.main()
