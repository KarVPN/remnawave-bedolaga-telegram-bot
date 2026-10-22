"""Protect upstream versions, explicit fork notes and immutable published releases."""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.karvpn_release import check_publication, main, release_data, release_notes


class KarvpnReleaseTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / 'pyproject.toml').write_text('[project]\nversion = "5.0.0"\n')
        self.descriptor = self.root / 'karvpn-image-release.json'
        self.descriptor.write_text(json.dumps({'upstream_version': '5.0.0', 'fork_revision': 1}))
        self.notes = self.root / 'docs/releases/5.0.0-karvpn.1.md'
        self.notes.parent.mkdir(parents=True)
        self.notes.write_text('# 5.0.0-karvpn.1\n\n- Only our fork change.\n')
        (self.root / 'CHANGELOG.md').write_text('UPSTREAM HISTORY\n' * 10000)
        self.environment = patch.dict(
            os.environ,
            {
                'GITHUB_REF': 'refs/heads/main',
                'GITHUB_REF_TYPE': 'branch',
                'GITHUB_REPOSITORY': 'KarVPN/remnawave-bedolaga-telegram-bot',
                'GITHUB_SHA': 'a' * 40,
            },
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_fork_revision_keeps_upstream_untouched(self):
        previous = (self.root / 'pyproject.toml').read_bytes()
        self.assertEqual(release_data(self.root), ('5.0.0-karvpn.1', 'v5.0.0-karvpn.1'))
        self.descriptor.write_text(json.dumps({'upstream_version': '5.0.0', 'fork_revision': 2}))
        self.assertEqual(release_data(self.root)[0], '5.0.0-karvpn.2')
        self.assertEqual((self.root / 'pyproject.toml').read_bytes(), previous)

    def test_upstream_version_mismatch_and_invalid_revisions_are_rejected(self):
        for upstream, revision in (('6.0.0', 1), ('5.0.0', 0), ('5.0.0', True), ('5.0.0', '1')):
            self.descriptor.write_text(json.dumps({'upstream_version': upstream, 'fork_revision': revision}))
            with self.assertRaises(ValueError):
                release_data(self.root)

    def test_wrong_release_tag_is_rejected(self):
        for tag in ('v6.0.0', 'v5.0.0-karvpn.2'):
            with self.assertRaises(ValueError):
                release_data(self.root, tag)

    def test_notes_use_only_the_current_explicit_file(self):
        with patch('scripts.karvpn_release.run_command') as command:
            notes = release_notes(self.root, '5.0.0-karvpn.1')
        self.assertEqual(notes, self.notes.read_text())
        self.assertNotIn('UPSTREAM HISTORY', notes)
        command.assert_not_called()

    def test_missing_wrong_or_oversized_notes_fail_instead_of_scanning_history(self):
        for content in ('# 6.0.0\n\n- Wrong version.\n', '# 5.0.0-karvpn.1\n', '# 5.0.0-karvpn.1\n\n- ' + 'x' * 21000):
            self.notes.write_text(content)
            with self.assertRaises(ValueError):
                release_notes(self.root, '5.0.0-karvpn.1')
        self.notes.unlink()
        with self.assertRaises(FileNotFoundError):
            release_notes(self.root, '5.0.0-karvpn.1')

    def test_metadata_uses_the_same_version_as_the_release(self):
        output = self.root / 'outputs'
        with (
            patch.dict(os.environ, {'GITHUB_OUTPUT': str(output)}),
            patch(
                'sys.argv',
                [
                    'karvpn_release.py',
                    'metadata',
                    '--root',
                    str(self.root),
                    '--tag',
                    'v5.0.0-karvpn.1',
                ],
            ),
        ):
            main()
        values = dict(line.split('=', 1) for line in output.read_text().splitlines())
        self.assertEqual(values['version'], '5.0.0-karvpn.1')
        self.assertEqual(values['tag'], 'v5.0.0-karvpn.1')
        self.assertEqual(values['image'], 'ghcr.io/karvpn/remnawave-bedolaga-telegram-bot')

    def test_release_body_contains_only_curated_notes_and_the_tested_digest(self):
        output = self.root / 'release.md'
        image = 'ghcr.io/karvpn/remnawave-bedolaga-telegram-bot@sha256:' + 'b' * 64
        with patch(
            'sys.argv',
            [
                'karvpn_release.py',
                'notes',
                '--root',
                str(self.root),
                '--image',
                image,
                '--output',
                str(output),
            ],
        ):
            main()
        body = output.read_text()
        self.assertIn('Only our fork change.', body)
        self.assertIn(image, body)
        self.assertNotIn('UPSTREAM HISTORY', body)

    def test_release_body_rejects_a_mutable_or_foreign_image(self):
        for image in ('ghcr.io/karvpn/remnawave-bedolaga-telegram-bot:main', 'ghcr.io/other/bot@sha256:' + 'b' * 64):
            with (
                patch(
                    'sys.argv',
                    [
                        'karvpn_release.py',
                        'notes',
                        '--root',
                        str(self.root),
                        '--image',
                        image,
                    ],
                ),
                self.assertRaises(ValueError),
            ):
                main()

    def publication_commands(self, *, tag_commit=None, release_status=404, ancestor=True):
        def run(_root, *arguments):
            if arguments[:2] == ('git', 'merge-base'):
                return subprocess.CompletedProcess(arguments, 0 if ancestor else 1)
            if arguments[:2] == ('git', 'rev-parse'):
                return subprocess.CompletedProcess(arguments, 0 if tag_commit else 128, stdout=(tag_commit or ''))
            return subprocess.CompletedProcess(
                arguments, 0 if release_status == 200 else 1, stderr=f'HTTP {release_status}'
            )

        return patch('scripts.karvpn_release.run_command', side_effect=run)

    def test_preview_branch_cannot_publish(self):
        with patch.dict(os.environ, {'GITHUB_REF': 'refs/heads/codex/test'}), self.publication_commands():
            with self.assertRaises(ValueError):
                check_publication(self.root, 'v5.0.0-karvpn.1')

    def test_new_release_from_main_can_publish(self):
        with self.publication_commands():
            check_publication(self.root, 'v5.0.0-karvpn.1')

    def test_matching_release_tag_on_main_can_publish(self):
        with (
            patch.dict(os.environ, {'GITHUB_REF': 'refs/tags/v5.0.0-karvpn.1'}),
            self.publication_commands(tag_commit='a' * 40),
        ):
            check_publication(self.root, 'v5.0.0-karvpn.1')

    def test_release_overwrite_different_tag_commit_and_network_errors_are_rejected(self):
        for options in (
            {'release_status': 200},
            {'tag_commit': 'b' * 40},
            {'release_status': 500},
            {'ancestor': False},
        ):
            with self.publication_commands(**options), self.assertRaises(ValueError):
                check_publication(self.root, 'v5.0.0-karvpn.1')


if __name__ == '__main__':
    unittest.main()
