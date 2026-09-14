"""Conservative, evidence-linked mail signals; no claim of repository verification."""
import hashlib
import re
from collections import defaultdict


def plain(body):
    lines = []
    for line in body.splitlines():
        if line.startswith('diff --git ') or line.strip() == '---':
            break
        if not line.lstrip().startswith('>'):
            lines.append(line)
    return '\n'.join(lines)


def clean_title(subject):
    text = re.sub(r'\s+', ' ', subject).strip()
    for _ in range(8):
        text = re.sub(r'^\s*(?:re|fwd?)\s*:\s*', '', text, flags=re.I)
        text = re.sub(r'^\s*failed:\s*patch\s*[\"\']?', '', text, flags=re.I)
        text = re.sub(r'^\s*\[(?:[^\]]*patch[^\]]*|\d+/\d+|net(?:-next)?(?:,[^\]]*)?)\]\s*', '', text, flags=re.I)
    text = re.sub(r'[\"\']?\s+failed to apply to .+? tree\s*$', '', text, flags=re.I)
    return text.strip(' \"\'') or '(no subject)'


def submission(message):
    subject = message.get('subject', '')
    return not re.match(r'^\s*(?:re|fwd?|failed)\s*:', subject, re.I) and bool(
        re.match(r'^\s*\[(?:[^\]]*patch[^\]]*|\d+/\d+|net(?:-next)?(?:,[^\]]*)?)\]', subject, re.I))


def version(message):
    header = ''.join(re.findall(r'\[([^\]]+)\]', message.get('subject', '')))
    found = re.search(r'\bv(\d+)\b', header, re.I)
    return int(found[1]) if found else None


def acceptance_line(line):
    return bool(re.search(r'^(?:(?:thanks|thank you)[,! .]*)?(?:(?:I(?: have|[’\']ve)?|both|patch|\d+ patch\(es\))\s+)?(?:applied|merged|queued|picked up|cherry[- ]picked)\b|\b(?:this (?:patch|series) (?:was|is)|(?:has|have) been) (?:applied|merged|queued)\b|\bwhat I applied to\b', line.strip(), re.I)) and not re.search(r'\b(?:if|will|would|should|could|not|once)\b|\?', line, re.I)


def scope_of(message, text):
    subject = message.get('subject', '')
    context = subject + ('\n' + text[:900] if not submission(message) else '')
    stable = re.search(r'\b(\d+\.\d+)(?:\.\d+)?(?:-stable|\.y)\b', context, re.I)
    if not stable:
        stable = re.search(r'\[PATCH\s+(?:AUTOSEL\s+)?(\d+\.\d+)\b', subject, re.I)
    if stable:
        return 'stable:' + stable[1]
    if re.search(r'\bstable(?: queue| tree| branch)\b|stable-queue', context, re.I):
        return 'stable:unspecified'
    # A future promise to send to Linus is not evidence of mainline acceptance.
    if re.search(r'(?:applied|merged)\s+(?:to|into)\s+(?:the\s+)?(?:mainline|Linus)', text, re.I):
        return 'mainline'
    branch = re.search(r'(?:applied|queued|merged|picked up)\s+(?:to|into|for)\s+(?:the\s+)?([\w./-]+)', text, re.I)
    if branch and branch[1].lower() not in {'my', 'our', 'this', 'https', 'a'}:
        return 'maintainer:' + branch[1].rstrip('.')
    if re.search(r'\b(?:applied|queued|merged|picked up)\b', text, re.I):
        return 'maintainer:unspecified'
    return 'general'


def commit_references(messages):
    refs = []
    for message in messages:
        # Keep references in patch metadata, but ignore quoted copies and diff lines.
        text = plain(message.get('body', ''))
        for line in text.splitlines():
            hashes = re.findall(r'(?<![\da-f])([\da-f]{12,40})(?![\da-f])', line, re.I)
            if not hashes:
                continue
            if re.match(r'\s*Fixes:', line, re.I):
                role = 'introduced_by'
            elif re.search(r'upstream commit|commit .* upstream', line, re.I):
                role = 'upstream_fix'
            elif re.search(r'cherry[- ]pick|backport', line, re.I):
                role = 'backport_source'
            elif re.search(r'git\.kernel\.org/.*/(?:c|commit)/|github\.com/.*/commit/|\bcommit\s*:?', line, re.I):
                if scope_of(message, text).startswith('stable:'):
                    role = 'backport_source' if 'failed' in message.get('subject', '').lower() else 'backport_commit'
                else:
                    role = 'patch_commit' if any(acceptance_line(s) for s in text.splitlines()) else 'referenced_commit'
            else:
                continue
            url = re.search(r'https://[^\s<>]+', line)
            for sha in hashes:
                refs.append({'sha': sha.lower(), 'role': role, 'message_id': message['message_id'],
                             'source': url[0].rstrip(').,') if url else message.get('link', ''),
                             'evidence': line.strip()[:400]})
    return list({(r['sha'], r['role'], r['message_id']): r for r in refs}.values())


