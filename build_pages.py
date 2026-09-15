#!/usr/bin/env python3
"""Build a self-contained GitHub Pages snapshot, without accessing the network."""

import argparse
import json
import sys
from pathlib import Path

import patch_status_dashboard as dashboard

ROOT = Path(__file__).resolve().parent
SITE = ROOT / "docs" / "index.html"


def build(report=None, archive=None, sync_error=None):
    if archive is not None:
        archive = Path(archive).resolve()
        if not archive.is_file():
            raise ValueError(f"Archive does not exist: {archive}")
        previous = dashboard.load_payload(SITE) or {}
        author = previous.get("author_email", dashboard.DEFAULT_EMAIL)
        records, total = dashboard.parse_archive(
            archive, author, previous.get("records", []), lambda text: print(text, flush=True))
        payload = dashboard.build_payload(archive.name, records, total, author, previous)
    else:
        source = Path(report).resolve() if report is not None else SITE
        payload = dashboard.load_payload(source)
        if not payload or not isinstance(payload.get("records"), list):
            raise ValueError(f"No usable report data: {source}")
        previous = dashboard.load_payload(SITE) or {}
        before = {dashboard.message_identity(m) for r in previous.get("records", []) for m in r["messages"]}
        after = {dashboard.message_identity(m) for r in payload["records"] for m in r["messages"]}
        if before - after:
            raise ValueError("This report omits emails already in the site. Import the latest archive into your local dashboard first, or use --archive to merge.")
    if payload.get('private_mailbox') or payload.get('revision_reminders') or payload.get('hosting_mode') == 'mail_monitor' or any(
            m.get('provenance') == 'private_mailbox' for r in payload.get('records', []) for m in r['messages']):
        raise ValueError('Mailbox reports are private and cannot be published. Import the public Lore archive into the normal dashboard instead.')
    payload = dashboard.upgrade_payload(payload)
    check_payload = payload
    if previous and previous.get('schema_version') == 3:
        previous = dashboard.upgrade_payload(previous)
        # A successful check with no content changes updates only the tiny status file.
        if previous['records'] == payload['records'] and previous.get('author_email') == payload.get('author_email'):
            payload = dict(previous)
    payload["hosting_mode"] = "pages"
    payload["hosting_repository"] = "xry1/linux-patch-status"
    dashboard.save_report(SITE, payload)
    sync = {k: check_payload.get(k, '') for k in ('last_checked_at', 'content_updated_at', 'check_result', 'source')}
    sync['report_revision'] = payload.get('revision', '')
    if sync_error:
        sync.update(check_result='error', error=sync_error,
                    last_checked_at=dashboard.datetime.now().astimezone().isoformat(timespec='seconds'))
    status_file = SITE.with_name('sync-status.json')
    temporary = status_file.with_suffix('.json.tmp')
    temporary.write_text(json.dumps(sync, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temporary.replace(status_file)
    print(f"Built: {SITE}")
    print(f"{len(payload['records'])} topics; {payload.get('stored_messages', 0)} archived emails.")
    return {**payload, 'last_import': check_payload.get('last_import', {}), 'sync_status': sync}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--report", type=Path, help="Export the latest local dashboard HTML")
    source.add_argument("--archive", type=Path, help="Merge a new .mbox or .mbox.gz into the published snapshot")
    parser.add_argument('--sync-error', help='Publish a failed check result while preserving existing mail')
    args = parser.parse_args()
    try:
        build(args.report, args.archive, args.sync_error)
    except (OSError, ValueError) as exc:
        print(f"Build failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
