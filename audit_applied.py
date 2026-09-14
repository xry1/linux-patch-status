"""Write a reviewable calibration report; never change the source dashboard."""
import argparse
import json
from datetime import datetime
from pathlib import Path

import patch_status_dashboard as dashboard


def audit(source, output):
    before = dashboard.load_payload(source)
    after = dashboard.upgrade_payload(before)
    previous = {r['id']: r for r in before['records']}
    rows = []
    for r in after['records']:
        old = previous.get(r['id'], {})
        rows.append({
            'id': r['id'], 'title': r['title'], 'kind': r['kind'],
            'before_applied': bool(old.get('signals', {}).get('applied')),
            'after_applied': r['signals']['applied'], 'basis': r['acceptance_basis'],
            'mainline_commits': r['mainline_commits'],
            'mail_confirmations': [e for e in r['events'] if e['kind'] == 'applied'],
            'excluded_other_authors': r['related_mainline_commits'],
            'thread_url': 'https://xry1.github.io/linux-patch-status/#patch=' + r['id'],
        })
    result = {
        'calibrated_at': datetime.now().astimezone().isoformat(timespec='seconds'),
        'before_applied': sum(x['before_applied'] for x in rows),
        'after_applied': sum(x['after_applied'] for x in rows),
        'added': [x['id'] for x in rows if x['after_applied'] and not x['before_applied']],
        'removed': [x['id'] for x in rows if x['before_applied'] and not x['after_applied']],
        'scope': 'All archived topics; cover letters are counted separately from patch topics in applied_audit.',
        'applied_audit': after['applied_audit'], 'records': rows,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix('.json.tmp')
    temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temporary.replace(output)
    print(f"Applied: {result['before_applied']} -> {result['after_applied']}; +{len(result['added'])}, -{len(result['removed'])}")
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--output', type=Path, default=Path(__file__).with_name('docs') / 'applied-audit.json')
    args = parser.parse_args()
    audit(args.report, args.output)
