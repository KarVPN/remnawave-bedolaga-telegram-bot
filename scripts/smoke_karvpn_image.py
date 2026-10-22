"""Exercise the image without polling Telegram or connecting to production data."""

import ast
import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.webapi.app import create_web_api_app


root = Path('/app')
profile = json.loads((root / 'karvpn-image-profile.json').read_text())
if profile['schema'] != 1 or profile['init_data_call_sites'] < 4:
    raise SystemExit('Invalid KarVPN image profile')
for path in (root / 'app').rglob('*.py'):
    ast.parse(path.read_text())
if any(path.name.startswith('.env') for path in root.rglob('*')):
    raise SystemExit('Unexpected .env file in image')
# TestClient without its context manager does not enter the application's
# lifespan: no DB bootstrap, schedulers, Telegram polling or production traffic.
client = TestClient(create_web_api_app())
response = client.get('/health')
if response.status_code != 200 or response.json()['status'] != 'ok':
    raise SystemExit('Public health endpoint failed')
print('PASS: Python sources, KarVPN deployment profile and public /health')
