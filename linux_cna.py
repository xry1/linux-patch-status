"""Index explicit fixes from the Linux CNA's public Git repository.

The repository's cve/schema defines .sha1 as the original fix, and .dyad
as introduced-version:introduced-sha:fixed-version:fixed-sha pairs.
JSON references alone are deliberately never repair evidence.
"""
import json
import os
import re
import subprocess
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if ROOT.name == 'outputs':
    ROOT = ROOT.parent / 'github-pages'
REPOSITORY = 'https://git.kernel.org/pub/scm/linux/security/vulns.git'
CACHE = ROOT / 'local' / 'linux-cna-source'
INDEX = ROOT / 'local' / 'linux-cna-index.json'
SHA = re.compile(r'[0-9a-f]{40}')
CVE = re.compile(r'CVE-\d{4}-\d{4,}')


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(value, f, ensure_ascii=False)
            f.write('\n')
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def git(*args, cwd=CACHE):
    proxy = urllib.request.getproxies().get('https')
    command = ['git'] + (['-c', 'http.proxy=' + proxy] if proxy else [])
    result = subprocess.run(command + list(args), cwd=cwd, capture_output=True,
                            text=True, encoding='utf-8', errors='replace', timeout=300)
    if result.returncode:
        raise RuntimeError('Linux CNA Git operation failed: ' + args[0])
    return result.stdout.strip()


def update_source():
    if not CACHE.exists():
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        git('clone', '--depth', '1', REPOSITORY, str(CACHE), cwd=CACHE.parent)
    if git('remote', 'get-url', 'origin').rstrip('/') != REPOSITORY:
        raise ValueError('Unexpected Linux CNA remote')
    if git('status', '--porcelain'):
        raise ValueError('Linux CNA cache has local edits; preserved')
    try:
        git('fetch', 'origin')
    except RuntimeError:
        git('fetch', 'https://kernel.googlesource.com/pub/scm/linux/security/vulns', 'refs/heads/master')
    git('merge', '--ff-only', 'FETCH_HEAD')
    return git('rev-parse', 'HEAD')


def parse_record(path, revision):
    if 'rejected' in path.parts and CVE.fullmatch(path.stem):
        return {'id': path.stem, 'state': 'REJECTED', 'title': '', 'fixes': [],
                'source': REPOSITORY + '/tree/' + path.relative_to(CACHE).as_posix() + '?id=' + revision}
    value = json.loads(path.read_text(encoding='utf-8'))
    meta = value.get('cveMetadata', {})
    identifier = meta.get('cveId')
    if not identifier or identifier != path.stem or not CVE.fullmatch(identifier):
        raise ValueError('Invalid CNA identifier: ' + path.name)
    state = meta.get('state')
    if state not in {'PUBLISHED', 'REJECTED'}:
        raise ValueError('Unexpected CNA state: ' + path.name)
    # Moving a record to rejected wins even if its old JSON was retained.
    if 'rejected' in path.parts:
        state = 'REJECTED'
    cna = value.get('containers', {}).get('cna', {})
    fixes = []
    def proof(sha, kind, filename, evidence):
        if not SHA.fullmatch(sha):
            raise ValueError('Invalid fix SHA: ' + path.name)
        fixes.append({'sha': sha, 'kind': kind, 'field': filename.suffix,
            'evidence': evidence,
            'source': REPOSITORY + '/tree/' + filename.relative_to(CACHE).as_posix() + '?id=' + revision})
    sha_file = path.with_suffix('.sha1')
    if sha_file.exists():
        for sha in sha_file.read_text(encoding='utf-8').split():
            proof(sha, 'original_fix', sha_file, sha)
    dyad = path.with_suffix('.dyad')
    if dyad.exists():
        for line in dyad.read_text(encoding='utf-8').splitlines():
            if not line.strip() or line.startswith('#'):
                continue
            parts = line.split(':')
            if len(parts) != 4:
                raise ValueError('Unrecognized dyad: ' + path.name)
            if parts[3] in {'0', ''}:
                continue
            proof(parts[3], 'fixed_version', dyad, line)
    # JSON's exclusive affected upper bound is an explicit fixed boundary.
    for affected in cna.get('affected', []):
        if affected.get('product') != 'Linux' or not affected.get('repo', '').startswith('https://git.kernel.org/'):
            continue
        for version in affected.get('versions', []):
            sha = version.get('lessThan', '')
            if version.get('versionType') == 'git' and version.get('status') == 'affected' and SHA.fullmatch(sha):
                proof(sha, 'fixed_boundary', path, 'affected.versions: status=affected, versionType=git, lessThan=' + sha)
    return {'id': identifier, 'state': state, 'title': cna.get('title', ''),
            'updated_at': meta.get('dateUpdated', ''), 'fixes': fixes,
            'source': REPOSITORY + '/tree/' + path.relative_to(CACHE).as_posix() + '?id=' + revision}


