"""Resolve KarVPN versions and publish only explicitly written fork release notes."""

import argparse
import json
import os
import re
import shutil
import subprocess
import tomllib
from datetime import UTC, datetime
from pathlib import Path


def release_data(root: Path, requested_tag: str = '') -> tuple[str, str]:
    release = json.loads((root / 'karvpn-image-release.json').read_text())
    if (root / 'pyproject.toml').is_file():
        upstream = tomllib.loads((root / 'pyproject.toml').read_text())['project']['version']
    else:
        upstream = json.loads((root / 'karvpn-version.json').read_text())['upstream']
    if release['upstream_version'] != upstream or not re.fullmatch(r'\d+\.\d+\.\d+', upstream):
        raise ValueError('Sync upstream_version with pyproject.toml before building')
    revision = release['fork_revision']
    if type(revision) is not int or revision < 1:
        raise ValueError('fork_revision must be a positive integer')
    version = f'{upstream}-karvpn.{revision}'
    tag = f'v{version}'
    if (root / 'package.json').is_file() and json.loads((root / 'package.json').read_text())['version'] != version:
        raise ValueError('Sync package.json with the reviewed KarVPN version')
    if requested_tag and requested_tag != tag:
        raise ValueError('The requested tag does not match karvpn-image-release.json')
    return version, tag


def release_notes(root: Path, version: str) -> str:
    # Deliberately never scan git history, upstream CHANGELOG or release-please PRs.
    notes = (root / 'docs' / 'releases' / f'{version}.md').read_text().strip()
    if not notes.startswith(f'# {version}\n') or not re.search(r'^- .+', notes, re.MULTILINE):
        raise ValueError('Write a version heading and a short list of KarVPN changes')
    if len(notes) > 20000:
        raise ValueError('Keep KarVPN release notes below 20000 characters')
    return notes + '\n'


def image_repository() -> str:
    repository = os.environ['GITHUB_REPOSITORY'].lower()
    if not re.fullmatch(r'[a-z0-9_.-]+/[a-z0-9_.-]+', repository):
        raise ValueError('Invalid GitHub repository')
    return repository


def run_command(root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    executable = shutil.which(arguments[0])
    if not executable:
        raise ValueError('Required release tool is missing')
    # Arguments are fixed commands plus validated repository, tag and commit IDs.
    return subprocess.run(  # noqa: S603
        [executable, *arguments[1:]],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )


def check_publication(root: Path, tag: str) -> None:
    ref = os.environ['GITHUB_REF']
    if ref not in ('refs/heads/main', f'refs/tags/{tag}'):
        raise ValueError('Publish from main or the matching release tag; use preview for branches')
    commit = os.environ.get('KARVPN_RELEASE_SHA', os.environ['GITHUB_SHA'])
    if not re.fullmatch(r'[a-f0-9]{40}', commit) or not re.fullmatch(r'v\d+\.\d+\.\d+-karvpn\.[1-9]\d*', tag):
        raise ValueError('Invalid release commit or tag')
    ancestry = run_command(root, 'git', 'merge-base', '--is-ancestor', commit, 'origin/main')
    if ancestry.returncode != 0:
        raise ValueError('Release commit must belong to main')
    existing = run_command(root, 'git', 'rev-parse', '--verify', f'refs/tags/{tag}^{{commit}}')
    if existing.returncode == 0 and existing.stdout.strip() != commit:
        raise ValueError('The release tag already points to another commit; increase fork_revision')
    if existing.returncode not in (0, 128):
        raise ValueError('Could not verify the existing Git tag')
    response = run_command(root, 'gh', 'api', '--silent', f'repos/{image_repository()}/releases/tags/{tag}')
    if response.returncode == 0:
        raise ValueError('This release already exists; increase fork_revision instead of overwriting it')
    if 'HTTP 404' not in response.stderr:
        raise ValueError('Could not verify release availability on GitHub')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('metadata', 'notes'))
    parser.add_argument('--root', type=Path, default=Path.cwd())
    parser.add_argument('--tag', default='')
    parser.add_argument('--publish', action='store_true')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--image', default='')
    args = parser.parse_args()
    requested = args.tag
    if os.environ.get('GITHUB_REF_TYPE') == 'tag':
        requested = os.environ['GITHUB_REF_NAME']
    version, tag = release_data(args.root, requested)
    if args.tag and args.tag != tag:
        raise ValueError('The reusable build requested a different release version')
    if args.command == 'metadata':
        days = int(os.environ.get('TELEGRAM_AUTH_MAX_AGE_DAYS', '3650'))
        if not 1 <= days <= 36500:
            raise ValueError('Invalid TELEGRAM_AUTH_MAX_AGE_DAYS')
        values = {
            'version': version,
            'tag': tag,
            'image': f'ghcr.io/{image_repository()}',
            'build_date': datetime.now(UTC).isoformat(),
        }
        with Path(os.environ['GITHUB_OUTPUT']).open('a') as output:
            output.writelines(f'{key}={value}\n' for key, value in values.items())
        return
    notes = release_notes(args.root, version)
    if args.publish:
        check_publication(args.root, tag)
    if args.image:
        expected = f'ghcr.io/{image_repository()}@sha256:'
        if not args.image.startswith(expected) or not re.fullmatch(r'[a-f0-9]{64}', args.image[len(expected) :]):
            raise ValueError('Expected the immutable image reference returned by the tested build')
        notes += f'\nDocker image (`linux/amd64`):\n\n```text\n{args.image}\n```\n'
    if args.output:
        args.output.write_text(notes)
    else:
        print(notes, end='')


if __name__ == '__main__':
    main()
