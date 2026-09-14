import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import build_pages
import patch_status_dashboard as dashboard
from patch_analysis import analyze, commit_references
from applied_evidence import apply_verifications


def message(mid, subject, body, day=1, author=False, parent=None):
    return {'message_id': mid, 'subject': subject, 'body': body,
            'sender': dashboard.DEFAULT_EMAIL if author else 'Reviewer <reviewer@example.org>',
            'date_iso': f'2026-09-{day:02d}T10:00:00+00:00', 'date': f'2026-09-{day:02d}',
            'references': [parent] if parent else [], 'in_reply_to': [parent] if parent else [],
            'link': dashboard.lore_link(mid)}


def record(messages):
    return {'title': 'net: example', 'messages': messages, 'osv_candidates': []}


class AnalysisTests(unittest.TestCase):
    def setUp(self):
        self.original = message('v1', '[PATCH] net: example', 'Patch rationale.\nFixes: ' + 'a' * 40, author=True)

    def analyze(self, *replies):
        return analyze([self.original, *replies], dashboard.DEFAULT_EMAIL)

    def test_review_and_acceptance_are_independent(self):
        result = self.analyze(message('r', 'Re: [PATCH] net: example', 'Reviewed-by: R <r@example.org>', 2),
                              message('a', 'Re: [PATCH] net: example', 'This patch was applied to netdev/net.git (main)', 3))
        self.assertTrue(result['signals']['applied'])
        self.assertTrue(result['signals']['reviewed'])
        self.assertFalse(result['signals']['mainline'])

    def test_stable_failure_does_not_erase_acceptance(self):
        result = self.analyze(message('a', 'Re: [PATCH] net: example', 'Applied to net.', 2),
                              message('f', 'FAILED: patch "[PATCH] net: example" failed to apply to 6.1-stable tree',
                                      'The patch does not apply to the 6.1-stable tree.', 3))
        self.assertEqual(result['status'], 'Applied')
        self.assertEqual(result['attention_items'][0]['scope'], 'stable:6.1')
        self.assertEqual(result['attention_items'][0]['message_id'], 'f')

    def test_later_acceptance_resolves_same_branch_failure(self):
        result = self.analyze(message('f', 'Re: [PATCH] net: example', 'Not applied.', 2),
                              message('a', 'Re: [PATCH] net: example', 'Applied, thanks.', 3))
        self.assertEqual(result['status'], 'Applied')
        self.assertFalse(result['has_attention'])

    def test_new_version_keeps_old_review_and_feedback_in_history(self):
        result = self.analyze(message('r', 'Re: [PATCH] net: example', 'Please resend.\nReviewed-by: R <r@example.org>', 2, parent='v1'),
                              message('v2', '[PATCH v2] net: example', 'Updated patch.', 3, author=True))
        self.assertEqual(result['latest_version'], 2)
        self.assertFalse(result['has_attention'])
        self.assertFalse(result['signals']['reviewed'])
        self.assertTrue(any(e['kind'] == 'attention' for e in result['events']))

    def test_reply_with_changed_subject_inherits_parent_version(self):
        result = self.analyze(message('v2', '[PATCH v2] net: example', 'Updated patch.', 2, author=True),
                              message('a', 'Thanks', 'Applied, thanks.', 3, parent='v2'))
        self.assertTrue(result['signals']['applied'])
        self.assertEqual(result['events'][-1]['version'], 2)

    def test_hypothetical_quoted_and_code_text_do_not_imply_acceptance(self):
        self.original['body'] = 'This pattern is applied elsewhere.\n---\ndiff --git a/a b/a\n+/* queued for cleanup */'
        result = self.analyze(message('r', 'Re: [PATCH] net: example',
                                      '> Applied, thanks.\nIf your patch is applied to the wrong tree, tell us.\nThis patch can be applied directly.', 2))
        self.assertFalse(result['signals']['applied'])

    def test_real_ath12k_queue_description_is_not_acceptance(self):
        result = self.analyze(message('review', 'Re: [PATCH] net: example',
            'I propose a better commit message.\nAdd a check in the timeout worker so that if it was already\n'
            'queued before finish_queued was set, it exits without aborting a scan\nthat is already finishing.', 2))
        self.assertFalse(result['signals']['applied'])

    def test_completed_announcements_with_wrapped_destination(self):
        for body in ('Applied to\n\nhttps://git.kernel.org/tree for-next', 'Applied now. Thanks.',
                     'This series was applied to netdev/net.git (main)', 'Queued for fixes, thanks!'):
            with self.subTest(body=body):
                self.assertTrue(self.analyze(message('a', 'Re: [PATCH] net: example', body, 2))['signals']['applied'])

    def test_suggestions_and_other_work_are_not_acceptance(self):
        for body in ('This patch can be applied to net directly.', 'applied elsewhere.',
                     'queued for freeing', 'The other implementation was merged into driver-core-next already.'):
            with self.subTest(body=body):
                self.assertFalse(self.analyze(message('a', 'Re: [PATCH] net: example', body, 2))['signals']['applied'])

    def proof(self, **changes):
        proof = {'sha': 'b' * 40, 'title': 'net: example', 'mainline_ancestor': True,
                 'merge_base': 'b' * 40, 'head_sha': 'c' * 40, 'behind_by': 0,
                 'author_email': dashboard.DEFAULT_EMAIL, 'author': 'Runyu Xiao',
                 'authored_by_user': True, 'signed_off_by_user': True,
                 'committed_at': '2026-09-03T10:00:00Z', 'checked_at': '2026-09-14T10:00:00Z',
                 'source': 'https://github.com/torvalds/linux/commit/' + 'b' * 40}
        proof.update(changes)
        return {'author_email': dashboard.DEFAULT_EMAIL, 'head_sha': 'c' * 40, 'commits': [proof]}

    def verified_record(self, snapshot):
        row = {**record([self.original]), **self.analyze()}
        return apply_verifications([row], snapshot, dashboard.DEFAULT_EMAIL)[0]

    def test_mainline_proof_restores_patch_without_applied_mail(self):
        row = self.verified_record(self.proof())
        self.assertTrue(row['signals']['applied'])
        self.assertTrue(row['signals']['mainline'])
        self.assertEqual(row['acceptance_basis'], 'mainline_verified')
        self.assertIsNone(row['events'][-1]['message_id'])

    def test_foreign_authored_same_title_is_not_our_acceptance(self):
        row = self.verified_record(self.proof(author_email='other@example.org', authored_by_user=False, signed_off_by_user=False))
        self.assertFalse(row['signals']['applied'])
        self.assertEqual(row['related_mainline_commits'][0]['attribution'], 'other_author')

    def test_signed_off_contribution_is_labeled_separately(self):
        row = self.verified_record(self.proof(author_email='other@example.org', authored_by_user=False))
        self.assertTrue(row['signals']['applied'])
        self.assertEqual(row['mainline_commits'][0]['attribution'], 'signed_off')

    def test_unproven_or_unrelated_commit_cannot_change_status(self):
        for change in ({'mainline_ancestor': False}, {'merge_base': 'd' * 40}, {'behind_by': 1},
                       {'head_sha': 'd' * 40}, {'title': 'net: another patch'}):
            with self.subTest(change=change):
                self.assertFalse(self.verified_record(self.proof(**change))['signals']['applied'])

    def test_mainline_proof_preserves_stable_attention(self):
        failure = message('f', 'FAILED: patch "net: example" failed to apply to 6.1-stable tree',
                          'The patch does not apply to the 6.1-stable tree.', 4)
        row = {**record([self.original, failure]), **self.analyze(failure)}
        apply_verifications([row], self.proof(), dashboard.DEFAULT_EMAIL)
        self.assertTrue(row['signals']['mainline'])
        self.assertEqual(row['attention_items'][0]['scope'], 'stable:6.1')

    def test_stable_submission_has_its_own_scope_and_is_not_mainline(self):
        result = self.analyze(message('s', '[PATCH 6.1 1/1] net: example', 'Stable review patch.', 2))
        self.assertEqual(result['kind'], 'patch')
        self.assertFalse(result['signals']['mainline'])

    def test_commit_roles_and_short_kernel_links(self):
        refs = commit_references([self.original, message('a', 'Re: [PATCH] net: example',
                    'Applied.\nhttps://git.kernel.org/netdev/net/c/' + 'b' * 12 + '\n[ Upstream commit ' + 'c' * 40 + ' ]', 2)])
        self.assertEqual({r['role'] for r in refs}, {'introduced_by', 'patch_commit', 'upstream_fix'})

    def test_backport_submission_updates_failure_without_claiming_acceptance(self):
        result = self.analyze(message('f', 'FAILED: patch "net: example" failed to apply to 6.1-stable tree',
                                      'The patch does not apply to the 6.1-stable tree.', 2),
                              message('b', '[PATCH 6.1.y 1/1] net: example', 'Backport for stable.', 3))
        self.assertEqual(result['attention_items'][0]['reason'], 'backport_pending')
        self.assertEqual(result['attention_items'][0]['message_id'], 'b')
        self.assertFalse(result['signals']['applied'])

    def test_merge_failure_preserves_every_message_and_alias(self):
        one = record([self.original, message('a', 'Re: [PATCH] net: example', 'Applied.\ncommit ' + 'b' * 40, 2)])
        failure = {'id': 'old-failure-id', 'title': 'net: example" failed to apply to 6.1-stable tree',
                   'messages': [message('f', 'FAILED: patch "[PATCH] net: example" failed to apply to 6.1-stable tree',
                                        'commit ' + 'b' * 40, 3)]}
        records = dashboard.enrich_records([one, failure])
        self.assertEqual(len(records), 1)
        self.assertEqual(len(records[0]['messages']), 3)
        self.assertIn('old-failure-id', records[0]['aliases'])

    def test_change_history_survives_unchanged_import_and_body_completion_counts(self):
        first = dashboard.build_payload('one', [record([self.original])], 1)
        updated = record([self.original, message('r', 'Re: [PATCH] net: example', 'Reviewed-by: R <r@example.org>', 2)])
        second = dashboard.build_payload('two', [updated], 2, previous=first)
        third = dashboard.build_payload('three', [updated], 2, previous=second)
        self.assertEqual(third['check_result'], 'unchanged')
        self.assertEqual(third['last_changes'], second['last_changes'])
        completed = copy.deepcopy(updated)
        completed['messages'][1]['body'] += '\nAdditional context.'
        fourth = dashboard.build_payload('four', [completed], 2, previous=third)
        self.assertEqual(fourth['last_import']['updated_messages'], 1)

    def test_osv_errors_are_not_empty_success_or_verified_fix(self):
        rows = [record([self.original, message('a', 'Re: [PATCH] net: example', 'Applied.\ncommit ' + 'b' * 40, 2)])]
        with patch.object(dashboard, 'osv_candidates', return_value={'b' * 40: {'state': 'error', 'error': 'timeout', 'candidates': []}}) as query:
            payload = dashboard.build_payload('one', rows, 2, online_cves=True)
        self.assertEqual(query.call_args.args[0], ['b' * 40])
        self.assertEqual(payload['records'][0]['cve_state'], 'error')

    def test_unchanged_check_publishes_only_small_status_file(self):
        with tempfile.TemporaryDirectory() as root:
            source, site = Path(root) / 'local.html', Path(root) / 'docs/index.html'
            initial = dashboard.build_payload('one', [record([self.original])], 1)
            dashboard.save_report(source, initial)
            with patch.object(build_pages, 'SITE', site):
                build_pages.build(report=source)
                before = site.read_bytes()
                initial['last_checked_at'] = '2026-09-14T18:00:00+08:00'
                initial['revision'] = 'new-check'
                initial['check_result'] = 'unchanged'
                dashboard.save_report(source, initial)
                build_pages.build(report=source)
                self.assertEqual(before, site.read_bytes())
                sync = json.loads(site.with_name('sync-status.json').read_text(encoding='utf-8'))
                self.assertEqual(sync['last_checked_at'], initial['last_checked_at'])
                build_pages.build(sync_error='Human verification required')
                self.assertEqual(before, site.read_bytes())
                self.assertEqual(json.loads(site.with_name('sync-status.json').read_text(encoding='utf-8'))['check_result'], 'error')


if __name__ == '__main__':
    unittest.main(verbosity=2)
