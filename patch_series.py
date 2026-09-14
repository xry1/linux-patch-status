"""Group numbered submissions using mail ancestry, without merging patch records."""
import hashlib
import re
from collections import defaultdict
from email.utils import parseaddr

from patch_analysis import plain, scope_of, submission, version


def numbered(message, author_email):
    if not submission(message) or parseaddr(message.get('sender', ''))[1].casefold() != author_email.casefold():
        return None
    if scope_of(message, plain(message.get('body', ''))).startswith('stable:'):
        return None
    match = re.search(r'\[[^\]]*\b(\d+)/(\d+)\b', message.get('subject', ''))
    if not match:
        return None
    index, total = map(int, match.groups())
    # Bound untrusted header counts before constructing missing-number lists.
    return (index, total, version(message) or 1) if 1 < total <= 1000 and 0 <= index <= total else None


def build_series(records, author_email):
    by_id = {r['id']: r for r in records}
    mail = {m['message_id']: m for r in records for m in r['messages']}
    owner = {m['message_id']: r['id'] for r in records for m in r['messages']}
    numbered_mail = {mid: info for mid, m in mail.items() if (info := numbered(m, author_email))}

    def ancestors(mid):
        seen = {mid}
        pending = list(mail[mid].get('in_reply_to', [])) + list(reversed(mail[mid].get('references', [])))
        while pending:
            parent = pending.pop(0)
            if parent in seen:
                continue
            seen.add(parent)
            yield parent
            if parent in mail:
                pending.extend(mail[parent].get('in_reply_to', []))
                pending.extend(reversed(mail[parent].get('references', [])))

    revisions = {}
    for mid, (index, total, ver) in numbered_mail.items():
        parents = list(ancestors(mid))
        covers = [p for p in parents if numbered_mail.get(p) == (0, total, ver)]
        first = [p for p in parents if numbered_mail.get(p) == (1, total, ver)]
        if index == 0:
            root = mid
        elif covers:
            root = covers[0]
        elif first:
            root = first[0]
        elif mail[mid].get('in_reply_to') and mail[mid]['in_reply_to'][0] not in mail:
            # A shared, missing cover can still link siblings; expose the missing cover.
            root = mail[mid]['in_reply_to'][0]
        else:
            root = mid
        key = (root, ver, total)
        rev = revisions.setdefault(key, {'root_message_id': root, 'version': ver, 'total': total,
            'cover_record_id': owner.get(root) if numbered_mail.get(root, (-1,))[0] == 0 else None,
            'submitted_at': mail.get(root, mail[mid]).get('date_iso', ''), 'members': []})
        if index:
            rev['members'].append({'record_id': owner[mid], 'number': index, 'message_id': mid})

    revisions = list(revisions.values())
    for rev in revisions:
        rev['members'].sort(key=lambda m: (m['number'], m['message_id']))
    parents = list(range(len(revisions)))

    def find(i):
        while parents[i] != i:
            parents[i] = parents[parents[i]]
            i = parents[i]
        return i

    for i, newer in enumerate(revisions):
        links = set(ancestors(newer['root_message_id'])) if newer['root_message_id'] in mail else set()
        members = {m['record_id'] for m in newer['members']}
        for j, older in enumerate(revisions[:i]):
            same_cover = newer['cover_record_id'] and newer['cover_record_id'] == older['cover_record_id']
            older_links = set(ancestors(older['root_message_id'])) if older['root_message_id'] in mail else set()
            linked_versions = newer['version'] != older['version'] and (
                older['root_message_id'] in links or newer['root_message_id'] in older_links)
            overlap = members & {m['record_id'] for m in older['members']}
            if same_cover or (linked_versions and overlap):
                parents[find(i)] = find(j)

    families = defaultdict(list)
    for i, rev in enumerate(revisions):
        families[find(i)].append(rev)
    result = []
    for r in records:
        r['series_ids'] = []
    for family in families.values():
        family.sort(key=lambda rev: (rev['version'], rev['submitted_at'], rev['root_message_id']))
        latest = family[-1]
        cover_ids = list(dict.fromkeys(rev['cover_record_id'] for rev in reversed(family) if rev['cover_record_id']))
        member_ids = list(dict.fromkeys(m['record_id'] for rev in reversed(family) for m in rev['members']))
        record_ids = list(dict.fromkeys(cover_ids + member_ids))
        # An isolated numbered patch with no cover or siblings is not a verified group.
        if not cover_ids and len(member_ids) < 2:
            continue
        for rev in family:
            numbers = [m['number'] for m in rev['members']]
            rev['missing_numbers'] = sorted(set(range(1, rev['total'] + 1)) - set(numbers))
            rev['ambiguous_numbers'] = sorted(n for n in set(numbers) if numbers.count(n) > 1)
        current_ids = list(dict.fromkeys(m['record_id'] for m in latest['members']))
        current = [by_id[mid] for mid in current_ids]
        complete = not latest['missing_numbers'] and not latest['ambiguous_numbers'] and len(current) == latest['total']
        counts = {'total': latest['total'], 'present': len(current),
                  'applied': sum(r['signals']['applied'] for r in current),
                  'mainline': sum(r.get('acceptance_basis') == 'mainline_verified' for r in current),
                  'reverted': sum(r['signals'].get('reverted', False) for r in current),
                  'attention': sum(r['has_attention'] for r in current)}
        state = ('mainline' if complete and counts['mainline'] == counts['total'] else
                 'applied' if complete and counts['applied'] == counts['total'] else
                 'reverted' if complete and counts['reverted'] == counts['total'] else
                 'partial' if counts['applied'] or counts['reverted'] else
                 'attention' if counts['attention'] else 'pending')
        series_id = 'series-' + hashlib.sha256(family[0]['root_message_id'].encode()).hexdigest()[:16]
        title_record = by_id.get(latest['cover_record_id']) or by_id[member_ids[0]]
        item = {'id': series_id, 'title': title_record['title'], 'latest_version': latest['version'],
                'revisions': family, 'cover_record_ids': cover_ids, 'member_record_ids': member_ids,
                'record_ids': record_ids, 'current_member_ids': current_ids,
                'counts': counts, 'complete': complete, 'state': state,
                'first_date': min(by_id[mid]['first_date'] for mid in record_ids),
                'last_date': max(by_id[mid]['last_date'] for mid in record_ids)}
        result.append(item)
        for mid in record_ids:
            by_id[mid]['series_ids'].append(series_id)
    return sorted(result, key=lambda s: (s['last_date'], s['title']), reverse=True)


