import copy
import unittest

import patch_status_dashboard as dashboard
from patch_series import build_series
from test_analysis import message


def rows(*messages):
    return dashboard.enrich_records([{'title': dashboard.clean_subject(m['subject']), 'messages': [m]} for m in messages])


def numbered(mid, position, title, ver=1, day=1, parent=None, author=True):
    return message(mid, f'[PATCH v{ver} {position}] {title}', 'Patch rationale.\n---\ndiff --git a/a b/a',
                   day, author, parent)


class SeriesTests(unittest.TestCase):
    def series(self, *messages):
        records = rows(*messages)
        return records, build_series(records, dashboard.DEFAULT_EMAIL)

    def test_cover_and_children_group_in_number_order_without_changing_status(self):
        records = rows(numbered('c', '0/2', 'net: a series'),
                       numbered('b', '2/2', 'net: second', parent='c'),
                       numbered('a', '1/2', 'net: first', parent='c'))
        before = copy.deepcopy(records)
        series = build_series(records, dashboard.DEFAULT_EMAIL)
        self.assertEqual(len(series), 1)
        self.assertTrue(series[0]['complete'])
        self.assertEqual([m['message_id'] for m in series[0]['revisions'][0]['members']], ['a', 'b'])
        self.assertEqual([(r['status'], r['signals'], r['messages']) for r in records],
                         [(r['status'], r['signals'], r['messages']) for r in before])
        self.assertTrue(all(r['series_ids'] == [series[0]['id']] for r in records))

    def test_same_numbers_and_similar_titles_in_separate_threads_stay_separate(self):
        _, series = self.series(numbered('c1', '0/2', 'net: first series'),
            numbered('c2', '0/2', 'net: second series'),
            numbered('a1', '1/2', 'net: first change', parent='c1'),
            numbered('a2', '1/2', 'net: first change elsewhere', parent='c2'))
        self.assertEqual(len(series), 2)
        self.assertTrue(all(s['counts']['present'] == 1 and not s['complete'] for s in series))

    def test_renamed_cover_with_linked_revisions_and_members_is_one_family(self):
        _, series = self.series(numbered('c1', '0/2', 'nvmet: original heading'),
            numbered('a1', '1/2', 'nvmet: first', parent='c1'), numbered('b1', '2/2', 'nvmet: second', parent='c1'),
            numbered('c2', '0/3', 'nvmet: updated heading', 2, 2, 'c1'),
            numbered('new', '1/3', 'fs: helper', 2, 2, 'c2'),
            numbered('a2', '2/3', 'nvmet: first', 2, 2, 'c2'), numbered('b2', '3/3', 'nvmet: second', 2, 2, 'c2'))
        self.assertEqual(len(series), 1)
        s = series[0]
        self.assertEqual(s['title'], 'nvmet: updated heading')
        self.assertEqual(s['latest_version'], 2)
        self.assertEqual(len(s['cover_record_ids']), 2)
        self.assertEqual([v['total'] for v in s['revisions']], [2, 3])
        self.assertEqual(s['counts']['present'], 3)

    def test_reply_chain_only_does_not_join_unrelated_new_series(self):
        _, series = self.series(numbered('c1', '0/2', 'net: old series'),
            numbered('a', '1/2', 'net: old child', parent='c1'),
            numbered('c2', '0/2', 'net: unrelated series', 2, 2, 'c1'),
            numbered('b', '1/2', 'net: unrelated child', 2, 2, 'c2'))
        self.assertEqual(len(series), 2)

    def test_complete_mainline_children_resolve_series_but_not_cover_commit(self):
        records = rows(numbered('c', '0/2', 'net: grouped'), numbered('a', '1/2', 'net: one', parent='c'),
                       numbered('b', '2/2', 'net: two', parent='c'))
        for r in records:
            if r['kind'] == 'patch':
                r['signals']['applied'] = True
                r['acceptance_basis'] = 'mainline_verified'
        series = build_series(records, dashboard.DEFAULT_EMAIL)
        self.assertEqual(series[0]['state'], 'mainline')
        self.assertEqual(series[0]['counts']['mainline'], 2)
        self.assertFalse(next(r for r in records if r['kind'] == 'cover')['signals']['applied'])

    def test_missing_or_duplicate_member_cannot_make_series_complete(self):
        for extra in ([], [numbered('duplicate', '1/2', 'net: different one', parent='c')]):
            records = rows(numbered('c', '0/2', 'net: incomplete'),
                           numbered('a', '1/2', 'net: one', parent='c'), *extra)
            for r in records:
                r['signals']['applied'] = True
                r['acceptance_basis'] = 'mainline_verified'
            series = build_series(records, dashboard.DEFAULT_EMAIL)
            self.assertFalse(series[0]['complete'])
            self.assertEqual(series[0]['state'], 'partial')
            self.assertEqual(series[0]['revisions'][0]['missing_numbers'], [2])

    def test_stable_mail_and_review_replies_are_not_series_members(self):
        records, series = self.series(numbered('c', '0/2', 'net: actual'),
            numbered('a', '1/2', 'net: one', parent='c'),
            message('stable', '[PATCH 6.1 2/2] net: two', 'Backport', 2, True, 'c'),
            message('reply', 'Re: [PATCH 2/2] net: review', 'Review reply', 2, False, 'c'),
            numbered('foreign', '2/2', 'net: foreign', parent='c', author=False))
        self.assertEqual(series[0]['counts']['present'], 1)
        self.assertEqual(sum(bool(r['series_ids']) for r in records), 2)

    def test_shared_missing_cover_and_coverless_chain_can_group(self):
        _, missing = self.series(numbered('a', '1/2', 'net: one', parent='missing'),
                                numbered('b', '2/2', 'net: two', parent='missing'))
        self.assertEqual(len(missing), 1)
        self.assertEqual(missing[0]['cover_record_ids'], [])
        _, chained = self.series(numbered('a', '1/2', 'net: one'), numbered('b', '2/2', 'net: two', parent='a'))
        self.assertEqual(len(chained), 1)

    def test_numbered_isolated_submission_does_not_invent_a_group(self):
        records, series = self.series(numbered('a', '1/2', 'net: one'))
        self.assertEqual(series, [])
        self.assertEqual(records[0]['series_ids'], [])

    def test_unreasonable_header_counts_remain_individual_records(self):
        records, series = self.series(numbered('c', '0/1000000000', 'net: malformed cover'),
                                     numbered('a', '1/1000000000', 'net: malformed member', parent='c'))
        self.assertEqual(series, [])
        self.assertEqual(len(records), 2)

    def test_latest_members_and_reverts_control_progress_not_historical_members(self):
        records = rows(numbered('c1', '0/3', 'net: series'),
            numbered('a1', '1/3', 'net: one', parent='c1'), numbered('b1', '2/3', 'net: two', parent='c1'),
            numbered('old', '3/3', 'net: removed member', parent='c1'),
            numbered('c2', '0/2', 'net: series', 2, 2),
            numbered('a2', '1/2', 'net: one', 2, 2, 'c2'), numbered('b2', '2/2', 'net: two', 2, 2, 'c2'))
        for r in records:
            r['signals']['applied'] = r['title'] == 'net: one'
            r['signals']['reverted'] = r['title'] == 'net: two'
        series = build_series(records, dashboard.DEFAULT_EMAIL)
        self.assertEqual(series[0]['counts']['total'], 2)
        self.assertEqual(len(series[0]['member_record_ids']), 3)
        self.assertEqual(series[0]['counts']['reverted'], 1)
        self.assertEqual(series[0]['state'], 'partial')
        self.assertEqual(build_series(records, dashboard.DEFAULT_EMAIL), series)


if __name__ == '__main__':
    unittest.main(verbosity=2)
