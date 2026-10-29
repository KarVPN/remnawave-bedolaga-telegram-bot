"""Shared automatic KarVPN release PRs; publication remains a reviewed operation."""

import argparse
import base64
import json
import os
import re
import subprocess
import tomllib
from pathlib import Path


VERSION = re.compile(r'(\d+\.\d+\.\d+)-karvpn\.([1-9]\d*)\Z')
MARKER = re.compile(r'<!-- karvpn-release:(\d+\.\d+\.\d+-karvpn\.[1-9]\d*) -->')


def command(root, *arguments, payload=None):
    result = subprocess.run(  # noqa: S603 -- argv, never shell; validated repo/ref and fixed commands
        list(arguments),
        cwd=root,
        input=payload,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        # Do not print subprocess stderr: credentials and server replies are not release notes.
        raise RuntimeError(f'{arguments[0]} {arguments[1]} failed (exit {result.returncode})')
    return result.stdout


def api(root, endpoint, method='GET', payload=None):
    arguments = ['gh', 'api', '--method', method, endpoint]
    if payload is not None:
        arguments += ['--input', '-']
    output = command(root, *arguments, payload=json.dumps(payload) if payload is not None else None)
    return json.loads(output) if output.strip() else None


def pages(root, endpoint):
    result = []
    page = 1
    while True:
        items = api(root, f'{endpoint}{"&" if "?" in endpoint else "?"}per_page=100&page={page}')
        result.extend(items)
        if len(items) < 100:
            return result
        page += 1


def repository():
    value = os.environ['GITHUB_REPOSITORY']
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', value):
        raise ValueError('Invalid GitHub repository')
    return value


def configuration(root):
    value = json.loads((root / 'karvpn-release-config.json').read_text())
    if not re.fullmatch(r'[a-z0-9-]+', value['component']):
        raise ValueError('Invalid release component')
    if not re.fullmatch(r'[a-f0-9]{40}', value['bootstrap_sha']):
        raise ValueError('Pin the release bootstrap commit')
    return value


def branch(config):
    return f'release-please--branches--main--components--{config["component"]}'


def released(root, repo):
    return [
        item
        for item in pages(root, f'repos/{repo}/releases')
        if not item['draft'] and VERSION.fullmatch(item['tag_name'].removeprefix('v'))
    ]


def next_version(upstream, releases):
    if not re.fullmatch(r'\d+\.\d+\.\d+', upstream):
        raise ValueError('Invalid upstream version')
    if not releases:
        return f'{upstream}-karvpn.1'
    latest = max(releases, key=lambda item: item.get('published_at') or item['created_at'])
    previous = VERSION.fullmatch(latest['tag_name'].removeprefix('v'))
    current_parts = tuple(map(int, upstream.split('.')))
    previous_parts = tuple(map(int, previous[1].split('.')))
    if current_parts < previous_parts:
        raise ValueError('Refusing an upstream downgrade')
    revision = int(previous[2]) + 1 if current_parts == previous_parts else 1
    return f'{upstream}-karvpn.{revision}'


def local_changes(root, base, head):
    """Only main's first-parent changes: never expand an imported upstream history."""
    command(root, 'git', 'merge-base', '--is-ancestor', base, head)
    log = command(root, 'git', 'log', '--first-parent', '--reverse', '--format=%B%x00', f'{base}..{head}')
    changes = []
    for message in log.split('\0'):
        lines = [line.strip() for line in message.strip().splitlines() if line.strip()]
        if not lines:
            continue
        title = lines[1] if lines[0].startswith('Merge pull request #') and len(lines) > 1 else lines[0]
        if re.match(r'^chore(?:\([^)]*\))?: release\s', title):
            continue
        title = re.sub(r'^[a-z]+(?:\([^)]*\))?!?:\s*', '', title)
        title = title.replace('<', '&lt;').replace('>', '&gt;').replace('@', '＠')
        if title not in changes:
            changes.append(title[:400])
    return changes


def release_files(root, version, changes, initial):
    match = VERSION.fullmatch(version)
    descriptor = {'upstream_version': match[1], 'fork_revision': int(match[2])}
    note_path = f'docs/releases/{version}.md'
    existing = root / note_path
    notes = (
        existing.read_text().strip()
        if initial and existing.is_file()
        else f'# {version}\n\nОснова: {match[1]}. Изменения форка KarVPN:'
    )
    visible = changes[:30]
    notes += '\n\n' + '\n'.join(f'- {title}' for title in visible) + '\n'
    if len(changes) > len(visible):
        notes += f'\nЕщё изменений: {len(changes) - len(visible)}. Полный состав проверяйте в сравнении коммитов релизного PR.\n'
    if len(notes) > 20000:
        raise ValueError('Shorten release notes before publication')
    files = {'karvpn-image-release.json': json.dumps(descriptor, indent=2) + '\n', note_path: notes}
    if (root / 'package.json').is_file():
        package = json.loads((root / 'package.json').read_text())
        package['version'] = version
        files['package.json'] = json.dumps(package, ensure_ascii=False, indent=2) + '\n'
        lock = json.loads((root / 'package-lock.json').read_text())
        lock['version'] = version
        lock['packages']['']['version'] = version
        files['package-lock.json'] = json.dumps(lock, ensure_ascii=False, indent=2) + '\n'
        files['.release-please-manifest.json'] = json.dumps({'.': version}, indent=2) + '\n'
    return files


def proposal(root, repo, config, releases):
    head = command(root, 'git', 'rev-parse', 'HEAD').strip()
    descriptor = json.loads((root / 'karvpn-image-release.json').read_text())
    upstream = descriptor['upstream_version']
    # Validate the adopted source version before proposing a number.
    if (root / 'pyproject.toml').is_file():
        source = tomllib.loads((root / 'pyproject.toml').read_text())['project']['version']
    else:
        source = json.loads((root / 'karvpn-version.json').read_text())['upstream']
    if source != upstream:
        raise ValueError('Synchronize the adopted upstream version first')
    latest = max(releases, key=lambda item: item.get('published_at') or item['created_at']) if releases else None
    base = latest['tag_name'] if latest else config['bootstrap_sha']
    changes = local_changes(root, base, head)
    if not changes:
        return None
    version = next_version(upstream, releases)
    files = release_files(root, version, changes, initial=latest is None)
    notes = files[f'docs/releases/{version}.md']
    body = f'{notes}\n<!-- karvpn-release:{version} -->\n\nАвтоматический релизный PR. Правки заметок для публикации вносите в файл docs/releases этой ветки. Проверьте версию и заметки, разрешите проверки GitHub при запросе и слейте PR. После слияния KarVPN Release проверит и опубликует Docker-образ, затем создаст релиз с digest. VPS обновляется отдельно через Ansible.\n'
    return {
        'head': head,
        'version': version,
        'files': files,
        'body': body,
        'title': f'chore(main): release {version}',
        'branch': branch(config),
    }


def update_proposal(root, repo, plan):
    # Do not overwrite a proposal based on a stale checkout of main.
    if api(root, f'repos/{repo}/git/ref/heads/main')['object']['sha'] != plan['head']:
        raise ValueError('Main advanced; rerun KarVPN Release from main')
    prs = pages(root, f'repos/{repo}/pulls?state=open&base=main')
    existing = next(
        (
            pr
            for pr in prs
            if pr['head']['ref'] == plan['branch']
            and (pr['head'].get('repo') or {}).get('full_name', '').lower() == repo.lower()
        ),
        None,
    )
    base = api(root, f'repos/{repo}/git/commits/{plan["head"]}')
    tree = api(
        root,
        f'repos/{repo}/git/trees',
        'POST',
        {
            'base_tree': base['tree']['sha'],
            'tree': [
                {'path': path, 'mode': '100644', 'type': 'blob', 'content': content}
                for path, content in plan['files'].items()
            ],
        },
    )
    refs = pages(root, f'repos/{repo}/git/matching-refs/heads/{plan["branch"]}')
    ref = next((item for item in refs if item['ref'] == f'refs/heads/{plan["branch"]}'), None)
    old = ref['object']['sha'] if ref else plan['head']
    current = api(root, f'repos/{repo}/git/commits/{old}')
    if current['tree']['sha'] != tree['sha']:
        parents = [old] if old == plan['head'] else [old, plan['head']]
        commit = api(
            root,
            f'repos/{repo}/git/commits',
            'POST',
            {'message': plan['title'], 'tree': tree['sha'], 'parents': parents},
        )
        if ref:
            # Fast-forward only: concurrent manual edits fail instead of being discarded.
            api(root, f'repos/{repo}/git/refs/heads/{plan["branch"]}', 'PATCH', {'sha': commit['sha'], 'force': False})
        else:
            api(root, f'repos/{repo}/git/refs', 'POST', {'ref': f'refs/heads/{plan["branch"]}', 'sha': commit['sha']})
    payload = {'title': plan['title'], 'body': plan['body']}
    if existing:
        if existing['title'] != plan['title'] or existing['body'] != plan['body']:
            api(root, f'repos/{repo}/pulls/{existing["number"]}', 'PATCH', payload)
    else:
        api(root, f'repos/{repo}/pulls', 'POST', {**payload, 'head': plan['branch'], 'base': 'main'})


def pending_release(root, repo, config, releases):
    published = {item['tag_name'] for item in releases}
    prs = pages(root, f'repos/{repo}/pulls?state=closed&base=main')
    for pr in sorted((item for item in prs if item['merged_at']), key=lambda item: item['merged_at'], reverse=True):
        marker = MARKER.search(pr.get('body') or '')
        if (
            not marker
            or pr['head']['ref'] != branch(config)
            or (pr['head'].get('repo') or {}).get('full_name', '').lower() != repo.lower()
        ):
            continue
        version = marker[1]
        tag = f'v{version}'
        if tag in published:
            continue
        sha = pr['merge_commit_sha']
        command(root, 'git', 'merge-base', '--is-ancestor', sha, 'origin/main')
        data = api(root, f'repos/{repo}/contents/karvpn-image-release.json?ref={sha}')
        descriptor = json.loads(base64.b64decode(data['content']))
        expected = f'{descriptor["upstream_version"]}-karvpn.{descriptor["fork_revision"]}'
        if version != expected or version != next_version(descriptor['upstream_version'], releases):
            raise ValueError('Merged release PR version does not match its reviewed files')
        return {'publish': 'true', 'version': version, 'tag': tag, 'commit': sha}
    return {'publish': 'false'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('pending', 'propose'))
    parser.add_argument('--root', type=Path, default=Path.cwd())
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    if os.environ['GITHUB_REF'] != 'refs/heads/main':
        raise ValueError('Run release automation from main')
    root, repo = args.root, repository()
    config = configuration(root)
    releases = released(root, repo)
    if args.command == 'pending':
        result = pending_release(root, repo, config, releases)
        if os.environ.get('GITHUB_OUTPUT'):
            with Path(os.environ['GITHUB_OUTPUT']).open('a') as output:
                output.writelines(f'{key}={value}\n' for key, value in result.items())
        print(json.dumps(result))
        return
    plan = proposal(root, repo, config, releases)
    if plan and not args.dry_run:
        update_proposal(root, repo, plan)
    print(
        json.dumps(
            {'version': plan['version'], 'files': list(plan['files'])} if plan else {'release_pr': 'not needed'},
            ensure_ascii=False,
        )
    )


if __name__ == '__main__':
    main()