def build_index(revision):
    old = json.loads(INDEX.read_text(encoding='utf-8')) if INDEX.exists() else {}
    entries = old.get('entries', {})
    if old.get('revision') == revision and old.get('schema') == 1:
        return old
    changed = None
    if old.get('revision') and old.get('schema') == 1:
        try:
            names = git('diff', '--name-only', old['revision'], revision, '--', 'cve').splitlines()
            changed = {m[0] for name in names for m in [CVE.search(name)] if m}
        except RuntimeError:
            pass  # Old shallow history unavailable: rebuild from current snapshot.
    if changed is None:
        entries = {}
        paths = list((CACHE / 'cve' / 'published').rglob('*.json'))
        paths += list((CACHE / 'cve' / 'rejected').rglob('*.json'))
    else:
        paths = []
        for identifier in changed:
            entries.pop(identifier, None)
            for state in ('published', 'rejected'):
                path = CACHE / 'cve' / state / identifier.split('-')[1] / (identifier + '.json')
                if path.exists():
                    paths.append(path)
    for path in paths:
        entry = parse_record(path, revision)
        entries[entry['id']] = entry
    if not entries:
        raise ValueError('Empty Linux CNA index')
    result = {'schema': 1, 'revision': revision, 'entries': entries,
              'source_committed_at': git('show', '-s', '--format=%cI', revision)}
    atomic_json(INDEX, result)
    return result


def reverse_index(index):
    result = {}
    for entry in index['entries'].values():
        if entry['state'] != 'PUBLISHED':
            continue
        for fix in entry['fixes']:
            result.setdefault(fix['sha'], []).append({
                'id': entry['id'], 'title': entry['title'], 'commit': fix['sha'],
                'source': entry['source'], 'proof': fix})
    return result


def cvelist_check(identifier, shas):
    from api_transport import windows_json
    year, number = identifier.split('-')[1:]
    path = 'cves/' + year + '/' + number[:-3] + 'xxx/' + identifier + '.json'
    url = 'https://raw.githubusercontent.com/CVEProject/cvelistV5/main/' + path
    value = windows_json(url)
    meta = value.get('cveMetadata', {})
    if meta.get('cveId') != identifier:
        raise ValueError('CVE record identifier mismatch')
    fixed = set()
    for affected in value.get('containers', {}).get('cna', {}).get('affected', []):
        if affected.get('product') != 'Linux' or not affected.get('repo', '').startswith('https://git.kernel.org/'):
            continue
        fixed.update(v['lessThan'] for v in affected.get('versions', [])
                     if v.get('status') == 'affected' and v.get('versionType') == 'git'
                     and SHA.fullmatch(v.get('lessThan', '')))
    return {'id': identifier, 'state': meta.get('state'), 'updated_at': meta.get('dateUpdated', ''),
            'matching_fixed': sorted(fixed & set(shas)), 'source': url}