def applied_submissions(records, series):
    """Count standalone patches and series once, retaining child-level evidence."""
    by_id = {r['id']: r for r in records}
    grouped = {rid for s in series for rid in s['record_ids']}
    units = []

    def accepted(r):
        return r['signals']['applied'] and not r['signals'].get('reverted', False)

    for s in series:
        evidence = [rid for rid in s['current_member_ids'] if accepted(by_id[rid])]
        latest = s['revisions'][-1]
        cover = by_id.get(latest['cover_record_id'])
        # A current cover confirmation can count the submission, but cannot
        # establish each child's status or override known child reverts.
        if (cover and accepted(cover) and not s['counts']['reverted']
                and cover['latest_version'] == latest['version']):
            evidence.append(cover['id'])
        if evidence:
            units.append({'id': s['id'], 'kind': 'series', 'record_ids': s['record_ids'],
                          'evidence_record_ids': evidence,
                          'partial': s['state'] not in ('mainline', 'applied')})
    for r in records:
        if r['id'] not in grouped and r['kind'] == 'patch' and accepted(r):
            units.append({'id': r['id'], 'kind': 'patch', 'record_ids': [r['id']],
                          'evidence_record_ids': [r['id']], 'partial': False})
    return {'total': len(units), 'standalone': sum(u['kind'] == 'patch' for u in units),
            'series': sum(u['kind'] == 'series' for u in units),
            'partial_series': sum(u['partial'] for u in units), 'units': units}
