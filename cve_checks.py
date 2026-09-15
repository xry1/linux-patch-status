"""Applied-only official Linux CNA fix matching; OSV remains separate context."""
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
        # A hash classified elsewhere as an introducing/reference commit must
        # not be promoted by an ambiguous stable-mail parser classification.
        excluded = {c['sha'].lower() for c in r.get('commit_refs', [])
                    if c.get('role') == 'introduced_by'}
        hashes = [s for s in hashes if not any(s.lower().startswith(x) for x in excluded)]
        hashes += [c['sha'] for c in r.get('maintainer_commits', []) if c.get('verified')]
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
    if snapshot.get('source_error') or any(e.get('state') == 'error' for e in snapshot.get('records', {}).values()):
        return True
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


def check_osv(records, query=query_commit):
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


def check(records, index=None, crosscheck=None):
    import linux_cna
    at = datetime.now(timezone.utc).isoformat(timespec='seconds')
    selected = targets(records)
    entries = {key: {'at': at, 'commits': shas, 'state': 'missing_commit' if not shas else 'unmatched',
                     'matches': [], 'candidates': [], 'errors': []} for key, shas in selected.items()}
    result = {'schema': 2, 'attempted_at': at, 'completed_at': None, 'records': entries,
              'unique_commits': len({s for shas in selected.values() for s in shas})}
    try:
        if index is None:
            index = linux_cna.build_index(linux_cna.update_source())
        reverse = linux_cna.reverse_index(index)
        result['official_source'] = {'repository': linux_cna.REPOSITORY, 'revision': index['revision'],
            'committed_at': index.get('source_committed_at', ''),
            'published_records': sum(e['state'] == 'PUBLISHED' for e in index['entries'].values()),
            'rejected_records': sum(e['state'] == 'REJECTED' for e in index['entries'].values())}
        crosscheck = crosscheck or linux_cna.cvelist_check
        groups = {}
        for shas in selected.values():
            for sha in shas:
                for match in reverse.get(sha, []):
                    groups.setdefault(match['id'], set()).add(sha)
        corroboration = {}
        def verify(item):
            identifier, shas = item
            try:
                return identifier, crosscheck(identifier, shas)
            except Exception as exc:
                return identifier, {'state': 'error', 'error': type(exc).__name__}
        with ThreadPoolExecutor(max_workers=3) as pool:
            corroboration.update(pool.map(verify, groups.items()))
        for identifier, shas in selected.items():
            entry = entries[identifier]
            seen = set()
            for sha in shas:
                for match in reverse.get(sha, []):
                    key = (match['id'], sha)
                    if key in seen:
                        continue
                    seen.add(key)
                    second = corroboration[match['id']]
                    evidence = {**match, 'cvelist': second}
                    if second.get('state') == 'PUBLISHED' and sha in second.get('matching_fixed', []):
                        entry['matches'].append(evidence)
                    else:
                        entry['candidates'].append(evidence)
                        if second.get('state') == 'error':
                            entry['errors'].append(match['id'] + ': ' + second['error'])
            if entry['errors']:
                entry['state'] = 'error'
            elif entry['candidates']:
                entry['state'] = 'pending_review'
            elif entry['matches']:
                entry['state'] = 'confirmed'
        if not any(e['errors'] for e in entries.values()):
            result['completed_at'] = at
    except Exception as exc:
        result['source_error'] = type(exc).__name__ + ': ' + str(exc)[:180]
        for entry in entries.values():
            if entry['commits']:
                entry['state'] = 'error'
                entry['errors'] = ['Official source update/index failed']
    return result


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
        r['cve_matches'] = []
        r['cve_query'] = {}
        r['cve_state'] = 'pending_review' if r.get('cves_in_mail') else 'unqueried'
        entry = snapshot.get('records', {}).get(r['id'])
        if r['id'] not in selected:
            continue
        if not entry or entry['commits'] != selected[r['id']]:
            r['osv_query'] = {}
            r['osv_candidates'] = []
            continue
        if snapshot.get('schema') == 1:
            r['osv_query'] = {k: v for k, v in entry.items() if k != 'candidates'}
            r['osv_candidates'] = entry['candidates']
            continue
        r['cve_query'] = {**entry, 'official_source': snapshot.get('official_source', {}),
                          'source_error': snapshot.get('source_error', '')}
        r['cve_matches'] = entry.get('matches', [])
        r['cve_state'] = entry['state']
        if entry['state'] == 'unmatched' and r.get('cves_in_mail'):
            r['cve_state'] = 'pending_review'
        r['osv_query'] = entry.get('osv_query', {})
        r['osv_candidates'] = entry.get('osv_affected', [])
    return records


def decorate(payload):
    payload = copy.deepcopy(payload)
    import resolve_maintainer_commits
    resolve_maintainer_commits.overlay(payload['records'])
    snapshot = read_store()
    overlay(payload['records'], snapshot)
    payload['cve_confirmed_records'] = sum(bool(r.get('cve_matches')) for r in payload['records'])
    payload['cve_candidate_records'] = sum(r.get('cve_state') == 'pending_review' for r in payload['records'])
    if set(targets(payload['records'])) & set(snapshot.get('records', {})):
        payload['revision'] = payload.get('revision', '').split(':cve:')[0] + ':cve:' + snapshot.get('attempted_at', '')
    return payload


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--if-due', action='store_true')
    args = parser.parse_args()
    old = read_store()
    if args.if_due and not due(old, datetime.now().astimezone()):
        print('not_due: weekly Applied-only Linux CNA check')
        return 0
    import mail_monitor
    import patch_status_dashboard as dashboard
    import resolve_maintainer_commits
    resolve_maintainer_commits.main()
    payload = dashboard.upgrade_payload(mail_monitor.load_seed())
    print('Updating official Linux CNA index and matching Applied repair commits...', flush=True)
    result = check(payload['records'])
    if not result['completed_at']:
        result['completed_at'] = old.get('completed_at')
    # Keep the prior successful evidence for failed requests, explicitly dated.
    for key, entry in result['records'].items():
        prior = old.get('records', {}).get(key)
        if prior and prior['commits'] == entry['commits']:
            entry['osv_query'] = prior.get('osv_query', {}) if old.get('schema') == 2 else {
                k: v for k, v in prior.items() if k not in {'candidates', 'previous_check'}}
            entry['osv_affected'] = prior.get('osv_affected', []) if old.get('schema') == 2 else prior.get('candidates', [])
            if entry['state'] != 'error' and old.get('schema') == 2:
                current = {(m['id'], m['commit']) for m in entry.get('matches', [])}
                entry['removed_matches'] = [m for m in prior.get('matches', [])
                                            if (m['id'], m['commit']) not in current]
        if entry['state'] == 'error' and prior and prior['commits'] == entry['commits']:
            if old.get('schema') == 2:
                entry['previous_check'] = prior.get('previous_check', prior) if prior['state'] == 'error' else prior
    save(result)
    from collections import Counter
    print(json.dumps({'applied_patches': len(result['records']),
                      'unique_commits': result['unique_commits'],
                      'states': dict(Counter(e['state'] for e in result['records'].values())),
                      'confirmed_patches': sum(bool(e.get('matches')) for e in result['records'].values()),
                      'official_source': result.get('official_source'),
                      'source_error': result.get('source_error')}, ensure_ascii=False))
    return int(bool(result.get('source_error')) or any(e['state'] == 'error' for e in result['records'].values()))


if __name__ == '__main__':
    raise SystemExit(main())
