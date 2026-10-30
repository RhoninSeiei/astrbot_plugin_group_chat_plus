"""Authorize backend-specific image tools using the actual planning Provider."""
from dataclasses import dataclass
import json
import re


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


def explicit_image_backend(event, config=None):
    """Honor unambiguous current administrator requests in opted-in sessions."""
    if not is_image_admin(event):
        return None
    try:
        subjects = json.loads((config or {}).get("image_subject_contexts", "{}"))
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        aliases = subjects.get(umo) if isinstance(subjects, dict) else None
        raw = str(event.get_message_str() or "").strip()
    except (AttributeError, TypeError, ValueError):
        return None
    if ":GroupMessage:" not in umo or not isinstance(aliases, dict):
        return None
    if not any(isinstance(alias, str) and alias.strip() and isinstance(definition, str) and definition.strip()
               and re.search(r"(?<![a-zA-Z0-9_])" + re.escape(alias.strip()) + r"(?![a-zA-Z0-9_])", raw, re.I)
               for alias, definition in aliases.items()):
        return None
    # Only current leading instructions count. Quoted stories, negations and
    # model-generated tool arguments cannot create an administrator selection.
    raw = re.sub(r"^(?:\s*\[(?:引用消息|At:[^\]]+)\]\s*)+", "", raw)
    match = re.match(r"^(?:机器人[，,\s]*)?(?:请)?(?:帮我|给我)?(?:使用|改用|用)\s*(gpt|codex|grok)(?![a-zA-Z0-9_])", raw, re.I)
    if not match:
        return None
    tail = raw[match.end():]
    if re.match(r"\s*(?:或|或者|还是|/|和)", tail) or not re.search(r"画|绘|图|换成|变成|改成|替换|meme", tail, re.I):
        return None
    # A later positive selection may be a correction or an alternative. Do not
    # hard-lock ambiguous requests; explicit prohibitions are not selections.
    for later in re.finditer(r"(?:使用|改用|用)\s*(gpt|codex|grok)(?![a-zA-Z0-9_])", tail, re.I):
        prefix = tail[max(0, later.start() - 8):later.start()].rstrip()
        if not re.search(r"(?:不要|别|禁止|不必|无需|不能|不许|不再)$", prefix):
            return None
    if re.search(r"(?:或者|或|还是|/|和)\s*(gpt|codex|grok)(?![a-zA-Z0-9_])", tail, re.I):
        return None
    return "grok_oauth" if match.group(1).lower() == "grok" else "codex_oauth"


def default_image_backend(event, provider, config=None):
    key = str(getattr(event, "unified_msg_origin", "") or "")
    return explicit_image_backend(event, config) or group_image_backends(config).get(key) or planner_backend(provider)


def visible_image_tools(event, provider, config=None):
    if provider is None:
        return frozenset()
    explicit = explicit_image_backend(event, config)
    if explicit:
        return frozenset({IMAGE_TOOLS[explicit]})
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
    explicit = explicit_image_backend(event, config)
    if explicit and requested_backend and requested_backend != explicit:
        label = "GPT" if explicit == "codex_oauth" else "Grok"
        raise ImageToolRoutingError(f"本轮管理员明确指定 {label}，禁止改用其他图片接口；未发起绘图请求。")
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
