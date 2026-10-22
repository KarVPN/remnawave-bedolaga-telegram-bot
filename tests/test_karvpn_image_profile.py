"""The image must retain KarVPN initData policy without changing widget auth."""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from scripts.prepare_karvpn_image import prepare


class ImageProfileTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        source = Path(__file__).resolve().parents[1]
        for name in ('cabinet', 'utils'):
            shutil.copytree(source / 'app' / name, self.root / 'app' / name)

    def test_patch_real_source_and_keep_widget_policy(self):
        widget = self.root / 'app/cabinet/auth/telegram_auth.py'
        previous = widget.read_bytes()
        prepare(self.root, 3650)
        profile = json.loads((self.root / 'karvpn-image-profile.json').read_text())
        self.assertEqual(profile['telegram_auth_max_age_days'], 3650)
        self.assertEqual(profile['init_data_call_sites'], 5)
        self.assertEqual(widget.read_bytes(), previous)
        prepare(self.root, 3650)
        self.assertEqual(json.loads((self.root / 'karvpn-image-profile.json').read_text()), profile)

    def test_fail_when_upstream_call_site_changes(self):
        path = self.root / 'app/cabinet/dependencies.py'
        path.write_text(path.read_text().replace('max_age_seconds=86400 * 30', 'max_age_seconds=42'))
        with self.assertRaises(ValueError):
            prepare(self.root, 3650)
        self.assertFalse((self.root / 'karvpn-image-profile.json').exists())

    def test_invalid_auth_window_is_rejected(self):
        for days in (0, -1, 36501):
            with self.assertRaises(ValueError):
                prepare(self.root, days)


if __name__ == '__main__':
    unittest.main()
