"""Local, opt-in reminders anchored to an author's actual patch submission."""
import copy
import hashlib
import json
import os
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr

from patch_analysis import submission, version

STORE = 'revision-reminders.json'
_lock = threading.Lock()


def clock():
    return datetime.now(timezone.utc)


def timestamp(value):
    try:
        date = datetime.fromisoformat(value)
        return date if date.tzinfo else None
    except (TypeError, ValueError):
        return None


def read(directory):
    path = directory / STORE
    if not path.exists():
        return {'schema_version': 1, 'items': {}}
    data = json.loads(path.read_text(encoding='utf-8'))
    if data.get('schema_version') != 1 or not isinstance(data.get('items'), dict):
        raise ValueError('投递提醒记录格式无效，原文件已保留。')
    return data


def save(directory, data):
    fd, name = tempfile.mkstemp(prefix='.reminders-', suffix='.tmp', dir=directory)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
        os.replace(name, directory / STORE)
    finally:
        if os.path.exists(name):
            os.unlink(name)


@contextmanager
def transaction(directory):
    directory.mkdir(parents=True, exist_ok=True)
    with _lock, (directory / 'revision-reminders.lock').open('a+b') as stream:
        if stream.tell() == 0:
            stream.write(b'0')
            stream.flush()
        stream.seek(0)
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            data = read(directory)
            yield data
            save(directory, data)
        finally:
            stream.seek(0)
            if os.name == 'nt':
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


def targets(payload):
    records = {r['id']: r for r in payload.get('records', [])}
    groups = [{'id': s['id'], 'title': s['title'], 'record_ids': s['record_ids'], 'kind': 'series'}
              for s in payload.get('series', [])]
    grouped = {rid for group in groups for rid in group['record_ids']}
    groups += [{'id': rid, 'title': r['title'], 'record_ids': [rid], 'kind': 'patch'}
               for rid, r in records.items() if rid not in grouped and r.get('kind') == 'patch']
    author = payload.get('author_email', '').casefold()
    result = {}
    for group in groups:
        originals = {m['message_id']: m for rid in group['record_ids'] for m in records[rid]['messages']
                     if submission(m) and parseaddr(m.get('sender', ''))[1].casefold() == author}
        if not originals:
            continue
        latest = max(version(m) or 1 for m in originals.values())
        current = [m for m in originals.values() if (version(m) or 1) == latest]
        # Every member must have a precise date. For a series wait from its last
        # submitted member, so the entire previous version has aged by 24 hours.
        if any(timestamp(m.get('date_iso')) is None for m in current):
            continue
        base = max(current, key=lambda m: timestamp(m['date_iso']))
        sent = timestamp(base['date_iso']).astimezone(timezone.utc).isoformat(timespec='seconds')
        source = hashlib.sha256(json.dumps([group['id'], latest, sent, sorted(m['message_id'] for m in current)]).encode()).hexdigest()[:24]
        member_records = [records[rid] for rid in group['record_ids'] if records[rid].get('kind') == 'patch']
        accepted = bool(member_records) and all(r.get('signals', {}).get('applied') for r in member_records)
        result[group['id']] = {**group, 'version': latest, 'submitted_at': sent, 'source_id': source,
                               'message_id': base['message_id'], 'accepted': accepted}
    return result


def reconcile(items, available, at):
    for item in items.values():
        if item['status'] not in ('scheduled', 'failed', 'uncertain'):
            continue
        target = available.get(item['target_id'])
        if target is None:
            overlaps = [t for t in available.values() if set(t['record_ids']) & set(item['record_ids'])]
            target = overlaps[0] if len(overlaps) == 1 else None
        if target is None:
            item.update(status='needs_review', reason='无法核对原投递，请检查归档后重新设置。', updated_at=at)
        elif target['version'] > item['version'] or target['source_id'] != item['source_id']:
            item.update(status='superseded', reason=f'已检测到新的投递（v{target["version"]}），原提醒已结束。', updated_at=at)
        elif target['accepted']:
            item.update(status='done', reason='已检测到接收依据，提醒已结束。', updated_at=at)


def view(payload, directory):
    available = targets(payload)
    data = read(directory)
    reconcile(data['items'], available, clock().isoformat(timespec='seconds'))
    return {'targets': sorted(available.values(), key=lambda t: t['submitted_at'], reverse=True),
            'items': sorted(data['items'].values(), key=lambda i: i['due_at']), 'default_hours': 24}


