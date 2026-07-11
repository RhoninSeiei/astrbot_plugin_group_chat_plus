from __future__ import annotations

import asyncio
import weakref
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_CODEX_PROVIDER_ID = "openai_oauth/gpt-5.6-sol"
DEFAULT_CODEX_MODEL = "gpt-5.6-sol"
DEFAULT_CODEX_SIZE = "1024x1024"
VALID_CODEX_SIZES = {"1024x1024", "1536x1024", "1024x1536"}
_PROVIDER_TIMEOUT_LOCKS: weakref.WeakKeyDictionary[Any, asyncio.Lock] = (
    weakref.WeakKeyDictionary()
)
_INSTANCE_LOCK_ATTR = "_codex_oauth_image_timeout_lock"


class CodexOAuthImageUserError(Exception):
    pass


class CodexOAuthImageConfigError(Exception):
    pass


class CodexOAuthImageProviderError(Exception):
    pass


@dataclass(frozen=True)
class CodexOAuthImageResult:
    path: str
    mode: str
    backend: str = "codex_oauth"
    media_type: str = "image/png"
    revised_prompt: str = ""


def _provider_timeout_lock(provider: Any) -> asyncio.Lock:
    weak_lock = None
    uses_weak_lock = True
    try:
        weak_lock = _PROVIDER_TIMEOUT_LOCKS.get(provider)
        if weak_lock is None:
            weak_lock = asyncio.Lock()
            _PROVIDER_TIMEOUT_LOCKS[provider] = weak_lock
    except Exception:
        uses_weak_lock = False

    if uses_weak_lock:
        return weak_lock

    attached_lock = None
    attachment_error = None
    try:
        provider_state = object.__getattribute__(provider, "__dict__")
        attached_lock = provider_state.get(_INSTANCE_LOCK_ATTR)
        if attached_lock is None:
            attached_lock = asyncio.Lock()
            provider_state[_INSTANCE_LOCK_ATTR] = attached_lock
        elif not isinstance(attached_lock, asyncio.Lock):
            attachment_error = CodexOAuthImageConfigError(
                "Codex OAuth 图片 Provider 并发锁配置无效。"
            )
    except Exception:
        attachment_error = CodexOAuthImageConfigError(
            "Codex OAuth 图片 Provider 无法保存并发锁。"
        )

    if attachment_error is not None:
        raise attachment_error
    return attached_lock


@asynccontextmanager
async def _temporary_provider_timeout(provider: Any, timeout: float):
    async with _provider_timeout_lock(provider):
        had_timeout = hasattr(provider, "timeout")
        previous_timeout = getattr(provider, "timeout", None)
        setattr(provider, "timeout", timeout)
        try:
            yield
        finally:
            if had_timeout:
                setattr(provider, "timeout", previous_timeout)
            else:
                try:
                    delattr(provider, "timeout")
                except AttributeError:
                    pass


