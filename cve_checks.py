"""Applied-only OSV checks. Affected-commit hits are NOT repair attribution."""
import argparse
import copy
import json
import os
import re
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if ROOT.name == 'outputs':
    ROOT = ROOT.parent / 'github-pages'
STORE = ROOT / 'applied_cve_checks.json'


def targets(records):
    result = {}
    for r in records:
        if r.get('kind') != 'patch' or not r.get('signals', {}).get('applied'):
            continue
        hashes = [c['sha'] for c in r.get('mainline_commits', []) if not c.get('reverted')]
        hashes += [c['sha'] for c in r.get('commit_refs', [])
                   if c.get('role') in {'patch_commit', 'upstream_fix', 'backport_commit'}]
        result[r['id']] = sorted({s.lower() for s in hashes if re.fullmatch('[0-9a-fA-F]{40}', s)})
    return result


def read_store():
    try:
        return json.loads(STORE.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return {}


def due(snapshot, now):
    # Schedule by local calendar date so a 09:00 weekly run is not delayed a day
    # by the afternoon time of the initial manual query.
    last = snapshot.get('completed_at')
    return not last or now.astimezone().date() >= datetime.fromisoformat(last).astimezone().date() + timedelta(days=7)


def query_commit(sha):
    from api_transport import windows_json
    found = {}
    token = None
    seen = set()
    try:
        while True:
            request = {'commit': sha}
            if token:
                request['page_token'] = token
            response = windows_json('https://api.osv.dev/v1/query', request)
            if not isinstance(response, dict) or not isinstance(response.get('vulns', []), list):
                raise ValueError('Unexpected OSV response')
            for item in response.get('vulns', []):
                identifier = item['id']
                found[identifier] = {'id': identifier, 'commit': sha,
                    'summary': item.get('summary', ''),
                    'source': 'https://osv.dev/vulnerability/' + identifier}
            token = response.get('next_page_token')
            if not token:
                break
            if token in seen:
                raise ValueError('Repeated OSV page')
            seen.add(token)
        return sha, {'state': 'checked', 'candidates': list(found.values())}
    except Exception as exc:
        return sha, {'state': 'error', 'error': type(exc).__name__, 'candidates': list(found.values())}


def check(records, query=query_commit):
    selected = targets(records)
    hashes = sorted({s for group in selected.values() for s in group})
    with ThreadPoolExecutor(max_workers=3) as pool:
        responses = dict(pool.map(query, hashes))
    at = datetime.now(timezone.utc).isoformat(timespec='seconds')
    entries = {}
    for identifier, shas in selected.items():
        errors = [s for s in shas if responses[s]['state'] == 'error']
        entries[identifier] = {'at': at, 'commits': shas, 'errors': errors,
            'state': 'missing_commit' if not shas else 'error' if errors else 'checked',
            'candidates': [c for s in shas for c in responses[s]['candidates']]}
    return {'schema': 1, 'attempted_at': at,
            'completed_at': at if all(v['state'] != 'error' for v in entries.values()) else None,
            'source': 'OSV affected-commit query; not a CVE repair-association search',
            'records': entries, 'unique_commits': len(hashes)}


def save(snapshot):
    fd, name = tempfile.mkstemp(dir=STORE.parent, prefix='.cve-', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(snapshot, f, ensure_ascii=False, indent=2)
            f.write('\n')
        os.replace(name, STORE)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def overlay(records, snapshot=None):
    snapshot = read_store() if snapshot is None else snapshot
    selected = targets(records)
    for r in records:
        entry = snapshot.get('records', {}).get(r['id'])
        if r['id'] not in selected:
            continue
        if not entry or entry['commits'] != selected[r['id']]:
            r['osv_query'] = {}
            r['osv_candidates'] = []
            r['cve_state'] = 'candidate' if r.get('cves_in_mail') else 'unqueried'
            continue
        r['osv_query'] = {k: v for k, v in entry.items() if k != 'candidates'}
        r['osv_candidates'] = entry['candidates']
        r['cve_state'] = ('candidate' if entry['state'] == 'checked' and
                          (entry['candidates'] or r.get('cves_in_mail')) else entry['state'])
    return records


def decorate(payload):
    payload = copy.deepcopy(payload)
    snapshot = read_store()
    overlay(payload['records'], snapshot)
    if set(targets(payload['records'])) & set(snapshot.get('records', {})):
        payload['revision'] = payload.get('revision', '').split(':cve:')[0] + ':cve:' + snapshot.get('attempted_at', '')
    return payload


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--if-due', action='store_true')
    args = parser.parse_args()
    old = read_store()
    if args.if_due and not due(old, datetime.now().astimezone()):
        print('not_due: weekly Applied-only OSV check')
        return 0
    import mail_monitor
    import patch_status_dashboard as dashboard
    payload = dashboard.upgrade_payload(mail_monitor.load_seed())
    result = check(payload['records'])
    if not result['completed_at']:
        result['completed_at'] = old.get('completed_at')
    # Keep the prior successful evidence for failed requests, explicitly dated.
    for key, entry in result['records'].items():
        prior = old.get('records', {}).get(key)
        if entry['state'] == 'error' and prior and prior['commits'] == entry['commits']:
            entry['previous_check'] = prior.get('previous_check', prior) if prior['state'] == 'error' else prior
    save(result)
    from collections import Counter
    print(json.dumps({'applied_patches': len(result['records']),
                      'unique_commits': result['unique_commits'],
                      'states': dict(Counter(e['state'] for e in result['records'].values())),
                      'candidate_patches': sum(bool(e['candidates']) for e in result['records'].values())}, ensure_ascii=False))
    return int(any(e['state'] == 'error' for e in result['records'].values()))


if __name__ == '__main__':
    raise SystemExit(main())
