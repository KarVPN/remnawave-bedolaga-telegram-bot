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

The confirmation also carries the way back (`APP_LOGIN_RETURN_URL`): the person
came from the app and the chat has nothing else to offer them (#78).
"""

from enum import StrEnum

import httpx
import structlog

from app.config import settings


logger = structlog.get_logger(__name__)

APP_LOGIN_PREFIX = 'login_'
# The BFF refuses anything shorter, so neither do we: it cannot be a real nonce.
APP_LOGIN_MIN_NONCE = 16
APP_LOGIN_TIMEOUT = 10.0

# Where the confirmation message sends a person back (#78): the scheme the app
# registers for itself, so a tap on the button opens the app instead of leaving
# someone in the chat. The host is what the app's intent-filter matches
# (`app/android/app/src/main/AndroidManifest.xml`, `karvpn://login`) — the two
# halves of one contract, one in each repository: change them together.
APP_LOGIN_RETURN_URL = 'karvpn://login'


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
