#!/usr/bin/env python3
"""Build and publish only this dashboard's files. Never force-push."""

import argparse
import re
import subprocess
import sys
from pathlib import Path

from build_pages import ROOT, build

FILES = [
    ".gitignore", "README.md", "build_pages.py", "publish_pages.py",
    "patch_status_dashboard.py", "patch_dashboard_template.html", "patch_analysis.py", "test_analysis.py",
    "applied_evidence.py", "applied_verifications.json", "Verify-Applied.ps1", "audit_applied.py", "docs/applied-audit.json",
    "patch_series.py", "test_series.py",
    "mail_monitor.py", "mail_credentials.py", "api_transport.py", "test_mail_monitor.py", "test_mail_folders.py", "MAIL_MONITOR.md",
    "配置邮箱提醒.cmd", "启动邮箱监测.cmd",
    "生成网站.cmd", "发布网站.cmd", "docs/index.html", "docs/update.html", "docs/.nojekyll", "docs/sync-status.json",
]


def git(*args, check=True):
    result = subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True,
                            text=True, encoding="utf-8", errors="replace")
    if check and result.returncode:
        raise RuntimeError((result.stderr or result.stdout).strip() or "Git command failed")
    return result


def github_repository(value):
    match = re.fullmatch(r"(?:https://github\.com/|git@github\.com:)?([A-Za-z0-9-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?", value)
    if not match or match[2] in {".", ".."}:
        raise ValueError("Use OWNER/REPO or a GitHub repository URL without credentials.")
    return f"{match[1]}/{match[2]}"


def publish(repository=None):
    if not (ROOT / ".git").exists():
        git("init", "--initial-branch=main")
    if git("symbolic-ref", "--short", "HEAD").stdout.strip() != "main":
        raise RuntimeError("Publish from this repository's main branch; no branch was changed.")
    existing = git("remote", "get-url", "origin", check=False)
    if repository:
        target = github_repository(repository)
        if existing.returncode:
            git("remote", "add", "origin", f"https://github.com/{target}.git")
        elif github_repository(existing.stdout.strip()).casefold() != target.casefold():
            raise RuntimeError("origin points to another repository; it has not been changed.")
    else:
        if existing.returncode:
            raise RuntimeError("No origin configured. Run with --repository OWNER/REPO after creating the GitHub repository.")
        target = github_repository(existing.stdout.strip())
    for setting in ("user.name", "user.email"):
        if not git("config", "--get", setting, check=False).stdout.strip():
            raise RuntimeError(f"Configure your commit identity in this folder: git config {setting} YOUR_VALUE")
    staged = git("-c", "core.quotepath=false", "diff", "--cached", "--name-only").stdout.splitlines()
    if set(staged) - set(FILES):
        raise RuntimeError("Other files are staged. Commit or unstage those files before publishing this dashboard.")
    git("add", "--", *FILES)
    changes = git("diff", "--cached", "--quiet", check=False)
    if changes.returncode == 1:
        git("commit", "-m", "Update Linux patch status website")
    elif changes.returncode:
        raise RuntimeError(changes.stderr.strip())
    # Authentication uses the installed Git credential helper, not a stored script token.
    pushed = subprocess.run(["git", "-C", str(ROOT), "push", "--set-upstream", "origin", "main"])
    if pushed.returncode:
        raise RuntimeError("Push failed. Local files and commit are saved; resolve Git's reported issue and retry.")
    owner, repo = target.split("/")
    url = f"https://{owner.lower()}.github.io/" + ("" if repo.lower() == f"{owner.lower()}.github.io" else repo + "/")
    print(f"Pushed to GitHub. Expected Pages URL after deployment: {url}")
    print("First deployment: Settings > Pages > Deploy from a branch > main > /docs > Save.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--report", type=Path)
    source.add_argument("--archive", type=Path)
    parser.add_argument('--sync-error', help='Publish a failed check result without changing archived mail')
    parser.add_argument("--repository", help="Existing GitHub repository: OWNER/REPO")
    args = parser.parse_args()
    try:
        if args.repository:
            github_repository(args.repository)
        build(args.report, args.archive, args.sync_error)
        publish(args.repository)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"Publish failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
