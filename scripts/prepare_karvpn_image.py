"""Apply the existing KarVPN deployment profile while building the image."""

import argparse
import ast
import json
import re
from pathlib import Path


def prepare(root: Path, days: int) -> None:
    if not 1 <= days <= 36500:
        raise ValueError('TELEGRAM_AUTH_MAX_AGE_DAYS must be between 1 and 36500')
    calls = 0
    for name in (
        'app/cabinet/dependencies.py',
        'app/cabinet/routes/account_linking.py',
        'app/cabinet/routes/auth.py',
        'app/cabinet/routes/support_ws.py',
    ):
        path = root / name
        source = path.read_text()
        source, count = re.subn(r'max_age_seconds=86400 \* \d+', f'max_age_seconds=86400 * {days}', source)
        if not count:
            raise ValueError(f'Telegram initData profile no longer matches {name}')
        ast.parse(source)
        path.write_text(source)
        calls += count
    path = root / 'app/utils/telegram_webapp.py'
    source, count = re.subn(
        r'max_age_seconds: int = 86400(?: \* \d+)?,',
        f'max_age_seconds: int = 86400 * {days},',
        path.read_text(),
    )
    if count != 1:
        raise ValueError('Mini App parser profile no longer matches the source')
    ast.parse(source)
    path.write_text(source)
    profile = {'schema': 1, 'telegram_auth_max_age_days': days, 'init_data_call_sites': calls}
    (root / 'karvpn-image-profile.json').write_text(json.dumps(profile) + '\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path('/app'))
    parser.add_argument('--days', type=int, required=True)
    args = parser.parse_args()
    prepare(args.root, args.days)
