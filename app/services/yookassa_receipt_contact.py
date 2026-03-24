from __future__ import annotations

from typing import TYPE_CHECKING

from app.utils.validators import validate_email, validate_phone

if TYPE_CHECKING:
    from app.database.models import User


def normalize_phone(phone: str) -> str:
    return ''.join(ch for ch in phone.strip() if ch not in ' -()')


def resolve_receipt_contact(
    user: User | None,
    *,
    receipt_email: str | None = None,
    receipt_phone: str | None = None,
) -> tuple[str | None, str | None]:
    candidates_email = [
        receipt_email,
        getattr(user, 'email', None) if user is not None else None,
        getattr(user, 'email_change_new', None) if user is not None else None,
    ]
    candidates_phone = [
        receipt_phone,
        getattr(user, 'phone', None) if user is not None else None,
    ]

    resolved_email: str | None = None
    for candidate in candidates_email:
        if not candidate:
            continue
        normalized = candidate.strip().lower()
        if validate_email(normalized):
            resolved_email = normalized
            break

    resolved_phone: str | None = None
    if not resolved_email:
        for candidate in candidates_phone:
            if not candidate:
                continue
            normalized = normalize_phone(candidate)
            if validate_phone(normalized):
                resolved_phone = normalized
                break

    return resolved_email, resolved_phone
