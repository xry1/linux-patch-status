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
        result = c.check_osv([record(sha='abc123')], lambda _: self.fail('must not query'))
        self.assertEqual(result['records']['a']['state'], 'missing_commit')

    def test_deduplicate_commits_and_affected_candidates(self):
        calls=[]
        def query(sha):
            calls.append(sha)
            return sha, {'state':'checked', 'candidates':[{'id':'CVE-2026-12345'}]}
        records=[record(), record('b')]
        snapshot=c.check_osv(records, query)
        c.overlay(records, snapshot)
        self.assertEqual(len(calls), 1)
        self.assertEqual(records[0]['cve_state'], 'unqueried')
        self.assertFalse(records[0]['cve_matches'])

    def test_failure_and_stale_hash(self):
        records=[record()]
        snapshot=c.check(records, {'revision':'test','entries':{}}, lambda *args: self.fail('no match'))
        snapshot['records']['a']['state']='error'
        snapshot['completed_at']=None
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
        old['source_error']='network'
        self.assertTrue(c.due(old, datetime.fromisoformat('2026-09-16T09:00:00+08:00')))

    def test_explicit_fix_and_second_source_required(self):
        index={'revision':'test','entries':{'CVE-2026-12345':{'id':'CVE-2026-12345','title':'test',
            'state':'PUBLISHED','source':'official','fixes':[{'sha':'a'*40,'source':'proof'}]}}}
        checked=c.check([record()], index, lambda *args:{'state':'PUBLISHED','matching_fixed':['a'*40]})
        self.assertEqual(checked['records']['a']['state'],'confirmed')
        disagreement=c.check([record()], index, lambda *args:{'state':'REJECTED','matching_fixed':[]})
        self.assertEqual(disagreement['records']['a']['state'],'pending_review')
        self.assertFalse(disagreement['records']['a']['matches'])
        index['entries']['CVE-2026-12345']['state']='REJECTED'
        rejected=c.check([record()], index, lambda *args:self.fail('rejected must not match'))
        self.assertEqual(rejected['records']['a']['state'],'unmatched')

    def test_non_applied_cannot_keep_confirmed_state(self):
        r=record(applied=False)
        r['cve_matches']=[{'id':'CVE-2026-12345'}]
        c.overlay([r], {'schema':2,'records':{}})
        self.assertEqual(r['cve_matches'],[])


if __name__ == '__main__':
    unittest.main()
