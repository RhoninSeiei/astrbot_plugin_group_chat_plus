"""Group LLM request and response safety helpers."""

from __future__ import annotations

from pathlib import Path
import re
from typing import NamedTuple
from urllib.parse import unquote, urlparse


class LLMRequestImageSanitizationResult(NamedTuple):
    contexts: list
    image_urls: list[str]
    removed_context_parts: int
    removed_image_urls: int
    removed_empty_messages: int


DEFAULT_PERSONA_FAILURE_REPLY = "模型服务暂时没能完成回复，稍后再试。"

_RAW_FAILURE_PREFIX_RE = re.compile(
    r"^\s*(?:LLM\s*响应错误\s*[:：]|All\s+chat\s+models\s+failed\s*:|"
    r"生成回复时发生错误\s*[:：])",
    re.IGNORECASE,
)
_INTERNAL_DETAIL_TERMS = (
    "all chat models failed",
    "llm 响应错误",
    "permissiondeniederror",
    "internalservererror",
    "insufficient_user_quota",
    "request id",
    "request_id",
    "cloudflare",
    "http://",
    "https://",
    "api key",
    "provider",
    "供应商",
    "预扣费",
    "剩余额度",
    "状态码",
    "密钥",
    "token",
    "<tool_call",
    "<function",
)


def _extract_image_reference(part: object) -> tuple[bool, str]:
    if not isinstance(part, dict):
        return False, ""

    part_type = str(part.get("type") or "").strip().lower()
    if part_type not in {"image_url", "input_image"}:
        return False, ""

    image_value = part.get("image_url")
    if isinstance(image_value, dict):
        image_value = image_value.get("url")
    return True, str(image_value or "").strip()


def _is_valid_image_reference(reference: object) -> bool:
    value = str(reference or "").strip()
    if not value:
        return False

    lowered = value.lower()
    if lowered.startswith(("http://", "https://")):
        parsed = urlparse(value)
        return bool(parsed.scheme and parsed.netloc)
    if lowered.startswith("data:image/"):
        return "," in value
    if lowered.startswith("file://"):
        parsed = urlparse(value)
        value = unquote(parsed.path or "")

    try:
        return Path(value).is_file()
    except (OSError, TypeError, ValueError):
        return False


def sanitize_llm_request_images(
    contexts,
    image_urls,
) -> LLMRequestImageSanitizationResult:
    source_contexts = contexts if isinstance(contexts, list) else list(contexts or [])
    sanitized_contexts = None
    removed_context_parts = 0
    removed_empty_messages = 0

    for message_index, original_message in enumerate(source_contexts):
        if not isinstance(original_message, dict):
            if sanitized_contexts is not None:
                sanitized_contexts.append(original_message)
            continue

        content = original_message.get("content")
        if not isinstance(content, list):
            if sanitized_contexts is not None:
                sanitized_contexts.append(original_message)
            continue

        sanitized_parts = None
        contained_image_part = False
        for part_index, part in enumerate(content):
            is_image_part, reference = _extract_image_reference(part)
            if not is_image_part:
                if sanitized_parts is not None:
                    sanitized_parts.append(part)
                continue
            contained_image_part = True
            if _is_valid_image_reference(reference):
                if sanitized_parts is not None:
                    sanitized_parts.append(part)
                continue

            if sanitized_parts is None:
                sanitized_parts = list(content[:part_index])
            removed_context_parts += 1

        if sanitized_parts is None:
            if sanitized_contexts is not None:
                sanitized_contexts.append(original_message)
            continue

        if sanitized_contexts is None:
            sanitized_contexts = list(source_contexts[:message_index])

        if contained_image_part and not sanitized_parts:
            removed_empty_messages += 1
            continue

        message = original_message.copy()
        message["content"] = sanitized_parts
        sanitized_contexts.append(message)

    source_image_urls = (
        image_urls if isinstance(image_urls, list) else list(image_urls or [])
    )
    sanitized_image_urls = None
    removed_image_urls = 0
    for reference_index, reference in enumerate(source_image_urls):
        if _is_valid_image_reference(reference):
            if sanitized_image_urls is not None:
                sanitized_image_urls.append(reference)
            continue

        if sanitized_image_urls is None:
            sanitized_image_urls = list(source_image_urls[:reference_index])
        removed_image_urls += 1

    return LLMRequestImageSanitizationResult(
        sanitized_contexts if sanitized_contexts is not None else source_contexts,
        sanitized_image_urls
        if sanitized_image_urls is not None
        else source_image_urls,
        removed_context_parts,
        removed_image_urls,
        removed_empty_messages,
    )


def classify_raw_llm_failure(text: str) -> str | None:
    value = str(text or "").strip()
    if not _RAW_FAILURE_PREFIX_RE.match(value):
        return None

    lowered = value.lower()
    if "image_url" in lowered and (
        "expected a valid url" in lowered or "invalid format" in lowered
    ):
        return "invalid_history_image"
    if "insufficient_user_quota" in lowered or "预扣费额度失败" in value:
        return "provider_quota"
    if (
        "error 522" in lowered
        or "status': 522" in lowered
        or "connection timed out" in lowered
    ):
        return "provider_timeout"
    if "all chat models failed" in lowered:
        return "all_models_failed"
    return "llm_response_failed"


def build_persona_failure_prompt(reason_code: str) -> str:
    event_description = {
        "invalid_history_image": "本次请求引用的历史图片已经失效，回复未能完成。",
        "provider_quota": "当前模型服务暂时无法完成本次回复。",
        "provider_timeout": "当前模型服务响应超时，本次回复未能完成。",
        "all_models_failed": "当前可用模型均未能完成本次回复。",
        "llm_response_failed": "本次模型回复生成失败。",
    }.get(reason_code, "本次模型回复生成失败。")
    return (
        "请依据系统人格，用一句自然、简短的中文告知群友以下情况："
        f"{event_description}"
        "只输出准备发送到群聊的句子。"
        "避免提及服务商、模型名称、状态码、额度、请求编号、网址、密钥、"
        "内部系统、调用过程或错误详情。不要输出工具调用格式。"
    )


def sanitize_persona_failure_reply(text: str) -> str:
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    value = re.sub(r"([，。！？；：、])\s+", r"\1", value)
    value = value.strip("`\"'").strip()
    if not value:
        return ""

    lowered = value.lower()
    if classify_raw_llm_failure(value) is not None:
        return ""
    if any(term in lowered for term in _INTERNAL_DETAIL_TERMS):
        return ""
    return value[:160].rstrip()
