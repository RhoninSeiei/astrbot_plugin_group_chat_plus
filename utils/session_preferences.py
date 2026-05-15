from typing import Any


EMPTY_PERSONA = {
    "name": "[%None]",
    "prompt": "",
    "begin_dialogs": [],
    "_begin_dialogs_processed": [],
    "tools": None,
    "skills": None,
    "custom_error_message": None,
}


def _event_umo(event: Any | None) -> str | None:
    if event is None:
        return None
    umo = getattr(event, "unified_msg_origin", None)
    return str(umo) if umo else None


def _event_platform_name(event: Any | None) -> str:
    if event is None:
        return ""
    getter = getattr(event, "get_platform_name", None)
    if callable(getter):
        try:
            return str(getter() or "")
        except Exception:
            return ""
    return ""


def _provider_settings_for(context: Any, umo: str | None) -> dict:
    getter = getattr(context, "get_config", None)
    if not callable(getter):
        return {}
    try:
        config = getter(umo)
    except TypeError:
        config = getter()
    except Exception:
        return {}
    if not config:
        return {}
    try:
        provider_settings = config.get("provider_settings", {})
    except Exception:
        return {}
    return provider_settings if isinstance(provider_settings, dict) else {}


async def resolve_session_persona(
    context: Any,
    *,
    event: Any | None = None,
    umo: str | None = None,
    platform_name: str | None = None,
    conversation_persona_id: str | None = None,
) -> dict:
    actual_umo = umo or _event_umo(event)
    actual_platform_name = (
        platform_name if platform_name is not None else _event_platform_name(event)
    )
    persona_manager = getattr(context, "persona_manager", None)
    if persona_manager is None:
        return dict(EMPTY_PERSONA)

    resolver = getattr(persona_manager, "resolve_selected_persona", None)
    if callable(resolver) and actual_umo:
        try:
            _persona_id, persona, _forced_id, _use_webchat_default = await resolver(
                umo=actual_umo,
                conversation_persona_id=conversation_persona_id,
                platform_name=actual_platform_name or "",
                provider_settings=_provider_settings_for(context, actual_umo),
            )
            if persona:
                return persona
            if _persona_id == "[%None]":
                return dict(EMPTY_PERSONA)
        except Exception:
            pass

    default_getter = getattr(persona_manager, "get_default_persona_v3", None)
    if callable(default_getter):
        try:
            return await default_getter(actual_umo)
        except Exception:
            return dict(EMPTY_PERSONA)

    return dict(EMPTY_PERSONA)


def get_session_provider(
    context: Any,
    *,
    event: Any | None = None,
    umo: str | None = None,
):
    actual_umo = umo or _event_umo(event)
    getter = getattr(context, "get_using_provider", None)
    if not callable(getter):
        return None

    if actual_umo:
        try:
            return getter(umo=actual_umo)
        except TypeError:
            try:
                return getter(actual_umo)
            except TypeError:
                pass

    return getter()