def analyze(messages, author_email):
    ordered = sorted(messages, key=lambda m: (m.get('date_iso') or m.get('date', ''), m['message_id']))
    originals = [m for m in ordered if submission(m) and author_email.lower() in m.get('sender', '').lower()
                 and not scope_of(m, plain(m.get('body', ''))).startswith('stable:')]
    kind = 'patch'
    if originals and all(re.search(r'\[[^\]]*\b0/\d+\b', m['subject']) for m in originals):
        kind = 'cover'
    elif not originals:
        kind = 'backport' if any(re.search(r'failed to apply|stable', m['subject'], re.I) for m in ordered) else 'discussion'
        if any(submission(m) and 'diff --git ' in m.get('body', '') for m in ordered):
            kind = 'patch'
    assigned = {m['message_id']: (version(m) or 1) for m in ordered if submission(m)}
    for _ in range(len(ordered) + 1):
        changed = False
        for m in ordered:
            if m['message_id'] in assigned:
                continue
            parents = m.get('in_reply_to', []) + list(reversed(m.get('references', [])))
            found = next((assigned[p] for p in parents if p in assigned), None)
            if found is not None or version(m):
                assigned[m['message_id']] = version(m) or found
                changed = True
        if not changed:
            break
    latest = max((version(m) or 1 for m in originals), default=max(assigned.values(), default=1))
    events = []
    for m in ordered:
        text = plain(m.get('body', ''))
        scope = scope_of(m, text)
        signal_text = re.split(r'^-{5,}.*original commit', text, maxsplit=1, flags=re.I | re.M)[0]
        # Stable notifications usually omit the original submission version.
        v = assigned.get(m['message_id'], latest if scope.startswith('stable:') else 1)
        m['patch_version'] = v
        def add(signal, line, reason='', action=''):
            events.append({'kind': signal, 'message_id': m['message_id'], 'date': m.get('date_iso') or m.get('date', ''),
                           'sender': m.get('sender', ''), 'version': v, 'scope': scope,
                           'evidence': line.strip()[:600], 'reason': reason, 'action': action})
        if m in originals:
            add('submitted', m['subject'])
        elif submission(m) and scope.startswith('stable:'):
            add('backport_submitted', m['subject'])
        # Review tags are explicit attribution, including tags carried in a new revision.
        for line in signal_text.splitlines():
            tag = re.match(r'^\s*(Reviewed|Acked|Tested)-by:\s*\S', line, re.I)
            if tag:
                add({'reviewed': 'reviewed', 'acked': 'acked', 'tested': 'tested'}[tag[1].lower()], line)
        # Do not infer acceptance from the author's patch rationale or a forwarded patch.
        if submission(m) or author_email.lower() in m.get('sender', '').lower():
            continue
        for line in [m['subject'], *signal_text.splitlines()]:
            failure = re.search(r'failed to apply|does not apply|not applied|\brejected\b|\bnack\b|won[’\']t apply', line, re.I)
            revision = re.search(r'\bplease\s+(?:fix|change|rework|revise|resend|rebase|send\s+(?:a\s+)?v\d)|\bneeds?\s+(?:to be\s+)?(?:reworked|rebased)', line, re.I)
            if failure or revision:
                backport = scope.startswith('stable:') and bool(failure)
                add('attention', line, 'backport_failure' if backport else 'revision_requested' if revision else 'rejected',
                    '检查该 stable 分支是否需要单独回移；不影响维护者树收录。' if backport else '阅读反馈，确认是否需要修改或回复。')
                continue
            if acceptance_line(line):
                add('applied', line)
    # Duplicate subject/body signals do not create duplicate timeline entries.
    events = list({(e['message_id'], e['kind'], e['scope'], e['evidence']): e for e in events}.values())
    current = [e for e in events if e['version'] == latest or e['scope'].startswith('stable:')]
    states = {}
    pending = {}
    for event in current:
        scope = event['scope']
        if event['kind'] == 'attention':
            pending[scope] = event
            if scope != 'general':
                states[scope] = event
        elif event['kind'] == 'applied':
            states[scope] = event
            pending.pop(scope, None)
            if not scope.startswith('stable:'):
                pending.pop('general', None)
        elif event['kind'] == 'backport_submitted':
            if states.get(scope, {}).get('kind') != 'applied':
                states[scope] = event
            if scope in pending:
                pending[scope] = {**event, 'reason': 'backport_pending',
                                  'action': '已有后续回移投递，请确认该分支是否最终收录。'}
    accepted = any(e['kind'] == 'applied' and not scope.startswith('stable:') for scope, e in states.items())
    reviewed = any(e['kind'] == 'reviewed' and e['version'] == latest for e in events)
    acked = any(e['kind'] == 'acked' and e['version'] == latest for e in events)
    tested = any(e['kind'] == 'tested' and e['version'] == latest for e in events)
    status = 'Applied' if accepted else 'Needs attention' if any(not s.startswith('stable:') for s in pending) else 'Reviewed' if reviewed or acked or tested else 'Submitted'
    versions = []
    for v in sorted(set(assigned.values()) | {latest}, reverse=True):
        matching = [m for m in ordered if m.get('patch_version') == v]
        versions.append({'version': v, 'message_ids': [m['message_id'] for m in matching],
                         'submitted_at': next((m.get('date_iso', m.get('date', '')) for m in matching if m in originals), ''),
                         'latest': v == latest})
    return {'kind': kind, 'latest_version': latest, 'versions': versions, 'events': events,
            'signals': {'applied': accepted, 'reviewed': reviewed, 'acked': acked, 'tested': tested,
                        'mainline': states.get('mainline', {}).get('kind') == 'applied'},
            'branch_states': list(states.values()), 'attention_items': list(pending.values()),
            'has_attention': bool(pending), 'status': status,
            'subsystem': clean_title(ordered[0]['subject']).split(':', 1)[0].lower()[:45],
            'commit_refs': commit_references(ordered)}


