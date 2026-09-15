"""Resolve four mail-accepted patches against public maintainer branch history."""
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from api_transport import windows_json, NativeHTTPError
from linux_cna import atomic_json

ROOT = Path(__file__).resolve().parent
if ROOT.name == 'outputs':
    ROOT = ROOT.parent / 'github-pages'
STORE = ROOT / 'maintainer_commits.json'
SCOPE = [
    ('Input: hp_sdc: shut down kicker timer on module exit', 'dtor/input', 'for-linus', 'drivers/input/serio/hp_sdc.c'),
    ('mmc: sh_mmcif: initialize IRQ-thread mutex before requesting interrupt', 'ulfh/mmc', 'fixes', 'drivers/mmc/host/sh_mmcif.c'),
    ('crypto: ccp: Initialize DBC ioctl mutex before registering device', 'herbert/crypto-2.6', 'master', 'drivers/crypto/ccp/dbc.c'),
    ('iio: admv1013: initialize callback mutex before registering notifier', 'jic23/iio', 'fixes-togreg', 'drivers/iio/frequency/admv1013.c'),
]


def resolve(spec, author):
    title, repo, branch, filename = spec
    base = 'https://kernel.googlesource.com/pub/scm/linux/kernel/git/' + repo
    current = 'refs/heads/' + branch
    chain = []
    for _ in range(60):
        cache = ROOT / 'local' / 'maintainer-history' / repo.replace('/', '-') / (current + '.json')
        if not current.startswith('refs/') and cache.exists():
            commit = json.loads(cache.read_text(encoding='utf-8'))
        else:
            for attempt in range(3):
                try:
                    commit = windows_json(base + '/+/' + current + '?format=JSON')
                    break
                except NativeHTTPError as exc:
                    if exc.status in {401, 403, 429} or attempt == 2:
                        raise RuntimeError('HTTP ' + str(exc.status) + ' reading ' + current) from None
            immutable = ROOT / 'local' / 'maintainer-history' / repo.replace('/', '-') / (commit['commit'] + '.json')
            atomic_json(immutable, commit)
        sha = commit['commit']
        if len(chain) and sha != current:
            raise ValueError('Unexpected parent commit')
        chain.append(sha)
        message = commit['message']
        if message.splitlines()[0].casefold() == title.casefold():
            if commit['author']['email'].casefold() != author.casefold():
                raise ValueError('Author mismatch')
            if filename not in {d.get('new_path') for d in commit.get('tree_diff', [])}:
                raise ValueError('Changed file mismatch')
            return {'title': title, 'sha': sha, 'author_email': author, 'verified': True,
                    'repository': base, 'branch': branch, 'head_sha': chain[0], 'ancestor_chain': chain,
                    'file': filename, 'message': message, 'source': base + '/+/' + sha,
                    'checked_at': datetime.now(timezone.utc).isoformat(timespec='seconds')}
        if not commit['parents']:
            break
        current = commit['parents'][0]
    return {'title': title, 'verified': False, 'reason': 'Not found within 60 first-parent commits'}


def overlay(records):
    saved = json.loads(STORE.read_text(encoding='utf-8')) if STORE.exists() else {}
    by_title = {p['title'].casefold(): p for p in saved.get('commits', [])}
    for r in records:
        proof = by_title.get(r['title'].casefold())
        r['maintainer_commits'] = [proof] if proof and proof.get('verified') and r.get('kind') == 'patch' and r.get('signals', {}).get('applied') else []
        r['maintainer_lookup'] = proof if proof and not proof.get('verified') else {}


def main():
    import mail_monitor
    import patch_status_dashboard as dashboard
    payload = dashboard.upgrade_payload(mail_monitor.load_seed())
    applied = {r['title'].casefold() for r in payload['records'] if r.get('signals', {}).get('applied')}
    old = json.loads(STORE.read_text(encoding='utf-8')) if STORE.exists() else {'commits': []}
    known = {p['title']: p for p in old['commits']}
    def ready(title):
        previous = known.get(title, {})
        if previous.get('verified'):
            return False
        # Respect service throttling: retry on a later run, not immediately.
        recent = STORE.exists() and datetime.now().timestamp() - STORE.stat().st_mtime < 86400
        return not (recent and 'HTTP 429' in previous.get('reason', ''))
    todo = [s for s in SCOPE if ready(s[0]) and s[0].casefold() in applied]
    if not todo:
        return
    def work(spec):
        try:
            return resolve(spec, payload['author_email'])
        except Exception as exc:
            return {'title': spec[0], 'verified': False, 'reason': type(exc).__name__ + ': ' + str(exc)[:150]}
    with ThreadPoolExecutor(max_workers=4) as pool:
        for result in pool.map(work, todo):
            known[result['title']] = result
            print(json.dumps({k: v for k, v in result.items() if k in {'title','sha','verified','reason'}}, ensure_ascii=False), flush=True)
    atomic_json(STORE, {'schema': 1, 'commits': list(known.values())})


if __name__ == '__main__':
    main()
