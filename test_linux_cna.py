import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import linux_cna as c


class OfficialIndexTests(unittest.TestCase):
    def test_severity_thresholds_and_missing(self):
        for score, level in [(0,'none'),(.1,'low'),(3.9,'low'),(4,'medium'),(6.9,'medium'),(7,'high'),(8.9,'high'),(9,'critical'),(10,'critical'),(11,'unknown'),(True,'unknown'),(None,'unknown')]:
            value={'containers':{'cna':{'metrics':[{'cvssV3_1':{'baseScore':score}}]}}}
            self.assertEqual(c.severity_record(value,'source')['level'],level)
        self.assertEqual(c.severity_record({},'source')['level'],'unknown')

    def test_severity_prefers_cna_and_preserves_adp(self):
        value={'containers':{'cna':{'metrics':[{'cvssV3_1':{'baseScore':5}}]},
            'adp':[{'providerMetadata':{'shortName':'CISA'},'metrics':[{'cvssV4_0':{'baseScore':9.5}}]}]}}
        result=c.severity_record(value,'source')
        self.assertEqual((result['role'],result['score']),('CNA',5))
        self.assertEqual(len(result['ratings']),2)
        del value['containers']['cna']
        self.assertEqual(c.severity_record(value,'source')['level'],'critical')

    def test_rating_refresh_failure_preserves_previous_rating(self):
        import cve_checks
        snapshot={'records':{'patch':{'matches':[{'id':'CVE-2026-12345','commit':'a'*40,
            'severity':{'level':'high','score':7.8}}]}},'completed_at':'original-fix-check'}
        with patch.object(cve_checks,'read_store',return_value=snapshot), patch.object(cve_checks,'save') as save, patch.object(c,'cvelist_check',side_effect=RuntimeError()):
            self.assertEqual(cve_checks.refresh_severity(),1)
        result=save.call_args.args[0]
        match=result['records']['patch']['matches'][0]
        self.assertEqual(match['severity']['score'],7.8)
        self.assertEqual(match['severity_error'],'RuntimeError')
        self.assertEqual(result['completed_at'],'original-fix-check')

    def test_original_fix_backport_and_json_boundary_only(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(c, 'CACHE', Path(tmp)):
            path=Path(tmp)/'cve'/'published'/'2026'/'CVE-2026-12345.json'
            path.parent.mkdir(parents=True)
            value={'cveMetadata':{'cveId':path.stem,'state':'PUBLISHED'},'containers':{'cna':{
                'references':[{'url':'https://git.kernel.org/stable/c/'+'d'*40}],
                'affected':[{'product':'Linux','repo':'https://git.kernel.org/stable/linux.git',
                  'versions':[{'version':'e'*40,'lessThan':'c'*40,'versionType':'git','status':'affected'},
                              {'version':'e'*40,'lessThanOrEqual':'f'*40,'versionType':'git','status':'affected'}]}]}}}
            path.write_text(json.dumps(value))
            path.with_suffix('.sha1').write_text('a'*40+'\n'+'b'*40+'\n')
            path.with_suffix('.dyad').write_text('# dyad version\n1:'+'e'*40+':2:'+'b'*40+'\n')
            result=c.parse_record(path,'revision')
            self.assertEqual({f['sha'] for f in result['fixes']},{'a'*40,'b'*40,'c'*40})

    def test_rejected_directory_overrides_old_json(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(c,'CACHE',Path(tmp)):
            path=Path(tmp)/'cve'/'rejected'/'2026'/'CVE-2026-12345.json'
            self.assertEqual(c.parse_record(path,'revision')['state'],'REJECTED')

    def test_source_failure_is_not_no_hit(self):
        import cve_checks
        from test_cve_checks import record
        with patch.object(c,'update_source',side_effect=RuntimeError('network')):
            result=cve_checks.check([record()])
        self.assertEqual(result['records']['a']['state'],'error')
        self.assertIsNone(result['completed_at'])

    def test_incremental_deletion_removes_old_association(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            index=root/'index.json'
            old={'schema':1,'revision':'old','entries':{
                'CVE-2026-12345':{'state':'PUBLISHED','fixes':[{'sha':'a'*40}]},
                'CVE-2026-12346':{'state':'PUBLISHED','fixes':[]}}}
            index.write_text(json.dumps(old))
            def git(*args):
                return 'cve/published/2026/CVE-2026-12345.json' if args[0]=='diff' else '2026-09-15'
            with patch.object(c,'CACHE',root),patch.object(c,'INDEX',index),patch.object(c,'git',side_effect=git):
                result=c.build_index('new')
            self.assertNotIn('CVE-2026-12345',result['entries'])
            self.assertIn('CVE-2026-12346',result['entries'])


if __name__=='__main__':
    unittest.main()