class CodexOAuthImageService:
    MAX_PROMPT_CHARS = 512

    def __init__(self, *, context: Any, config: dict) -> None:
        self.context = context
        self.config = dict(config or {})

    @staticmethod
    def normalize_size(value: Any) -> str:
        normalized = str(value or "").strip().lower().replace("×", "x")
        compact = "".join(normalized.split())
        aliases = {
            "1:1": "1024x1024", "square": "1024x1024", "方图": "1024x1024",
            "16:9": "1536x1024", "landscape": "1536x1024", "横图": "1536x1024",
            "1080p": "1536x1024", "1920x1080": "1536x1024",
            "9:16": "1024x1536", "portrait": "1024x1536", "竖图": "1024x1536",
            "1080x1920": "1024x1536",
        }
        resolved = aliases.get(compact, compact)
        if resolved not in VALID_CODEX_SIZES:
            raise CodexOAuthImageUserError(
                "Codex OAuth 图片尺寸仅支持 1024x1024、1536x1024、1024x1536。"
            )
        return resolved

    async def generate(self, *, prompt: str, size: str = "") -> CodexOAuthImageResult:
        return await self._execute(
            prompt=prompt,
            size=size,
            reference_images=None,
            action="generate",
        )

    async def edit(self, *, prompt: str, image_path: str) -> CodexOAuthImageResult:
        source = None
        source_is_file = False
        source_error = None
        try:
            source = Path(str(image_path))
            source_is_file = source.is_file()
        except Exception:
            source_error = CodexOAuthImageProviderError(
                "Codex OAuth 图片文件检查失败。"
            )
        if source_error is not None:
            raise source_error
        if not source_is_file:
            raise CodexOAuthImageUserError("未找到可用于编辑的图片。")
        return await self._execute(
            prompt=prompt,
            size=self.config.get("codex_oauth_image_default_size", DEFAULT_CODEX_SIZE),
            reference_images=[str(source)],
            action="edit",
        )

    def _validate_prompt(self, prompt: str) -> str:
        clean_prompt = str(prompt or "").strip()
        if not clean_prompt:
            raise CodexOAuthImageUserError("图片提示词不能为空。")
        if len(clean_prompt) > self.MAX_PROMPT_CHARS:
            raise CodexOAuthImageUserError("图片提示词最多 512 个字符。")
        return clean_prompt

    def _resolve_timeout(self) -> float:
        raw_timeout = self.config.get("codex_oauth_image_timeout", 300)
        timeout = None
        timeout_error = None
        try:
            timeout = float(raw_timeout)
        except Exception:
            timeout_error = CodexOAuthImageConfigError("Codex OAuth 超时配置无效。")
        if timeout_error is not None:
            raise timeout_error
        if timeout < 30 or timeout > 900:
            raise CodexOAuthImageConfigError("Codex OAuth 超时必须在 30 至 900 秒之间。")
        return timeout

    def _resolve_provider(self, *, needs_edit: bool) -> tuple[Any, Any]:
        provider_id = str(
            self.config.get("codex_oauth_image_provider_id")
            or DEFAULT_CODEX_PROVIDER_ID
        ).strip()
        provider = None
        getter_is_callable = False
        lookup_error = None
        try:
            getter = getattr(self.context, "get_provider_by_id", None)
            getter_is_callable = callable(getter)
            provider = getter(provider_id) if getter_is_callable else None
        except Exception:
            lookup_error = CodexOAuthImageProviderError(
                "Codex OAuth 图片 Provider 查询失败。"
            )
        if lookup_error is not None:
            raise lookup_error
        if not getter_is_callable:
            raise CodexOAuthImageConfigError("Codex OAuth 图片 Provider 查询接口不存在。")
        if provider is None:
            raise CodexOAuthImageConfigError("Codex OAuth 图片 Provider 不存在。")

        can_generate = False
        can_edit = False
        generate_image = None
        metadata_error = None
        try:
            capabilities = getattr(provider, "capabilities", {})
            if isinstance(capabilities, dict):
                can_generate = bool(capabilities.get("image_generate"))
                can_edit = bool(capabilities.get("image_edit"))
            generate_image = getattr(provider, "generate_image", None)
        except Exception:
            metadata_error = CodexOAuthImageProviderError(
                "Codex OAuth 图片 Provider 元数据读取失败。"
            )
        if metadata_error is not None:
            raise metadata_error
        if not can_generate:
            raise CodexOAuthImageConfigError("图片 Provider 缺少 image_generate 能力。")
        if needs_edit and not can_edit:
            raise CodexOAuthImageConfigError("图片 Provider 缺少 image_edit 能力。")
        if not callable(generate_image):
            raise CodexOAuthImageConfigError("图片 Provider 缺少 generate_image 方法。")
        return provider, generate_image

    async def _execute(
        self,
        *,
        prompt: str,
        size: str,
        reference_images: list[str] | None,
        action: str,
    ) -> CodexOAuthImageResult:
        clean_prompt = self._validate_prompt(prompt)
        provider, generate_image = self._resolve_provider(
            needs_edit=bool(reference_images)
        )
        resolved_size = self.normalize_size(
            size
            or self.config.get("codex_oauth_image_default_size")
            or DEFAULT_CODEX_SIZE
        )
        model = str(
            self.config.get("codex_oauth_image_model") or DEFAULT_CODEX_MODEL
        ).strip()
        timeout = self._resolve_timeout()
        generated = None
        service_error = None
        provider_error = None
        try:
            async with _temporary_provider_timeout(provider, timeout):
                generated = await generate_image(
                    prompt=clean_prompt,
                    model=model,
                    size=resolved_size,
                    n=1,
                    reference_images=reference_images or None,
                    action=action,
                )
        except (CodexOAuthImageUserError, CodexOAuthImageConfigError) as error:
            service_error = error
        except Exception:
            provider_error = CodexOAuthImageProviderError(
                "Codex OAuth 图片 Provider 调用失败。"
            )
        if service_error is not None:
            raise service_error
        if provider_error is not None:
            raise provider_error

        results = None
        result_path = None
        media_type = None
        revised_prompt = None
        result_is_file = False
        result_error = None
        try:
            results = list([] if generated is None else generated)
            if results:
                first = results[0]
                result_path = Path(str(getattr(first, "path", "") or ""))
                media_type = str(
                    getattr(first, "mime_type", "") or "image/png"
                )
                revised_prompt = str(getattr(first, "revised_prompt", "") or "")
                result_is_file = result_path.is_file()
        except Exception:
            result_error = CodexOAuthImageProviderError(
                "Codex OAuth 图片结果读取失败。"
            )
        if result_error is not None:
            raise result_error
        if not results:
            raise CodexOAuthImageProviderError("Codex OAuth 图片调用未返回结果。")
        if not result_is_file:
            raise CodexOAuthImageProviderError("Codex OAuth 图片结果文件不可用。")
        return CodexOAuthImageResult(
            path=str(result_path),
            mode=action,
            media_type=media_type,
            revised_prompt=revised_prompt,
        )
