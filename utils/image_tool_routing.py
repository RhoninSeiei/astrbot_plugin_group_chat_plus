"""Authorize backend-specific image tools using the actual planning Provider."""
from dataclasses import dataclass
import json


IMAGE_TOOLS = {"grok_oauth": "gcp_grok_image", "codex_oauth": "gcp_gpt_image"}
LEGACY_IMAGE_TOOLS = frozenset({"gcp_step_image_generate", "gcp_step_image_edit"})
PUBLIC_IMAGE_TOOLS = frozenset(IMAGE_TOOLS.values())
ALL_IMAGE_TOOLS = PUBLIC_IMAGE_TOOLS | LEGACY_IMAGE_TOOLS


class ImageToolRoutingError(ValueError):
    pass


def is_image_admin(event):
    try:
        checker = getattr(event, "is_admin", None)
        return callable(checker) and checker() is True
    except Exception:
        return False


def planner_backend(provider):
    try:
        return {"grok_oauth_chat_completion": "grok_oauth",
                "openai_oauth_chat_completion": "codex_oauth"}.get(provider.meta().type)
    except Exception:
        return None


def group_image_backends(config):
    try:
        values = json.loads((config or {}).get("image_group_backends", "{}"))
    except (TypeError, ValueError):
        return {}
    return {k: v for k, v in values.items() if isinstance(k, str) and isinstance(v, str) and v in IMAGE_TOOLS} if isinstance(values, dict) else {}


def default_image_backend(event, provider, config=None):
    key = str(getattr(event, "unified_msg_origin", "") or "")
    return group_image_backends(config).get(key) or planner_backend(provider)


def visible_image_tools(event, provider, config=None):
    if provider is None:
        return frozenset()
    if is_image_admin(event):
        return PUBLIC_IMAGE_TOOLS
    tool = IMAGE_TOOLS.get(default_image_backend(event, provider, config))
    return frozenset({tool}) if tool else frozenset()


@dataclass(frozen=True)
class ImageToolRoute:
    backend: str
    provider: object


def resolve_image_tool_route(event, caller, requested_backend, context, config):
    if caller is None:
        raise ImageToolRoutingError("无法确定本次图片规划模型。")
    actual = planner_backend(caller)
    default = default_image_backend(event, caller, config)
    requested = requested_backend or default
    if requested not in IMAGE_TOOLS:
        raise ImageToolRoutingError("当前规划模型没有对应的图片工具。")
    # Enforce the group default before even returning the current provider.
    if requested != default and not is_image_admin(event):
        raise ImageToolRoutingError("普通成员只能使用当前群默认的图片接口。")
    if requested == actual:
        return ImageToolRoute(requested, caller)
    key = "grok_image_provider_id" if requested == "grok_oauth" else "codex_oauth_image_provider_id"
    default = "grok_oauth/grok-4.6" if requested == "grok_oauth" else "openai_oauth/gpt-5.6-sol"
    try:
        provider = context.get_provider_by_id(str(config.get(key) or default).strip())
    except Exception:
        raise ImageToolRoutingError("指定图片接口的提供商不可用。") from None
    if planner_backend(provider) != requested:
        raise ImageToolRoutingError("指定图片接口的提供商类型不匹配。")
    return ImageToolRoute(requested, provider)
