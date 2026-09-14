"""Apply saved, independently verified mainline facts without making network calls."""
import json
import re
from pathlib import Path

from patch_analysis import clean_title

EVIDENCE_FILE = Path(__file__).with_name('applied_verifications.json')


def load_verifications():
    if not EVIDENCE_FILE.exists():
        return {}
    evidence = json.loads(EVIDENCE_FILE.read_text(encoding='utf-8-sig'))
    if evidence.get('schema_version') != 1 or evidence.get('repository') != 'torvalds/linux':
        raise ValueError('Unsupported mainline verification snapshot')
    return evidence


def apply_verifications(records, snapshot, author_email):
    if snapshot.get('author_email', '').casefold() != author_email.casefold():
        snapshot = {}
    by_title = {}
    for proof in snapshot.get('commits', []):
        sha = proof.get('sha', '')
        if not (re.fullmatch('[0-9a-f]{40}', sha) and proof.get('mainline_ancestor') is True
                and proof.get('merge_base') == sha and proof.get('behind_by') == 0
                and proof.get('head_sha') == snapshot.get('head_sha')):
            continue
        by_title.setdefault(clean_title(proof['title']).casefold(), []).append(proof)
    for record in records:
        candidates = by_title.get(clean_title(record['title']).casefold(), []) if record['kind'] == 'patch' else []
        own, other = [], []
        for proof in candidates:
            item = {key: proof.get(key) for key in ('sha', 'title', 'author', 'author_email', 'committed_at',
                    'head_sha', 'source', 'comparison_url', 'checked_at')}
            authored = proof.get('authored_by_user') is True and proof.get('author_email', '').casefold() == author_email.casefold()
            signed = proof.get('signed_off_by_user') is True
            item['attribution'] = 'author' if authored else 'signed_off' if signed else 'other_author'
            (own if authored or signed else other).append(item)
        record['mainline_commits'] = own
        record['related_mainline_commits'] = other
        record['acceptance_basis'] = 'mainline_verified' if own else 'mail_confirmation' if record['signals']['applied'] else 'unconfirmed'
        if not own:
            continue
        record['signals']['mainline'] = True
        record['signals']['applied'] = True
        record['status'] = 'Applied'
        committed_at = max(x['committed_at'] for x in own)
        # Mainline adoption resolves older general review requests, not stable failures.
        record['attention_items'] = [e for e in record['attention_items']
                                      if e['scope'].startswith('stable:') or e['date'] > committed_at]
        record['has_attention'] = bool(record['attention_items'])
        for proof in own:
            event = {'kind': 'mainline_verified', 'message_id': None, 'version': None,
                     'date': proof['committed_at'], 'scope': 'mainline', 'sender': proof['author'],
                     'evidence': '主线历史已核实：' + proof['sha'], 'source': proof['source'],
                     'comparison_url': proof['comparison_url'], 'checked_at': proof['checked_at'],
                     'reason': '', 'action': ''}
            record['events'].append(event)
            record['branch_states'].append(event)
        record['events'].sort(key=lambda e: (e['date'], e.get('message_id') or ''))
    return records