def regroup(records):
    """Merge exact normalized subjects; match truncated stable notices only by fix hash too."""
    grouped = {}
    for r in records:
        key = clean_title(r['title']).casefold()
        if key not in grouped:
            grouped[key] = {**r, 'title': clean_title(r['title']), 'messages': [], 'osv_candidates': [], 'aliases': []}
        target = grouped[key]
        target['messages'].extend(r['messages'])
        target['osv_candidates'].extend(r.get('osv_candidates', []))
        target['aliases'].extend([r.get('id', ''), *r.get('aliases', [])])
    candidates = list(grouped.items())
    refs = {key: {x['sha'] for x in commit_references(r['messages']) if x['role'] not in {'introduced_by', 'referenced_commit'}} for key, r in candidates}
    for key, r in candidates:
        if not all(re.search(r'failed to apply', m['subject'], re.I) for m in r['messages']):
            continue
        targets = [k for k, other in candidates if k != key and k.startswith(key) and refs[key] & refs[k]
                   and any(submission(m) for m in other['messages'])]
        if len(targets) == 1:
            target = grouped[targets[0]]
            target['messages'].extend(r['messages'])
            target['aliases'].extend(r['aliases'])
            target['osv_candidates'].extend(r['osv_candidates'])
            grouped.pop(key, None)
    for key, r in grouped.items():
        unique = {}
        for m in r['messages']:
            mid = m['message_id']
            if mid not in unique or len(m.get('body', '')) > len(unique[mid].get('body', '')):
                unique[mid] = m
        r['messages'] = sorted(unique.values(), key=lambda m: (m.get('date_iso') or m.get('date', ''), m['message_id']))
        r['id'] = hashlib.sha256(key.encode()).hexdigest()[:16]
        r['aliases'] = sorted(set(filter(None, r['aliases'])) - {r['id']})
        r['osv_candidates'] = list({x['id']: x for x in r['osv_candidates']}.values())
    return list(grouped.values())
