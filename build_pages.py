#!/usr/bin/env python3
"""Build a self-contained GitHub Pages snapshot, without accessing the network."""

import argparse
import sys
from pathlib import Path

import patch_status_dashboard as dashboard

ROOT = Path(__file__).resolve().parent
SITE = ROOT / "docs" / "index.html"


def build(report=None, archive=None):
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
    payload = dict(payload)
    payload["hosting_mode"] = "pages"
    dashboard.save_report(SITE, payload)
    print(f"Built: {SITE}")
    print(f"{len(payload['records'])} topics; {payload.get('stored_messages', 0)} archived emails.")
    return payload


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--report", type=Path, help="Export the latest local dashboard HTML")
    source.add_argument("--archive", type=Path, help="Merge a new .mbox or .mbox.gz into the published snapshot")
    args = parser.parse_args()
    try:
        build(args.report, args.archive)
    except (OSError, ValueError) as exc:
        print(f"Build failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
