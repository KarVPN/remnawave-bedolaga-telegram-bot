"""Cabinet refresh-token lifetime: the app must not be logged out every week.

ADR 0005 (karvpn-apps) decides that the app reuses the cabinet login, so the
cabinet refresh token is the outer session of the app. Its shipped default used
to be 7 days, which is exactly the "logged out once a week" complaint from the
closed-beta ticket; the target written down in the ADR is 90 days.
"""

from app.config import Settings, settings


def test_shipped_default_is_long_enough_for_the_app():
    default = Settings.model_fields['CABINET_REFRESH_TOKEN_EXPIRE_DAYS'].default

    assert default == 90
    # The complaint was about a weekly logout: the default must stay above a week.
    assert default > 7


def test_env_value_wins_over_the_default(monkeypatch):
    monkeypatch.setattr(settings, 'CABINET_REFRESH_TOKEN_EXPIRE_DAYS', 30, raising=False)

    assert settings.get_cabinet_refresh_token_expire_days() == 30


def test_expired_or_zero_config_never_issues_a_dead_token(monkeypatch):
    monkeypatch.setattr(settings, 'CABINET_REFRESH_TOKEN_EXPIRE_DAYS', 0, raising=False)

    assert settings.get_cabinet_refresh_token_expire_days() == 1


def test_access_token_stays_short(monkeypatch):
    # Only the refresh token was extended: a long-lived access token would be a
    # different, unacceptable trade-off.
    monkeypatch.setattr(
        settings,
        'CABINET_ACCESS_TOKEN_EXPIRE_MINUTES',
        Settings.model_fields['CABINET_ACCESS_TOKEN_EXPIRE_MINUTES'].default,
        raising=False,
    )

    assert settings.get_cabinet_access_token_expire_minutes() == 15
