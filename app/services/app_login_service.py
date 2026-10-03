"""App login deep link: the bot confirms to the KarVPN BFF what the app started.

Flow: the app asks the BFF to start a login and shows
`t.me/<bot>?start=login_<nonce>`. Tapping it opens the bot with that payload;
the bot tells the BFF which Telegram user confirmed the nonce, and the app's
poll picks the session up. The shared secret in KARVPN_BOT_SECRET is what
proves the call comes from us, so it never travels in the payload.

Answers stay honest: `ok` only when the BFF accepted the confirmation,
`expired` when the nonce is gone, `unavailable` when the BFF could not be
reached or answered unexpectedly, `disabled` when this deployment has no BFF
configured at all.

The confirmation also carries the way back (`KARVPN_APP_LOGIN_RETURN_URL`): the
person came from the app and the chat has nothing else to offer them (#78).
"""

from enum import StrEnum
from urllib.parse import urlsplit

import httpx
import structlog

from app.config import settings


logger = structlog.get_logger(__name__)

APP_LOGIN_PREFIX = 'login_'
# The BFF refuses anything shorter, so neither do we: it cannot be a real nonce.
APP_LOGIN_MIN_NONCE = 16
APP_LOGIN_TIMEOUT = 10.0

# What Telegram accepts inside an inline button (#163). It is stricter than a
# URL in general: any other scheme makes the server reject the message together
# with its keyboard, and the person gets nothing —
#
#   Bad Request: inline keyboard button URL 'karvpn://login' is invalid:
#   Unsupported URL protocol
#
# — which is why the app's own scheme (`karvpn://login`, registered by the app's
# intent-filter in `app/android/app/src/main/AndroidManifest.xml`) is not used
# here any more. The bot hands over the http(s) address of a page that opens the
# app itself and stays ignorant of the scheme: the contract about `karvpn://`
# now lives entirely in the app and on that page.
APP_LOGIN_RETURN_URL_SCHEMES = ('http', 'https')


class AppLoginResult(StrEnum):
    OK = 'ok'
    EXPIRED = 'expired'
    UNAVAILABLE = 'unavailable'
    DISABLED = 'disabled'


def is_app_login_payload(start_parameter: str | None) -> bool:
    """Whether this deep-link payload is an app login we must handle ourselves."""
    if not start_parameter or not start_parameter.startswith(APP_LOGIN_PREFIX):
        return False
    return len(start_parameter.removeprefix(APP_LOGIN_PREFIX)) >= APP_LOGIN_MIN_NONCE


def app_login_nonce(start_parameter: str) -> str:
    return start_parameter.removeprefix(APP_LOGIN_PREFIX)


def is_app_login_configured() -> bool:
    return bool(settings.KARVPN_BFF_URL.strip() and settings.KARVPN_BOT_SECRET.strip())


def is_supported_return_url(url: str) -> bool:
    """Whether Telegram will accept this address in an inline button (#163)."""
    return urlsplit(url).scheme.lower() in APP_LOGIN_RETURN_URL_SCHEMES


def app_login_return_url() -> str | None:
    """The address of the return button, or `None` when the button must be skipped.

    `None` covers both an unconfigured deployment — the shipped default is empty,
    because the domain appears later than the code — and a value that Telegram
    would refuse. In both cases the answer is sent without the keyboard: the
    confirmation itself is what the tap is for, and a rejected URL would take the
    whole message down with it (that is the #163 failure).
    """
    url = settings.KARVPN_APP_LOGIN_RETURN_URL.strip()
    if not url:
        return None
    if not is_supported_return_url(url):
        logger.warning(
            'app_login: KARVPN_APP_LOGIN_RETURN_URL is not an http(s) address — the return button is skipped',
            url=url,
        )
        return None
    return url


async def confirm_app_login(telegram_id: int, nonce: str) -> AppLoginResult:
    """Tell the BFF that this Telegram user confirms the app's login nonce."""
    if not is_app_login_configured():
        logger.warning('app_login: KARVPN_BFF_URL/KARVPN_BOT_SECRET are not configured')
        return AppLoginResult.DISABLED

    url = f'{settings.KARVPN_BFF_URL.rstrip("/")}/bot/login/confirm'
    try:
        async with httpx.AsyncClient(timeout=APP_LOGIN_TIMEOUT) as client:
            response = await client.post(
                url,
                json={'telegram_id': telegram_id, 'nonce': nonce},
                headers={'Authorization': f'Bearer {settings.KARVPN_BOT_SECRET}'},
            )
    except httpx.HTTPError as error:
        logger.warning('app_login: the BFF is unreachable', error=str(error))
        return AppLoginResult.UNAVAILABLE

    if 200 <= response.status_code < 300:
        return AppLoginResult.OK
    # 404 and 410 both mean the nonce is not (or no longer) usable.
    if response.status_code in (404, 410):
        return AppLoginResult.EXPIRED
    if response.status_code == 401:
        logger.error('app_login: the BFF rejected our secret — check KARVPN_BOT_SECRET')
        return AppLoginResult.UNAVAILABLE
    logger.warning('app_login: unexpected answer from the BFF', status=response.status_code)
    return AppLoginResult.UNAVAILABLE
