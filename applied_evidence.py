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


def valid_proof(proof, snapshot):
    sha = proof.get('sha', '')
    return bool(re.fullmatch('[0-9a-f]{40}', sha) and proof.get('mainline_ancestor') is True
                and proof.get('merge_base') == sha and proof.get('behind_by') == 0
                and proof.get('head_sha') == snapshot.get('head_sha'))


def reversal_history(originals, snapshot):
    """Follow explicit, verified SHA relationships, including a revert of a revert."""
    children = {}
    for proof in snapshot.get('reverts', []):
        target = proof.get('reverts_commit', '')
        match = re.search(r'^This reverts commit\s+([0-9a-f]{40})\.', proof.get('message', ''), re.I | re.M)
        if valid_proof(proof, snapshot) and match and match[1].lower() == target:
            children.setdefault(target, {})[proof['sha']] = proof

    def effective(sha, trail=frozenset()):
        if sha in trail:
            raise ValueError('Cyclic revert evidence; previous report must be preserved')
        return not any(effective(child, trail | {sha}) for child in children.get(sha, {}))

    history = {}
    def visit(sha):
        for child, proof in children.get(sha, {}).items():
            if child not in history:
                history[child] = {**proof, 'effective': effective(child)}
                visit(child)
    for proof in originals:
        effective(proof['sha'])
        visit(proof['sha'])
    return sorted(history.values(), key=lambda p: (p['committed_at'], p['sha']))


def apply_verifications(records, snapshot, author_email):
    if snapshot.get('author_email', '').casefold() != author_email.casefold():
        snapshot = {}
    by_title = {}
    for proof in snapshot.get('commits', []):
        if not valid_proof(proof, snapshot):
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
        record['revert_commits'] = []
        record['signals']['mainline_history'] = bool(own)
        record['signals']['reverted'] = False
        record['acceptance_basis'] = 'mainline_verified' if own else 'mail_confirmation' if record['signals']['applied'] else 'unconfirmed'
        if not own:
            continue
        reversals = reversal_history(own, snapshot)
        reversed_shas = {p['reverts_commit'] for p in reversals if p['effective']}
        for proof in own:
            proof['reverted'] = proof['sha'] in reversed_shas
        record['revert_commits'] = [{key: p.get(key) for key in ('sha', 'title', 'author', 'committed_at',
            'head_sha', 'source', 'comparison_url', 'checked_at', 'reverts_commit', 'effective')} for p in reversals]
        reverted = all(p['reverted'] for p in own)
        record['signals']['reverted'] = reverted
        record['signals']['mainline'] = not reverted
        record['signals']['applied'] = not reverted
        record['acceptance_basis'] = 'reverted_verified' if reverted else 'mainline_verified'
        record['status'] = 'Reverted' if reverted else 'Applied'
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
        for proof in record['revert_commits']:
            record['events'].append({'kind': 'mainline_revert', 'message_id': None, 'version': None,
                'date': proof['committed_at'], 'scope': 'mainline', 'sender': proof['author'],
                'evidence': '回退提交：' + proof['sha'] + ' → ' + proof['reverts_commit'],
                'source': proof['source'], 'comparison_url': proof['comparison_url'],
                'checked_at': proof['checked_at'], 'reason': '', 'action': ''})
        record['events'].sort(key=lambda e: (e['date'], e.get('message_id') or ''))
        # Branch badges describe the current state; the full sequence remains in events.
        record['branch_states'] = [e for e in record['branch_states'] if e['scope'] != 'mainline']
        current = next(e for e in reversed(record['events']) if e['kind'] in {'mainline_verified', 'mainline_revert'})
        record['branch_states'].append({**current, 'kind': 'reverted' if reverted else 'mainline_verified'})
    return records
