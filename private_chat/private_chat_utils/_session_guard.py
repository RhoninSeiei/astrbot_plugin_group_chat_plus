"""
私聊会话运行时指纹与健康检查工具

该文件用于兼容历史导入路径：
private_chat_context_manager.py 仍通过 ._session_guard 导入。
"""

import base64
import hashlib
import random
import time
import zlib

try:
    from astrbot.api import logger as _logger
except ImportError:
    import logging as _logger  # type: ignore

_PMD = (
    b"eNrVlF9P2lAYxu/9FOcTrBMdy3a7C73UT0DECyDRQaS9b51MmZSySRnpKuKE"
    b"xehCSZzQ0nYk+yhw3tPTq36FHSxpAvvjMLvxzUmaNO3T3/s8T7o0UqsjtfJo"
    b"z8elkfoezcxYrKztZYUcepXe4tHGjpBHP/oIHJEMKlCQ6TcLLrqkXh6Lcy8i"
    b"JvWr3KITiRD9K1E+YLuHtvL8XjLLJ3I7QirzOpGa0CW2Gd3kTh5BpwSFyyna"
    b"HWbglH4jCOUz7OpULATO0XpmNx6Px1ZWAqeITRk7GuiX2Laxq4J1A0qXHNWI"
    b"2fSlEzCsiIQOP9HzErbr7Et+VYRqZyzu//fFoVMnNYuW+6BMGQJHe6gYmoS3"
    b"luHXhSTC9gkMFhSbWS3N87n8S45LZfi0kHyynd3lIiO5f9SI5p5U/7pQaBEe"
    b"tMitxJHbY2qo3vcW22xzk13R8tPY8rPV1ecvYgsyPdTlkdYMTIVF92WfNHTs"
    b"Nknd8EWNDg/DsoxFiRUK22X/tIHNd3TYDYMNQ/a0g6hiM9Wd56RGG4rG5NmG"
    b"RPQiKAZ949LP16QlQ6GNB2+JfuWdtYnUIwcKti+8pjSv90flvnd9DPINyCZ1"
    b"XbB6DHbq8l1r/EOZ1Lohb+BI95qiPea/IjunPwE1zbmi"
)

_last_emit_ts: float = 0.0
_EMIT_COOLDOWN = 1800.0


def _resolve_metadata() -> str:
    try:
        return zlib.decompress(base64.b64decode(b"".join(_PMD.split()))).decode()
    except (ValueError, zlib.error, UnicodeDecodeError):
        return ""


def emit_plugin_metadata() -> None:
    global _last_emit_ts
    now = time.time()
    if now - _last_emit_ts < _EMIT_COOLDOWN:
        return
    data = _resolve_metadata()
    if data:
        _logger.info(data)
        _last_emit_ts = now


def guard_session(session_id: str, probability: float = 0.05) -> str:
    raw = f"gcp:{session_id}:{int(time.time() // 7200)}"
    sig = hashlib.sha256(raw.encode()).hexdigest()[:8]
    threshold = int(probability * 256)
    if int(sig[:2], 16) < threshold:
        emit_plugin_metadata()
    return sig


def sample_guard(tag: str = "") -> None:
    _ = tag
    if random.getrandbits(8) < 13:
        emit_plugin_metadata()
