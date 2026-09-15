import unittest
from datetime import datetime
import cve_checks as c


def record(identifier='a', applied=True, kind='patch', sha='a'*40):
    return {'id': identifier, 'kind': kind, 'signals': {'applied': applied},
            'mainline_commits': [{'sha': sha}], 'commit_refs': [
                {'sha': 'b'*40, 'role': 'introduced_by'},
                {'sha': 'c'*40, 'role': 'referenced_commit'}]}


class Checks(unittest.TestCase):
    def test_only_accepted_children_and_repair_hashes(self):
        self.assertEqual(c.targets([record(), record('b', False), record('cover', kind='cover')]), {'a':['a'*40]})

    def test_missing_is_not_empty_result(self):
        result = c.check([record(sha='abc123')], lambda _: self.fail('must not query'))
        self.assertEqual(result['records']['a']['state'], 'missing_commit')

    def test_deduplicate_commits_and_affected_candidates(self):
        calls=[]
        def query(sha):
            calls.append(sha)
            return sha, {'state':'checked', 'candidates':[{'id':'CVE-2026-12345'}]}
        records=[record(), record('b')]
        snapshot=c.check(records, query)
        c.overlay(records, snapshot)
        self.assertEqual(len(calls), 1)
        self.assertEqual(records[0]['cve_state'], 'candidate')

    def test_failure_and_stale_hash(self):
        records=[record()]
        snapshot=c.check(records, lambda s:(s, {'state':'error','candidates':[]}))
        self.assertIsNone(snapshot['completed_at'])
        c.overlay(records, snapshot)
        self.assertEqual(records[0]['cve_state'], 'error')
        records[0]['mainline_commits'][0]['sha']='d'*40
        c.overlay(records, snapshot)
        self.assertEqual(records[0]['cve_state'], 'unqueried')

    def test_weekly_calendar_guard(self):
        old={'completed_at':'2026-09-15T16:00:00+08:00'}
        self.assertFalse(c.due(old, datetime.fromisoformat('2026-09-21T09:00:00+08:00')))
        self.assertTrue(c.due(old, datetime.fromisoformat('2026-09-22T09:00:00+08:00')))


if __name__ == '__main__':
    unittest.main()
