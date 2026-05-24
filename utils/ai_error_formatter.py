"""AI provider error formatting helpers."""

from __future__ import annotations

import re
from typing import Optional


_MAX_ERROR_LENGTH = 300

_HTTP_STATUS_MAP = {
    400: "请求参数错误（Bad Request）",
    401: "认证失败（Unauthorized），请检查 API Key",
    403: "访问被拒绝（Forbidden）",
    404: "API 接口不存在（Not Found）",
    429: "请求频率超限（Rate Limit）",
    500: "AI 服务商内部服务器错误",
    502: "AI 服务商网关错误（Bad Gateway），服务可能暂时不可用",
    503: "AI 服务商服务不可用（Service Unavailable）",
    504: "AI 服务商网关超时（Gateway Timeout）",
}

_HTML_ERROR_KEYWORDS = (
    "<!doctype html>",
    "<html",
    "<title>",
    "cloudflare",
    "bad gateway",
    "service unavailable",
    "error code",
    "ray id",
)

_UPSTREAM_EMPTY_OUTPUT_KEYWORDS = (
    "upstream_empty_output",
    "upstream model returned empty output",
    "model returned no usable output",
    "no usable output",
    "empty output",
    "empty assistant message",
)


def format_ai_error(exception: Exception, context_label: str = "AI调用") -> str:
    """Convert provider exceptions into concise log-safe messages."""
    raw_msg = str(exception).strip() or type(exception).__name__

    if _is_html_response(raw_msg):
        status = _extract_http_status(raw_msg)
        return _build_html_error_message(context_label, status)

    if _is_upstream_empty_output(raw_msg):
        return _build_upstream_empty_output_message(context_label, raw_msg)

    status_code = _extract_http_status(raw_msg)
    if status_code and status_code in _HTTP_STATUS_MAP:
        return _build_http_error_message(context_label, status_code, raw_msg)

    network_hint = _detect_network_error(raw_msg)
    if network_hint:
        return _build_network_error_message(context_label, network_hint, raw_msg)

    return _build_generic_error_message(context_label, raw_msg)


def _is_html_response(text: str) -> bool:
    lower = text.lower()
    return any(keyword in lower for keyword in _HTML_ERROR_KEYWORDS) and len(text) > 200


def _is_upstream_empty_output(text: str) -> bool:
    lower = text.lower()
    return any(keyword in lower for keyword in _UPSTREAM_EMPTY_OUTPUT_KEYWORDS)


def _extract_http_status(text: str) -> Optional[int]:
    patterns = (
        r"(\d{3})\s*:\s*[A-Za-z\s]+$",
        r"[Ee]rror\s+[Cc]ode\s*:?\s*(\d{3})",
        r"HTTP[/\s]*(\d{3})",
        r"status[\s]*:?[\s]*(\d{3})",
        r"(\d{3})\s+(Bad Gateway|Service Unavailable|Gateway Timeout|Internal Server Error)",
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            code = int(match.group(1))
            if 400 <= code <= 599:
                return code
    return None


def _detect_network_error(text: str) -> Optional[str]:
    lower = text.lower()
    hints = {
        "connection": "网络连接失败",
        "timeout": "请求超时",
        "ssl": "SSL/TLS 证书问题",
        "certificate": "证书验证失败",
        "dns": "DNS 解析失败",
        "refused": "连接被拒绝",
        "reset": "连接被重置",
        "broken pipe": "连接中断",
        "socket": "Socket 连接异常",
    }
    for keyword, hint in hints.items():
        if keyword in lower:
            return hint
    return None


def _truncate(message: str, max_len: int = _MAX_ERROR_LENGTH) -> str:
    if len(message) <= max_len:
        return message
    return message[:max_len] + f"... (已截断，原始长度 {len(message)} 字符)"


def _build_html_error_message(label: str, status: Optional[int]) -> str:
    code_text = f" HTTP {status}" if status else ""
    detail = (
        _HTTP_STATUS_MAP.get(status, "")
        if status
        else "返回了 HTML 错误页面（可能是网关、代理或 CDN 错误）"
    )
    return (
        f"[{label}] AI 服务商故障{code_text}：{detail}；"
        "返回内容是 HTML 错误页面，已省略原始页面正文"
    )


def _build_upstream_empty_output_message(label: str, raw: str) -> str:
    return (
        f"[{label}] 上游模型返回空输出：模型或中转接口这次没有返回可用内容；"
        f"原始信息: {_truncate(raw)}"
    )


def _build_http_error_message(label: str, code: int, raw: str) -> str:
    detail = _HTTP_STATUS_MAP.get(code, "未知 HTTP 错误")
    fault_type = "AI 服务商故障" if code >= 500 else "请求参数或配置问题"
    extra = _truncate(raw) if raw and raw != detail else ""
    extra_text = f"；原始信息: {extra}" if extra else ""
    return f"[{label}] {fault_type}（HTTP {code}）：{detail}{extra_text}"


def _build_network_error_message(label: str, hint: str, raw: str) -> str:
    return f"[{label}] 网络问题（{hint}）：{_truncate(raw)}"


def _build_generic_error_message(label: str, raw: str) -> str:
    return f"[{label}] 发生错误: {_truncate(raw)}"
