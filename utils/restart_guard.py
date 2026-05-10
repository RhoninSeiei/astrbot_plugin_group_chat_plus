from __future__ import annotations

from collections.abc import Iterable
from typing import Any


def normalize_user_ids(values: object) -> set[str]:
    if values in (None, ""):
        return set()
    if isinstance(values, str):
        return {values.strip()} if values.strip() else set()
    if isinstance(values, dict):
        iterable: Iterable[Any] = values.keys()
    elif isinstance(values, Iterable):
        iterable = values
    else:
        iterable = [values]
    return {str(item).strip() for item in iterable if str(item).strip()}


def is_restart_command_authorized(
    event: Any,
    *,
    admin_user_ids: object,
    command_allowlist: object = None,
    command_denylist: object = None,
) -> bool:
    sender_id = _event_sender_id(event)
    if not sender_id:
        return False
    if not _event_is_admin(event, sender_id=sender_id, admin_user_ids=admin_user_ids):
        return False

    denied_ids = normalize_user_ids(command_denylist)
    if sender_id in denied_ids:
        return False

    allowed_ids = normalize_user_ids(command_allowlist)
    if allowed_ids and sender_id not in allowed_ids:
        return False
    return True


def _event_sender_id(event: Any) -> str:
    getter = getattr(event, "get_sender_id", None)
    if callable(getter):
        return str(getter() or "").strip()
    return str(getattr(event, "sender_id", "") or "").strip()


def _event_is_admin(event: Any, *, sender_id: str, admin_user_ids: object) -> bool:
    checker = getattr(event, "is_admin", None)
    if callable(checker) and bool(checker()):
        return True
    if str(getattr(event, "role", "") or "").strip().lower() == "admin":
        return True
    return sender_id in normalize_user_ids(admin_user_ids)
