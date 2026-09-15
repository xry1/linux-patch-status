import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import linux_cna as c


class OfficialIndexTests(unittest.TestCase):
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
