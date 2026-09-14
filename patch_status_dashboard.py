#!/usr/bin/env python3
"""Local Linux patch dashboard. Python 3.10+; no third-party dependencies."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import mailbox
import os
import re
import socket
import tempfile
import threading
import urllib.error
import urllib.request
import webbrowser
from collections import Counter, defaultdict
from datetime import datetime, timezone
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote, urlparse
from patch_analysis import analyze, clean_title, commit_references, regroup, submission
from applied_evidence import apply_verifications, load_verifications
from patch_series import applied_submissions, build_series

DEFAULT_EMAIL = "runyu.xiao@seu.edu.cn"
MAX_UPLOAD = 512 * 1024 * 1024
MAX_EXPANDED = 2 * 1024 * 1024 * 1024
TEMPLATE = Path(__file__).with_name("patch_dashboard_template.html")


def decode_header_value(value):
    try:
        return str(make_header(decode_header(value or "")))
    except (LookupError, TypeError, UnicodeError):
        return str(value or "")


def message_text(message):
    parts = []
    for part in message.walk():
        if part.is_multipart() or part.get_content_type() not in {
            "text/plain", "text/x-patch", "text/x-diff"
        }:
            continue
        payload = part.get_payload(decode=True)
        if isinstance(payload, bytes):
            try:
                text = payload.decode(part.get_content_charset() or "utf-8", "replace")
            except LookupError:
                text = payload.decode("utf-8", "replace")
        else:
            text = str(payload or "")
        if part.get_filename():
            text = "[附件: " + decode_header_value(part.get_filename()) + "]\n" + text
        parts.append(text)
    return "\n".join(parts)


def unquoted_text(text):
    return "\n".join(line for line in text.splitlines()
                     if not line.lstrip().startswith(">")
                     and line.strip().lower() not in {
                         "-----original message-----", "begin forwarded message:"
                     })


def parse_date(value):
    try:
        parsed = parsedate_to_datetime(value or "")
        if not parsed.tzinfo:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat(), parsed.strftime("%Y-%m-%d")
    except (TypeError, ValueError, OverflowError):
        return "", str(value or "")[:32]


def clean_subject(subject):
    return clean_title(subject)


def subject_key(subject):
    return clean_subject(subject).casefold()


def lore_link(message_id):
    return ("https://lore.kernel.org/all/" +
            quote(message_id.strip("<>"), safe="@._-+") + "/") if message_id else ""


def extract_commit_ids(text):
    context = re.compile(
        r"fixes:|commit\s+[0-9a-f]{12,40}|cherry[- ]picked|backported|"
        r"git\.kernel\.org/(?:stable/c|pub/scm/.*/commit)|github\.com/.*/commit/", re.I)
    return sorted({m.lower() for line in text.splitlines() if context.search(line)
                   for m in re.findall(r"(?<![0-9a-f])([0-9a-f]{12,40})(?![0-9a-f])", line, re.I)})


def extract_cves(text):
    return sorted({m.upper() for m in re.findall(r"CVE-\d{4}-\d{4,8}", text, re.I)})


def evidence_lines(text):
    pattern = re.compile(
        r"applied|queued|picked|cherry[- ]picked|merged|accepted|reviewed-by|acked-by|"
        r"tested-by|cve-|fixes:|failed to apply|not applied|rejected|nack|commit\s+[0-9a-f]{12,40}", re.I)
    return list(dict.fromkeys(re.sub(r"\s+", " ", line).strip()[:280]
                              for line in text.splitlines() if pattern.search(line)))[:20]


def infer_status(messages):
    result = analyze(messages, DEFAULT_EMAIL)
    return result['status'], [e['date'] + ': ' + e['evidence'] for e in result['events']]


def enrich_records(records, author_email=DEFAULT_EMAIL):
    records = regroup(records)
    for record in records:
        record.update(analyze(record['messages'], author_email))
    apply_verifications(records, load_verifications(), author_email)
    for record in records:
        items = record['messages']
        dates = [m.get('date_iso') or m.get('date', '') for m in items]
        dates = [d[:10] for d in dates if re.match(r'\d{4}-\d{2}-\d{2}', d)]
        record['first_date'] = min(dates, default='')
        record['last_date'] = max(dates, default='')
        record['message_count'] = len(items)
        record.setdefault('osv_query', {})
        # Re-importing truncated stable notices must not manufacture new alias metadata.
        record['aliases'] = sorted(set(record.get('aliases', [])) | {
            hashlib.sha256(subject_key(m['subject']).encode('utf-8')).hexdigest()[:16]
            for m in items if re.search(r'failed to apply', m['subject'], re.I)
            and subject_key(m['subject']) != subject_key(record['title'])})
        record['branches'] = sorted({e['scope'] for e in record['branch_states']})
        record['commit_ids_in_mail'] = sorted({x['sha'] for x in record['commit_refs']})
        record['evidence'] = [e['date'] + ': ' + e['evidence'] for e in record['events']]
        record['cve_mentions'] = [{'id': c, 'message_id': m['message_id'], 'source': m.get('link', '')}
                                  for m in items for c in extract_cves(unquoted_text(m.get('body', '')))]
        record['cves_in_mail'] = sorted({x['id'] for x in record['cve_mentions']})
        # A mail mention or OSV affected-commit response is never a verified fix mapping.
        record['cve_state'] = 'candidate' if record['cves_in_mail'] or record.get('osv_candidates') else 'unqueried'
        if record.get('osv_query'):
            record['cve_state'] = 'candidate' if record['cve_state'] == 'candidate' else record['osv_query']['state']
    return sorted(records, key=lambda r: (r['last_date'], r['title']), reverse=True)


def upgrade_payload(payload):
    """Re-analyze saved mail without pretending a network check happened."""
    payload = dict(payload)
    payload['records'] = enrich_records(payload.get('records', []), payload.get('author_email', DEFAULT_EMAIL))
    payload['series'] = build_series(payload['records'], payload.get('author_email', DEFAULT_EMAIL))
    payload['series_counts'] = {'total': len(payload['series']),
        'mainline': sum(s['state'] == 'mainline' for s in payload['series']),
        'grouped_topics': sum(bool(r['series_ids']) for r in payload['records'])}
    payload['schema_version'] = 3
    payload['status_counts'] = dict(Counter(r['status'] for r in payload['records']))
    payload['kind_counts'] = dict(Counter(r['kind'] for r in payload['records']))
    payload['topic_signal_counts'] = {k: sum(r['signals'][k] for r in payload['records']) for k in ('applied', 'reviewed', 'acked', 'tested', 'mainline', 'mainline_history', 'reverted')}
    payload['applied_submissions'] = applied_submissions(payload['records'], payload['series'])
    payload['signal_counts'] = {**payload['topic_signal_counts'], 'applied': payload['applied_submissions']['total']}
    payload['signal_count_units'] = {k: 'submission' if k == 'applied' else 'topic' for k in payload['signal_counts']}
    verification = load_verifications()
    payload['applied_audit'] = {
        'verified_at': verification.get('checked_at'),
        'mainline_topics': sum(r['acceptance_basis'] == 'mainline_verified' for r in payload['records']),
        'mainline_history_topics': sum(bool(r['mainline_commits']) for r in payload['records']),
        'reverted_topics': sum(r['signals']['reverted'] for r in payload['records']),
        'reverts_verified_at': verification.get('reverts_checked_at'),
        'mail_only_topics': sum(r['acceptance_basis'] == 'mail_confirmation' for r in payload['records']),
        'mail_only_patch_topics': sum(r['acceptance_basis'] == 'mail_confirmation' and r['kind'] == 'patch' for r in payload['records']),
        'applied_patch_topics': sum(r['signals']['applied'] and r['kind'] == 'patch' for r in payload['records']),
        'applied_cover_topics': sum(r['signals']['applied'] and r['kind'] == 'cover' for r in payload['records']),
        'head_sha': verification.get('head_sha'),
    }
    payload['attention_count'] = sum(r['has_attention'] for r in payload['records'])
    payload['stored_messages'] = len({m['message_id'] for r in payload['records'] for m in r['messages']})
    payload['months'] = dict(Counter(r['last_date'][:7] for r in payload['records'] if r['last_date']))
    payload.setdefault('last_checked_at', payload.get('last_import', {}).get('at', payload.get('generated_at', '')))
    last = payload.get('last_import', {})
    payload.setdefault('check_result', 'updated' if last.get('added_messages') or last.get('updated_messages') or last.get('changed_statuses') else 'unchanged')
    changed_imports = [x['at'] for x in payload.get('imports', []) if x.get('added_messages') or x.get('updated_messages')]
    payload.setdefault('content_updated_at', max(changed_imports, default=payload.get('generated_at', '')))
    return payload


def message_identity(item):
    mid = item.get("message_id", "").strip("<> \t\r\n")
    if not mid:
        # Old reports may have only a Lore URL; recover the original ID.
        link = item.get("link", "")
        if link.startswith("https://lore.kernel.org/all/"):
            mid = unquote(link[len("https://lore.kernel.org/all/"):].strip("/"))
    if not mid:
        raw = "\n".join(item.get(k, "") for k in ("sender", "date", "subject", "body"))
        mid = "missing-id-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return mid


def parse_archive(source, author_email, previous_records=None, progress=None):
    """Read gzip/plain mbox. Keep selected patches and their reference-linked replies."""
    notify = progress or (lambda _: None)
    previous_records = previous_records or []
    known_groups = {}
    id_group = {}
    messages = {}
    for record in previous_records:
        key = subject_key(record["title"])
        known_groups[key] = record["title"]
        for old in record["messages"]:
            item = dict(old)
            item["message_id"] = message_identity(item)
            messages[item["message_id"]] = item
            id_group[item["message_id"]] = key

    notify("正在解压归档…")
    with tempfile.TemporaryDirectory(prefix="patch-mbox-") as temp:
        extracted = Path(temp) / "archive.mbox"
        with source.open("rb") as check:
            compressed = check.read(2) == b"\x1f\x8b"
        opener = gzip.open if compressed else open
        with opener(source, "rb") as src, extracted.open("wb") as dst:
            size = 0
            while chunk := src.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_EXPANDED:
                    raise ValueError("解压后的归档超过 2 GB，请拆分归档。")
                dst.write(chunk)
        with extracted.open("rb") as stream:
            if not stream.readline().startswith(b"From "):
                raise ValueError("文件不是有效的 mbox 邮件归档。请选择 .mbox 或 .mbox.gz。")

        total = 0
        archive = mailbox.mbox(str(extracted), create=False)
        try:
            for message in archive:
                total += 1
                if total % 3000 == 0:
                    notify(f"正在解析邮件：{total:,} 封…")
                if not message.get("Subject") and not message.get("From"):
                    continue
                body = message_text(message)
                subject = decode_header_value(message.get("Subject"))
                sender = decode_header_value(message.get("From"))
                date_iso, date = parse_date(message.get("Date"))
                item = {
                    "message_id": decode_header_value(message.get("Message-ID")).strip("<> \t\r\n"),
                    "subject": subject, "sender": sender,
                    "to": decode_header_value(message.get("To")),
                    "cc": decode_header_value(message.get("Cc")),
                    "date": date, "date_iso": date_iso, "body": body,
                    "references": re.findall(r"<([^<>]+)>", str(message.get("References", ""))),
                    "in_reply_to": re.findall(r"<([^<>]+)>", str(message.get("In-Reply-To", ""))),
                }
                item["message_id"] = message_identity(item)
                item["link"] = lore_link(item["message_id"]) if not item["message_id"].startswith("missing-id-") else ""
                prior = messages.get(item["message_id"])
                # Some cross-list copies are truncated; retain the complete body.
                if prior is None or len(body) >= len(prior.get("body", "")):
                    messages[item["message_id"]] = item
                key = subject_key(subject)
                direct = re.search(rf"(?im)^Signed-off-by:.*<{re.escape(author_email)}>\s*$",
                                   unquoted_text(body))
                signal = author_email.casefold() in sender.casefold() or bool(direct)
                if signal and ("patch" in subject.casefold() or submission(item) or key in known_groups):
                    known_groups.setdefault(key, clean_subject(subject))
        finally:
            archive.close()

    if not total or not messages:
        raise ValueError("归档中没有可读取的邮件。")
    notify("正在关联回复、合并重复邮件…")
    for mid, item in messages.items():
        key = subject_key(item["subject"])
        if key in known_groups:
            id_group[mid] = key

    # A reply may change its subject to 'Applied'. Use the nearest known parent.
    # Do not pull sibling patches into a record through a shared series cover.
    unresolved = [mid for mid in messages if mid not in id_group]
    while unresolved:
        next_round = []
        changed = False
        for mid in unresolved:
            item = messages[mid]
            if re.match(r"^\s*\[.*patch", item["subject"], re.I):
                continue
            parents = item.get("in_reply_to", []) + list(reversed(item.get("references", [])))
            group = next((id_group[p] for p in parents if p in id_group), None)
            if group is not None:
                id_group[mid] = group
                changed = True
            else:
                next_round.append(mid)
        if not changed:
            break
        unresolved = next_round

    grouped = defaultdict(list)
    for mid, key in id_group.items():
        grouped[key].append(messages[mid])
    prior_by_key = {subject_key(r["title"]): r for r in previous_records}
    records = []
    for key, items in grouped.items():
        items.sort(key=lambda m: (m.get("date_iso") or m.get("date", ""), m["message_id"]))
        status, evidence = infer_status(items)
        dates = [m.get("date_iso") or m.get("date", "") for m in items]
        dates = [d[:10] for d in dates if re.match(r"\d{4}-\d{2}-\d{2}", d)]
        all_text = "\n".join(m["subject"] + "\n" + unquoted_text(m.get("body", "")) for m in items)
        commits = sorted({x['sha'] for x in commit_references(items)})
        prior = prior_by_key.get(key, {})
        same_commits = set(commits) == set(prior.get("commit_ids_in_mail", []))
        records.append({
            "id": hashlib.sha256(key.encode("utf-8")).hexdigest()[:16],
            "title": known_groups[key], "status": status, "message_count": len(items),
            "first_date": min(dates) if dates else "", "last_date": max(dates) if dates else "",
            "branches": sorted(set(re.findall(r"\b\d+\.\d+(?:\.\d+)?(?:-rc\d+)?\b",
                                              " ".join(m["subject"] for m in items)))),
            "cves_in_mail": extract_cves(all_text), "commit_ids_in_mail": commits,
            "evidence": evidence, "messages": items,
            "osv_candidates": prior.get("osv_candidates", []) if same_commits else [],
            "osv_query": prior.get("osv_query", {}) if same_commits else {},
            "aliases": prior.get("aliases", []),
        })
    records.sort(key=lambda r: (r["last_date"], r["title"]), reverse=True)
    if not records:
        raise ValueError(f"没有找到 {author_email} 的 patch。请确认邮箱和搜索归档。")
    return enrich_records(records, author_email), total


def osv_candidates(commit_ids):
    results = {}
    for commit_id in commit_ids:
        request = urllib.request.Request(
            "https://api.osv.dev/v1/query", data=json.dumps({"commit": commit_id}).encode(),
            headers={"Content-Type": "application/json", "User-Agent": "linux-patch-status-dashboard"})
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                results[commit_id] = {"state": "checked", "candidates": [
                    {"id": x.get("id", ""), "summary": x.get("summary", ""),
                     "commit": commit_id, "source": "https://osv.dev/vulnerability/" + quote(x.get("id", ""), safe="")}
                    for x in json.load(response).get("vulns", [])]}
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            results[commit_id] = {"state": "error", "candidates": [], "error": str(exc)}
    return results


def build_payload(source_name, records, total, author_email=DEFAULT_EMAIL, previous=None, online_cves=False):
    records = enrich_records(records, author_email)
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    if online_cves:
        osv = osv_candidates(sorted({x['sha'] for r in records for x in r['commit_refs'] if x['role'] not in {'introduced_by', 'referenced_commit'}}))
        for record in records:
            shas = {x['sha'] for x in record['commit_refs'] if x['role'] not in {'introduced_by', 'referenced_commit'}}
            record["osv_candidates"] = sorted(
                {x["id"]: x for c in shas for x in osv[c]['candidates']}.values(),
                key=lambda x: x["id"])
            record['osv_query'] = {'at': now, 'state': 'error' if any(osv[c]['state'] == 'error' for c in shas)
                                   else 'checked' if shas else 'unqueried', 'commits': sorted(shas),
                                   'errors': [osv[c]['error'] for c in shas if osv[c].get('error')]}
    previous = upgrade_payload(previous) if previous else {}
    prior_messages = {message_identity(m) for r in previous.get("records", []) for m in r["messages"]}
    current_messages = {m["message_id"] for r in records for m in r["messages"]}
    old_status = {subject_key(r["title"]): r["status"] for r in previous.get("records", [])}
    prior_imports = previous.get("imports", [])
    old_records = {r['id']: r for r in previous.get('records', [])}
    old_mail = {m['message_id']: m for r in previous.get('records', []) for m in r['messages']}
    changes = []
    for record in records:
        old = old_records.get(record['id'])
        added = [m['message_id'] for m in record['messages'] if m['message_id'] not in prior_messages]
        updated = [m['message_id'] for m in record['messages'] if m['message_id'] in old_mail and
                   any(m.get(k) != old_mail[m['message_id']].get(k) for k in ('body', 'subject', 'sender', 'date_iso'))]
        state_changed = bool(old and any(old.get(k) != record.get(k) for k in ('signals', 'attention_items', 'status')))
        if added or updated or state_changed:
            changes.append({'record_id': record['id'], 'title': record['title'], 'new_topic': old is None,
                            'added_message_ids': added, 'updated_message_ids': updated,
                            'before': old.get('status') if old else None, 'after': record['status'],
                            'status_changed': state_changed})
    imported = {"source": source_name, "at": now, "archive_messages": total,
                "added_messages": len(current_messages - prior_messages),
                "updated_messages": sum(len(c['updated_message_ids']) for c in changes)}
    payload = {
        "schema_version": 3, "source": source_name, "generated_at": now,
        "last_checked_at": now, "check_result": 'updated' if changes else 'unchanged',
        "content_updated_at": now if changes else previous.get('content_updated_at', now),
        "last_changes": {'at': now, 'items': changes} if changes else previous.get('last_changes', {}),
        "change_history": (previous.get('change_history', []) + ([{'at': now, 'items': changes}] if changes else []))[-20:],
        "revision": os.urandom(8).hex(), "author_email": author_email,
        "total_messages": total, "stored_messages": len(current_messages),
        "records": records, "status_counts": dict(Counter(r["status"] for r in records)),
        "cve_candidate_records": sum(bool(r["cves_in_mail"] or r["osv_candidates"]) for r in records),
        "months": dict(Counter(r["last_date"][:7] for r in records if r["last_date"])),
        "online_cves": online_cves,
        "imports": (prior_imports + [imported])[-50:],
        "last_import": {**imported,
            "added_patches": sum(subject_key(r["title"]) not in old_status for r in records),
            "changed_statuses": sum(subject_key(r["title"]) in old_status and
                                    old_status[subject_key(r["title"])] != r["status"] for r in records)}
    }
    return upgrade_payload(payload)


def render_report(payload):
    # Mail is untrusted content: JSON stays in a data element; reader uses textContent.
    data = json.dumps(payload, ensure_ascii=True).replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
    return TEMPLATE.read_text(encoding="utf-8").replace("__DATA__", data)


def load_payload(output):
    if not output.is_file():
        return None
    text = output.read_text(encoding="utf-8")
    marker = '<script id="report-data" type="application/json">'
    if marker in text:
        return json.loads(text.split(marker, 1)[1].split("</script>", 1)[0])
    # Migrate the first version's embedded data and CVE candidate annotations.
    if "const data = " in text:
        return json.JSONDecoder().raw_decode(text.split("const data = ", 1)[1])[0]
    raise ValueError("现有报告的数据无法读取，请选择另一个输出文件。")


def save_report(output, payload):
    output.parent.mkdir(parents=True, exist_ok=True)
    rendered = render_report(payload)  # Validate rendering before changing the existing report.
    descriptor, temporary = tempfile.mkstemp(prefix=".patch-report-", suffix=".tmp", dir=output.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(rendered)
        os.replace(temporary, output)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def build_report(source, records, total_messages, online_cves=False):
    """Compatibility helper for earlier CLI usage."""
    return render_report(build_payload(source.name, records, total_messages, online_cves=online_cves))


class DashboardState:
    def __init__(self, output, payload, author_email):
        self.output = output
        self.payload = payload
        self.author_email = author_email
        self.project_id = hashlib.sha256(str(output.resolve()).casefold().encode("utf-8")).hexdigest()
        self.lock = threading.Lock()
        self.job = {"state": "idle", "message": ""}

    def set_job(self, **values):
        with self.lock:
            self.job.update(values)

    def import_archive(self, source, filename):
        try:
            with self.lock:
                previous = self.payload
            records, total = parse_archive(
                source, self.author_email, (previous or {}).get("records", []),
                lambda message: self.set_job(message=message))
            payload = build_payload(filename, records, total, self.author_email, previous)
            save_report(self.output, payload)
            with self.lock:
                self.payload = payload
                self.job = {"state": "complete", "message": "导入完成，邮件和报告已保存。"}
        except Exception as exc:
            self.set_job(state="error", message=f"导入失败：{exc}。原报告已保留。")
        finally:
            source.unlink(missing_ok=True)


def make_server(state, port):
    class LocalServer(ThreadingHTTPServer):
        allow_reuse_address = os.name != "nt"
        allow_reuse_port = False

        def server_bind(self):
            # Windows SO_REUSEADDR permits two listeners on the same port.
            if os.name == "nt":
                self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            super().server_bind()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def reply(self, code, value, content_type="application/json; charset=utf-8"):
            body = json.dumps(value, ensure_ascii=True).encode() if not isinstance(value, bytes) else value
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def local_request(self):
            expected = f"127.0.0.1:{self.server.server_port}"
            host = self.headers.get("Host", "")
            origin = self.headers.get("Origin")
            return host == expected and (not origin or origin == "http://" + expected)

        def do_GET(self):
            if not self.local_request():
                return self.reply(403, {"error": "Local requests only"})
            route = urlparse(self.path).path
            with state.lock:
                payload = state.payload
                job = dict(state.job)
            if route == "/":
                if payload:
                    return self.reply(200, render_report(payload).encode(), "text/html; charset=utf-8")
                empty = {"records": [], "author_email": state.author_email, "revision": "empty"}
                return self.reply(200, render_report(empty).encode(), "text/html; charset=utf-8")
            if route == "/api/data":
                return self.reply(200, payload or {"records": [], "revision": "empty"})
            if route == "/api/status":
                return self.reply(200, {"job": job, "revision": (payload or {}).get("revision", "empty"),
                                        "application": "linux-patch-status", "project_id": state.project_id})
            self.reply(404, {"error": "Not found"})

        def do_POST(self):
            if not self.local_request() or self.headers.get("X-Patch-Import") != "1":
                return self.reply(403, {"error": "Local import required"})
            if self.path != "/api/import":
                return self.reply(404, {"error": "Not found"})
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = 0
            if not 0 < length <= MAX_UPLOAD:
                return self.reply(413, {"error": "请选择 512 MB 以内的 mbox 文件。"})
            with state.lock:
                if state.job["state"] in {"uploading", "processing"}:
                    return self.reply(409, {"error": "已有归档正在处理，请稍候。"})
                state.job = {"state": "uploading", "message": "正在接收归档…"}
            filename = Path(unquote(self.headers.get("X-Archive-Name", "import.mbox.gz"))).name
            fd, name = tempfile.mkstemp(prefix="patch-import-", suffix=".mbox")
            source = Path(name)
            try:
                self.connection.settimeout(60)
                with os.fdopen(fd, "wb") as dst:
                    remaining = length
                    while remaining:
                        chunk = self.rfile.read(min(1024 * 1024, remaining))
                        if not chunk:
                            raise ValueError("文件未传输完整")
                        dst.write(chunk)
                        remaining -= len(chunk)
                state.set_job(state="processing", message="正在解析归档…")
                threading.Thread(target=state.import_archive, args=(source, filename), daemon=True).start()
                self.reply(202, {"message": "归档已收到，正在分析。"})
            except Exception as exc:
                source.unlink(missing_ok=True)
                state.set_job(state="error", message=str(exc))
                self.reply(400, {"error": str(exc)})

    return LocalServer(("127.0.0.1", port), Handler)


def main():
    parser = argparse.ArgumentParser(description="Import lore mail and run your local patch dashboard")
    parser.add_argument("mbox_gz", type=Path, nargs="?", help=".mbox or .mbox.gz file")
    parser.add_argument("--author-email", default=DEFAULT_EMAIL)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--check-cve", action="store_true", help="Query OSV affected-version candidates (not fix mappings)")
    parser.add_argument("--serve", action="store_true", help="Open a local website with an archive import button")
    parser.add_argument("--open", action="store_true", help="Open the local website in the default browser")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    output = (args.output or Path(__file__).with_name("runyu-patch-status-cve.html")).resolve()
    previous = load_payload(output)
    payload = upgrade_payload(previous) if previous else None
    if previous and previous != payload:
        save_report(output, payload)
    if args.mbox_gz:
        source = args.mbox_gz.expanduser().resolve()
        if not source.is_file():
            parser.error(f"Input file does not exist: {source}")
        if source == output:
            parser.error("Input and output paths must differ")
        records, total = parse_archive(source, args.author_email, (previous or {}).get("records", []),
                                       lambda message: print(message, flush=True))
        payload = build_payload(source.name, records, total, args.author_email, previous, args.check_cve)
        save_report(output, payload)
        print(f"Parsed {total} messages into {len(records)} patch topics; "
              f"{payload['stored_messages']} full messages saved.", flush=True)
        print(f"Report: {output}", flush=True)
    elif not args.serve:
        parser.error("Provide a .mbox/.mbox.gz file, or use --serve")

    if args.serve:
        author_email = (payload or {}).get("author_email", args.author_email)
        state = DashboardState(output, payload, author_email)
        try:
            server = make_server(state, args.port)
        except OSError as exc:
            # Double-clicking the launcher again reopens this workspace's server.
            address = f"http://127.0.0.1:{args.port}"
            try:
                with urllib.request.urlopen(address + "/api/status", timeout=2) as response:
                    running = json.load(response)
                if running.get("application") == "linux-patch-status" and running.get("project_id") == state.project_id:
                    print(f"Already running: {address}", flush=True)
                    if args.open:
                        webbrowser.open(address)
                    return 0
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
                pass
            parser.error(f"Cannot start local server on port {args.port}: {exc}. Try --port 8766")
        address = f"http://127.0.0.1:{server.server_port}"
        print(f"Local dashboard: {address}\nKeep this process running; Ctrl+C stops it.", flush=True)
        if args.open:
            webbrowser.open(address)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
