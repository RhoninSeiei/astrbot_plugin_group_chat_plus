"""Authorize backend-specific image tools using the actual planning Provider."""
from dataclasses import dataclass


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


def visible_image_tools(event, provider):
    if provider is None:
        return frozenset()
    if is_image_admin(event):
        return PUBLIC_IMAGE_TOOLS
    tool = IMAGE_TOOLS.get(planner_backend(provider))
    return frozenset({tool}) if tool else frozenset()


@dataclass(frozen=True)
class ImageToolRoute:
    backend: str
    provider: object


def resolve_image_tool_route(event, caller, requested_backend, context, config):
    if caller is None:
        raise ImageToolRoutingError("无法确定本次图片规划模型。")
    actual = planner_backend(caller)
    requested = requested_backend or actual
    if requested not in IMAGE_TOOLS:
        raise ImageToolRoutingError("当前规划模型没有对应的图片工具。")
    if requested == actual:
        return ImageToolRoute(requested, caller)
    # Check before any lookup. Neither model arguments nor chat text can grant
    # permission to cross from the actual planner to another image backend.
    if not is_image_admin(event):
        raise ImageToolRoutingError("普通成员只能使用当前规划模型对应的图片工具。")
    key = "grok_image_provider_id" if requested == "grok_oauth" else "codex_oauth_image_provider_id"
    default = "grok_oauth/grok-4.6" if requested == "grok_oauth" else "openai_oauth/gpt-5.6-sol"
    try:
        provider = context.get_provider_by_id(str(config.get(key) or default).strip())
    except Exception:
        raise ImageToolRoutingError("指定图片接口的提供商不可用。") from None
    if planner_backend(provider) != requested:
        raise ImageToolRoutingError("指定图片接口的提供商类型不匹配。")
    return ImageToolRoute(requested, provider)
