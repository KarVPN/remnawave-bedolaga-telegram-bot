"""App login: the bot confirms to the KarVPN BFF what the app started.

The app asks the BFF to start a login; the bot tells the BFF which Telegram user
confirms it, and the app's poll picks the session up. The shared secret in
KARVPN_BOT_SECRET is what proves the call comes from us, so it never travels in
the payload.

There are two ways to confirm, and both end in the same call: the deep link the
app shows (`t.me/<bot>?start=login_<nonce>`, tapped in Telegram) and the
six-digit code the app shows, sent to the bot as a plain message (#178). The
code exists because the phone that runs the app is exactly the device whose
Telegram may not open — from a desktop the person can type the code into the
chat instead. `/bot/login/confirm` accepts `nonce` or `code`; the code is only
another way for the BFF to find the pending login, never another kind of trust,
so no new authorization appears here.

The code is read strictly (`app_login_code`): six digits, nothing around them
but whitespace. Digits split by a space or a hyphen stay out, because
`123-456` is a legal promo code shape and the registration flow must keep it.

Answers stay honest: `ok` only when the BFF accepted the confirmation,
`expired` when the login is gone (for the code path that covers "no such code"
as well — the BFF hides an expired pending login behind the same 404),
`unavailable` when the BFF could not be reached or answered unexpectedly,
`disabled` when this deployment has no BFF configured at all.

The confirmation also carries the way back (`KARVPN_APP_LOGIN_RETURN_URL`): the
person came from the app and the chat has nothing else to offer them (#78). The
cabinet sign-in the app opens (#163) ends in the same chat and answers with the
button built from that same address, so the setting is read here for all of them.
"""

import re
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

# The six digits the app shows for a login confirmed from another device (#178).
# It is the same shape the BFF checks (`^[0-9]{6}$` in `/bot/login/confirm`), so
# a code we let through is never rejected here on the format alone.
APP_LOGIN_CODE_LENGTH = 6
APP_LOGIN_CODE_PATTERN = re.compile(rf'\d{{{APP_LOGIN_CODE_LENGTH}}}')


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


def app_login_code(text: str | None) -> str | None:
    """The login code this chat message carries, or `None` when it carries none (#178).

    The code itself, and nothing but the code: six digits with whitespace around
    them allowed, because a copy from the app onto a desktop arrives with a
    trailing newline or a non-breaking space often enough. Everything else is
    refused, and each refusal is a decision:

    * digits split by a space or a dash (`123 456`, `123-456`) — the hyphen form
      is a *legal promo code shape* (`validate_promo_format`), and reading it as
      a login would take that input away from registration (see the priority
      note on the handler registration in `app/handlers/start.py`);
    * a code with words around it (`код 123456`) — a message like that is not
      necessarily a confirmation, and confirming an app login must stay a
      deliberate act: the BFF records whoever sends the code as the device's
      owner.

    Digits never appear as a referral code (`ref` + 8 characters), so the only
    real overlap is with a hand-made promo code, which is exactly the case the
    strictness above keeps in the registration flow.
    """
    if not text:
        return None
    candidate = text.strip()
    return candidate if APP_LOGIN_CODE_PATTERN.fullmatch(candidate) else None


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
    return await _confirm_app_login(telegram_id, {'nonce': nonce})


async def confirm_app_login_by_code(telegram_id: int, code: str) -> AppLoginResult:
    """Tell the BFF that this Telegram user confirms the login behind this code (#178).

    The same call and the same answers as the deep link: `/bot/login/confirm`
    takes `nonce` or `code`, and a code is only the way the BFF finds the pending
    login when the person could not tap the link. No new kind of trust appears —
    the BFF still rate-limits every attempt per Telegram user.
    """
    return await _confirm_app_login(telegram_id, {'code': code})


async def _confirm_app_login(telegram_id: int, confirmation: dict[str, str]) -> AppLoginResult:
    """Post one confirmation: `nonce` and `code` differ in the field, not in the answer."""
    if not is_app_login_configured():
        logger.warning('app_login: KARVPN_BFF_URL/KARVPN_BOT_SECRET are not configured')
        return AppLoginResult.DISABLED

    url = f'{settings.KARVPN_BFF_URL.rstrip("/")}/bot/login/confirm'
    try:
        async with httpx.AsyncClient(timeout=APP_LOGIN_TIMEOUT) as client:
            response = await client.post(
                url,
                json={'telegram_id': telegram_id, **confirmation},
                headers={'Authorization': f'Bearer {settings.KARVPN_BOT_SECRET}'},
            )
    except httpx.HTTPError as error:
        logger.warning('app_login: the BFF is unreachable', error=str(error))
        return AppLoginResult.UNAVAILABLE

    if 200 <= response.status_code < 300:
        return AppLoginResult.OK
    # 404 and 410 both mean the login is not (or no longer) usable. For a code
    # 404 is the whole story: the BFF looks only at pending, unexpired logins,
    # so a code that ran out of its ten minutes is "not found" just like a code
    # that never existed — and the answer must stay true to both.
    if response.status_code in (404, 410):
        return AppLoginResult.EXPIRED
    if response.status_code == 401:
        logger.error('app_login: the BFF rejected our secret — check KARVPN_BOT_SECRET')
        return AppLoginResult.UNAVAILABLE
    logger.warning('app_login: unexpected answer from the BFF', status=response.status_code)
    return AppLoginResult.UNAVAILABLE
