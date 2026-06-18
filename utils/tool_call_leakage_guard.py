"""Helpers for preventing tool-call control markup from reaching chat output."""

from __future__ import annotations

import json
import re
from typing import NamedTuple


class ToolCallSanitizationResult(NamedTuple):
    had_markup: bool
    sanitized_text: str
    should_block: bool


_TOOL_CALL_TAG_RE = re.compile(
    r"<\s*/?\s*(?:tool_call|tool_calls|function|parameters?|arguments?|argument|tool)\b[^>]*>",
    re.IGNORECASE,
)
_UNCLOSED_TOOL_CALL_TAG_RE = re.compile(
    r"<\s*/?\s*(?:tool_call|tool_calls|function|parameters?|arguments?|argument|tool)\b[^\r\n<]*",
    re.IGNORECASE,
)
_TOOL_CODE_FENCE_RE = re.compile(
    r"```(?:\s*(?:tool_call|json|function))?", re.IGNORECASE
)
_FUNCTION_NAME_ONLY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]*")


def contains_tool_call_markup(text: str) -> bool:
    return bool(_TOOL_CALL_TAG_RE.search(text or ""))


def strip_tool_call_markup(text: str) -> str:
    cleaned = text or ""
    cleaned = _TOOL_CODE_FENCE_RE.sub(" ", cleaned)
    cleaned = _TOOL_CALL_TAG_RE.sub(" ", cleaned)
    cleaned = _UNCLOSED_TOOL_CALL_TAG_RE.sub(" ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    cleaned = cleaned.strip(" \t\r\n`'\"<>")
    return cleaned.strip()


def _is_json_payload_like(text: str) -> bool:
    candidate = (text or "").strip()
    if not candidate:
        return False
    try:
        parsed = json.loads(candidate)
    except Exception:
        parsed = None
    if isinstance(parsed, (dict, list)):
        return True
    return candidate[0] in "{[" and ":" in candidate


def _is_function_name_only(text: str) -> bool:
    candidate = (text or "").strip()
    if not _FUNCTION_NAME_ONLY_RE.fullmatch(candidate):
        return False
    return "_" in candidate or "." in candidate


def _is_tool_payload_only(text: str) -> bool:
    candidate = (text or "").strip()
    if not candidate:
        return True
    if _is_json_payload_like(candidate):
        return True
    return _is_function_name_only(candidate)


def sanitize_tool_call_markup(text: str) -> ToolCallSanitizationResult:
    original = (text or "").strip()
    if not contains_tool_call_markup(original):
        return ToolCallSanitizationResult(False, original, False)

    sanitized = strip_tool_call_markup(original)
    should_block = _is_tool_payload_only(sanitized)
    return ToolCallSanitizationResult(
        True,
        "" if should_block else sanitized,
        should_block,
    )
