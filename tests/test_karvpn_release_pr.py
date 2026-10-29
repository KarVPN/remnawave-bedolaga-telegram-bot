"""Release state transitions use local Git and mocked API, never publish anything."""

import base64
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import karvpn_release_pr as release


class ReleasePrTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repo = 'KarVPN/example'
        self.git('init', '-b', 'main')
        self.git('config', 'user.name', 'Release test')
        self.git('config', 'user.email', 'test@example.invalid')
        self.write('pyproject.toml', '[project]\nversion = "5.0.0"\n')
        self.write('karvpn-image-release.json', json.dumps({'upstream_version': '5.0.0', 'fork_revision': 1}))
        self.write('CHANGELOG.md', 'UPSTREAM HISTORY\n' * 10000)
        self.write('docs/releases/5.0.0-karvpn.1.md', '# 5.0.0-karvpn.1\n\n- Подготовленные изменения KarVPN.\n')
        self.commit('chore: baseline')
        self.base = self.git('rev-parse', 'HEAD').strip()
        self.config = {'component': 'example', 'bootstrap_sha': self.base}
        self.write('karvpn-release-config.json', json.dumps(self.config))
        self.git('update-ref', 'refs/remotes/origin/main', self.base)

    def git(self, *args):
        return release.command(self.root, 'git', *args)

    def write(self, name, content):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    def commit(self, message):
        self.git('add', '.')
        self.git('commit', '-m', message)

    def published(self, version='5.0.0-karvpn.1'):
        return [{'tag_name': f'v{version}', 'draft': False, 'created_at': '2026-10-22', 'published_at': '2026-10-22'}]

    def merged_pr(self, version='5.0.0-karvpn.1'):
        return {
            'number': 7,
            'merged_at': '2026-10-23',
            'body': f'<!-- karvpn-release:{version} -->',
            'head': {'ref': release.branch(self.config), 'repo': {'full_name': self.repo}},
            'merge_commit_sha': self.base,
        }

    def test_first_revision_and_separate_counter(self):
        self.assertEqual(release.next_version('5.0.0', []), '5.0.0-karvpn.1')
        self.assertEqual(release.next_version('5.0.0', self.published()), '5.0.0-karvpn.2')
        self.assertEqual(release.next_version('5.0.0', self.published('5.0.0-karvpn.9')), '5.0.0-karvpn.10')
        self.assertEqual(release.next_version('5.1.0', self.published()), '5.1.0-karvpn.1')
        with self.assertRaises(ValueError):
            release.next_version('4.99.9', self.published())

    def test_first_release_keeps_curated_notes_without_upstream_history(self):
        self.write('feature.txt', 'new')
        self.commit('feat(users): Уведомлять о продлении')
        plan = release.proposal(self.root, self.repo, self.config, [])
        notes = plan['files']['docs/releases/5.0.0-karvpn.1.md']
        self.assertIn('Подготовленные изменения', notes)
        self.assertIn('Уведомлять о продлении', notes)
        self.assertNotIn('UPSTREAM HISTORY', notes)
        self.assertNotIn('pyproject.toml', plan['files'])

    def test_release_merge_does_not_generate_another_pr(self):
        self.commit('chore(main): release 5.0.0-karvpn.1')
        self.git('tag', 'v5.0.0-karvpn.1')
        self.assertIsNone(release.proposal(self.root, self.repo, self.config, self.published()))
        with patch('scripts.karvpn_release_pr.pages', return_value=[self.merged_pr()]):
            self.assertEqual(
                release.pending_release(self.root, self.repo, self.config, self.published()), {'publish': 'false'}
            )

    def test_only_new_main_changes_are_in_the_next_notes(self):
        self.git('tag', 'v5.0.0-karvpn.1')
        self.write('feature.txt', 'new')
        self.commit('Merge pull request #8 from KarVPN/feature\n\nИсправить окно подтверждения')
        plan = release.proposal(self.root, self.repo, self.config, self.published())
        self.assertEqual(plan['version'], '5.0.0-karvpn.2')
        self.assertEqual(plan['files']['docs/releases/5.0.0-karvpn.2.md'].count('- '), 1)

    def test_imported_upstream_commits_are_not_expanded(self):
        self.git('checkout', '-b', 'upstream')
        for index in range(3):
            self.write(f'upstream-{index}', 'imported')
            self.commit(f'feat: UPSTREAM_RECORD_{index}')
        self.git('checkout', 'main')
        self.git('merge', '--no-ff', 'upstream', '-m', 'feat: Синхронизация оригинала')
        plan = release.proposal(self.root, self.repo, self.config, [])
        notes = plan['files']['docs/releases/5.0.0-karvpn.1.md']
        self.assertIn('Синхронизация оригинала', notes)
        self.assertNotIn('UPSTREAM_RECORD', notes)

    def test_cabinet_version_and_lock_stay_in_sync(self):
        (self.root / 'pyproject.toml').unlink()
        self.write('karvpn-version.json', json.dumps({'upstream': '5.0.0'}))
        self.write('package.json', json.dumps({'name': 'cabinet', 'version': '5.0.0-karvpn.1'}))
        self.write(
            'package-lock.json',
            json.dumps(
                {
                    'version': '5.0.0-karvpn.1',
                    'packages': {'': {'version': '5.0.0-karvpn.1'}, 'dependency': {'version': '1.2.3'}},
                }
            ),
        )
        self.commit('feat: кабинет')
        plan = release.proposal(self.root, self.repo, self.config, [])
        lock = json.loads(plan['files']['package-lock.json'])
        self.assertEqual(lock['version'], plan['version'])
        self.assertEqual(lock['packages']['']['version'], plan['version'])
        self.assertEqual(lock['packages']['dependency']['version'], '1.2.3')

    def test_wrong_upstream_is_rejected(self):
        self.write('karvpn-image-release.json', json.dumps({'upstream_version': '6.0.0', 'fork_revision': 1}))
        with self.assertRaises(ValueError):
            release.proposal(self.root, self.repo, self.config, [])

    def test_pending_release_builds_the_reviewed_merge_commit(self):
        descriptor = {'upstream_version': '5.0.0', 'fork_revision': 1}
        with (
            patch('scripts.karvpn_release_pr.pages', return_value=[self.merged_pr()]),
            patch(
                'scripts.karvpn_release_pr.api',
                return_value={'content': base64.b64encode(json.dumps(descriptor).encode()).decode()},
            ),
        ):
            result = release.pending_release(self.root, self.repo, self.config, [])
        self.assertEqual(
            result, {'publish': 'true', 'version': '5.0.0-karvpn.1', 'tag': 'v5.0.0-karvpn.1', 'commit': self.base}
        )

    def test_mismatched_or_skipped_revision_in_merged_pr_is_rejected(self):
        descriptor = {'upstream_version': '5.0.0', 'fork_revision': 9}
        with (
            patch('scripts.karvpn_release_pr.pages', return_value=[self.merged_pr('5.0.0-karvpn.9')]),
            patch(
                'scripts.karvpn_release_pr.api',
                return_value={'content': base64.b64encode(json.dumps(descriptor).encode()).decode()},
            ),
            self.assertRaises(ValueError),
        ):
            release.pending_release(self.root, self.repo, self.config, [])

    def test_regular_pr_and_deleted_head_repo_do_not_publish(self):
        pr = self.merged_pr()
        pr['head']['repo'] = None
        with patch('scripts.karvpn_release_pr.pages', return_value=[pr]):
            self.assertEqual(release.pending_release(self.root, self.repo, self.config, []), {'publish': 'false'})
        pr = self.merged_pr()
        pr['head']['ref'] = 'feature'
        with patch('scripts.karvpn_release_pr.pages', return_value=[pr]):
            self.assertEqual(release.pending_release(self.root, self.repo, self.config, []), {'publish': 'false'})

    def test_existing_release_pr_number_is_reused_without_force_push(self):
        self.write('feature.txt', 'new')
        self.commit('feat: New feature')
        plan = release.proposal(self.root, self.repo, self.config, [])
        pr = {
            'number': 7,
            'title': 'old',
            'body': 'old',
            'head': {'ref': plan['branch'], 'repo': {'full_name': self.repo}},
        }
        writes = []

        def api(_root, endpoint, method='GET', payload=None):
            if method != 'GET':
                writes.append((endpoint, method, payload))
                return {'sha': 'new-tree' if endpoint.endswith('/trees') else 'new-commit'}
            if endpoint.endswith('/heads/main'):
                return {'object': {'sha': plan['head']}}
            return {'tree': {'sha': 'old-tree'}}

        def pages(_root, endpoint):
            if '/pulls?' in endpoint:
                return [pr]
            return [{'ref': f'refs/heads/{plan["branch"]}', 'object': {'sha': 'old-head'}}]

        with (
            patch('scripts.karvpn_release_pr.api', side_effect=api),
            patch('scripts.karvpn_release_pr.pages', side_effect=pages),
        ):
            release.update_proposal(self.root, self.repo, plan)
        self.assertTrue(any(endpoint.endswith('/pulls/7') and method == 'PATCH' for endpoint, method, _ in writes))
        self.assertFalse(any(endpoint.endswith('/pulls') and method == 'POST' for endpoint, method, _ in writes))
        ref_update = next(payload for endpoint, _, payload in writes if '/git/refs/' in endpoint)
        self.assertFalse(ref_update['force'])
        commit = next(payload for endpoint, _, payload in writes if endpoint.endswith('/git/commits'))
        self.assertEqual(commit['parents'], ['old-head', plan['head']])

    def test_stale_main_is_rejected_before_writing(self):
        plan = {'head': 'old'}
        with (
            patch('scripts.karvpn_release_pr.api', return_value={'object': {'sha': 'new'}}) as api,
            self.assertRaises(ValueError),
        ):
            release.update_proposal(self.root, self.repo, plan)
        self.assertEqual(api.call_count, 1)

    def test_dry_run_never_writes_to_github(self):
        with (
            patch.dict(os.environ, {'GITHUB_REF': 'refs/heads/main', 'GITHUB_REPOSITORY': self.repo}),
            patch('sys.argv', ['release', 'propose', '--root', str(self.root), '--dry-run']),
            patch('scripts.karvpn_release_pr.released', return_value=[]),
            patch(
                'scripts.karvpn_release_pr.proposal',
                return_value={'version': '5.0.0-karvpn.1', 'files': {'notes': 'text'}},
            ),
            patch('scripts.karvpn_release_pr.update_proposal') as update,
        ):
            release.main()
        update.assert_not_called()

    def test_no_publication_from_feature_branch(self):
        with (
            patch.dict(os.environ, {'GITHUB_REF': 'refs/heads/feature'}),
            patch('sys.argv', ['release', 'pending']),
            self.assertRaises(ValueError),
        ):
            release.main()

    def test_notes_are_bounded_and_do_not_mention_users(self):
        files = release.release_files(self.root, '5.0.0-karvpn.2', ['x' * 400] * 100, initial=False)
        notes = files['docs/releases/5.0.0-karvpn.2.md']
        self.assertLess(len(notes), 20000)
        self.assertIn('Ещё изменений: 70', notes)


if __name__ == '__main__':
    unittest.main()
