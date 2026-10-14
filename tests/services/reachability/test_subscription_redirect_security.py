"""Validate redirect destinations before issuing a request, including literal IPs."""

from types import SimpleNamespace

import pytest
from yarl import URL

from app.services.reachability.subscriptions import MAX_REDIRECTS, SubscriptionFetchError, _get


class RedirectSession:
    def __init__(self, locations):
        self.locations = iter(locations)
        self.requests = []

    def get(self, url, **kwargs):
        self.requests.append((url, kwargs))
        location = next(self.locations)

        class Content:
            async def read(self, _size):
                return b''

        response = SimpleNamespace(
            url=URL(url),
            status=302 if location else 200,
            headers={'Location': location} if location else {},
            content=Content(),
        )

        class Context:
            async def __aenter__(self):
                return response

            async def __aexit__(self, *_args):
                pass

        return Context()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'location',
    [
        'http://127.0.0.1/admin',
        'http://169.254.169.254/latest/meta-data',
        'http://10.0.0.1/',
        'http://[::1]/',
        'https://user:pass@sub.example/',
        'file:///etc/passwd',
    ],
)
async def test_private_or_invalid_redirect_is_rejected_before_second_request(location):
    session = RedirectSession([location])
    with pytest.raises(SubscriptionFetchError):
        await _get(session, 'https://sub.example/start', {})
    assert len(session.requests) == 1
    assert session.requests[0][1]['allow_redirects'] is False


@pytest.mark.asyncio
async def test_public_relative_redirect_is_followed():
    session = RedirectSession(['/next', None])
    assert await _get(session, 'https://sub.example/start', {}) == (b'', {})
    assert [url for url, _kwargs in session.requests] == ['https://sub.example/start', 'https://sub.example/next']


@pytest.mark.asyncio
async def test_redirect_loop_stops_at_limit():
    session = RedirectSession(['/loop'] * (MAX_REDIRECTS + 1))
    with pytest.raises(SubscriptionFetchError, match='много перенаправлений'):
        await _get(session, 'https://sub.example/start', {})
    assert len(session.requests) == MAX_REDIRECTS + 1