def decorate(payload, directory):
    result = dict(payload)
    result['revision_reminders'] = view(payload, directory)
    return result


def action(payload, directory, request):
    if not isinstance(request, dict):
        raise ValueError('提醒请求格式无效。')
    operation = request.get('action')
    if any(key in request and not isinstance(request[key], str) for key in ('action', 'target_id', 'source_id', 'id')):
        raise ValueError('提醒标识格式无效。')
    available = targets(payload)
    at = clock().isoformat(timespec='seconds')
    with transaction(directory) as data:
        reconcile(data['items'], available, at)
        if operation == 'schedule':
            target = available.get(request.get('target_id'))
            if not target or target['source_id'] != request.get('source_id'):
                raise ValueError('投递版本已变化或缺少准确时间，请刷新页面再设置。')
            if target['accepted']:
                raise ValueError('该投递已有接收依据，请先核对是否仍需发送新版本。')
            hours = request.get('hours', 24)
            if type(hours) is not int or not 1 <= hours <= 168:
                raise ValueError('间隔需为 1 至 168 的整数小时。')
            key = target['source_id']
            prior = data['items'].get(key)
            if prior and prior['status'] in ('sent', 'sending', 'uncertain'):
                raise ValueError('这版提醒已发送或结果未确认，请先核对推送记录。')
            due = timestamp(target['submitted_at']) + timedelta(hours=hours)
            data['items'][key] = {**copy.deepcopy(target), 'id': key, 'target_id': target['id'],
                                  'hours': hours, 'due_at': due.isoformat(timespec='seconds'),
                                  'status': 'scheduled', 'attempts': 0, 'reason': '',
                                  'created_at': prior.get('created_at', at) if prior else at, 'updated_at': at}
        elif operation in ('cancel', 'done', 'retry'):
            item = data['items'].get(request.get('id'))
            if not item:
                raise ValueError('未找到该提醒。')
            if item['status'] == 'sending':
                raise ValueError('提醒正在发送，请稍后查看结果。')
            if operation == 'retry':
                if item['status'] not in ('uncertain', 'failed'):
                    raise ValueError('仅失败或结果未确认的提醒可以重试。')
                item.update(status='scheduled', attempts=0, reason='', updated_at=at)
            else:
                item.update(status='cancelled' if operation == 'cancel' else 'done',
                            reason='已手动取消。' if operation == 'cancel' else '已手动标记完成。', updated_at=at)
        else:
            raise ValueError('不支持的提醒操作。')
    return view(payload, directory)


def process_due(payload, directory, send, limit=10):
    """Claim before sending; leave ambiguous delivery for explicit manual retry."""
    if not (directory / STORE).is_file():
        return
    at = clock()
    with transaction(directory) as data:
        for item in data['items'].values():
            if item['status'] == 'sending':
                item.update(status='uncertain', reason='上次发送中断，结果未确认；请检查飞书后再决定是否重试。')
        reconcile(data['items'], targets(payload), at.isoformat(timespec='seconds'))
        due = [i['id'] for i in sorted(data['items'].values(), key=lambda i: i['due_at'])
               if i['status'] == 'scheduled' and timestamp(i['due_at']) <= at][:limit]
    for key in due:
        with transaction(directory) as data:
            item = data['items'][key]
            # A web request may cancel a reminder between selection and claim.
            if item['status'] != 'scheduled' or timestamp(item['due_at']) > clock():
                continue
            item.update(status='sending', attempts=item['attempts'] + 1, updated_at=clock().isoformat(timespec='seconds'))
            claimed = copy.deepcopy(item)
        # No store lock is held during the network request; the website stays usable.
        try:
            send(claimed)
            result = {'status': 'sent', 'reason': '', 'delivered_at': clock().isoformat(timespec='seconds')}
        except Exception as exc:
            uncertain = getattr(exc, 'uncertain', True)
            result = {'status': 'uncertain' if uncertain else 'failed' if claimed['attempts'] >= 3 else 'scheduled',
                      'reason': '发送结果未确认，请核对飞书后再决定是否重试。' if uncertain else '飞书明确拒绝；最多尝试三次。'}
        with transaction(directory) as data:
            data['items'][key].update(result, updated_at=clock().isoformat(timespec='seconds'))
