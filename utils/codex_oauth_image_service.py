from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_CODEX_PROVIDER_ID = "openai_oauth/gpt-5.6-sol"
DEFAULT_CODEX_MODEL = "gpt-5.6-sol"
DEFAULT_CODEX_SIZE = "1024x1024"
VALID_CODEX_SIZES = {"1024x1024", "1536x1024", "1024x1536"}
_PROVIDER_TIMEOUT_LOCKS: dict[int, asyncio.Lock] = {}


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
    provider_key = id(provider)
    lock = _PROVIDER_TIMEOUT_LOCKS.get(provider_key)
    if lock is None:
        lock = asyncio.Lock()
        _PROVIDER_TIMEOUT_LOCKS[provider_key] = lock
    return lock


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
        source = Path(str(image_path))
        if not source.is_file():
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
        try:
            timeout = float(raw_timeout)
        except (TypeError, ValueError) as exc:
            raise CodexOAuthImageConfigError("Codex OAuth 超时配置无效。") from exc
        if timeout < 30 or timeout > 900:
            raise CodexOAuthImageConfigError("Codex OAuth 超时必须在 30 至 900 秒之间。")
        return timeout

    def _resolve_provider(self, *, needs_edit: bool) -> Any:
        provider_id = str(
            self.config.get("codex_oauth_image_provider_id")
            or DEFAULT_CODEX_PROVIDER_ID
        ).strip()
        getter = getattr(self.context, "get_provider_by_id", None)
        provider = getter(provider_id) if callable(getter) else None
        if provider is None:
            raise CodexOAuthImageConfigError("Codex OAuth 图片 Provider 不存在。")
        capabilities = getattr(provider, "capabilities", {})
        if not isinstance(capabilities, dict) or not capabilities.get("image_generate"):
            raise CodexOAuthImageConfigError("图片 Provider 缺少 image_generate 能力。")
        if needs_edit and not capabilities.get("image_edit"):
            raise CodexOAuthImageConfigError("图片 Provider 缺少 image_edit 能力。")
        if not callable(getattr(provider, "generate_image", None)):
            raise CodexOAuthImageConfigError("图片 Provider 缺少 generate_image 方法。")
        return provider

    async def _execute(
        self,
        *,
        prompt: str,
        size: str,
        reference_images: list[str] | None,
        action: str,
    ) -> CodexOAuthImageResult:
        clean_prompt = self._validate_prompt(prompt)
        provider = self._resolve_provider(needs_edit=bool(reference_images))
        resolved_size = self.normalize_size(
            size
            or self.config.get("codex_oauth_image_default_size")
            or DEFAULT_CODEX_SIZE
        )
        model = str(
            self.config.get("codex_oauth_image_model") or DEFAULT_CODEX_MODEL
        ).strip()
        timeout = self._resolve_timeout()
        try:
            async with _temporary_provider_timeout(provider, timeout):
                generated = await provider.generate_image(
                    prompt=clean_prompt,
                    model=model,
                    size=resolved_size,
                    n=1,
                    reference_images=reference_images or None,
                    action=action,
                )
        except (CodexOAuthImageUserError, CodexOAuthImageConfigError):
            raise
        except Exception as exc:
            raise CodexOAuthImageProviderError(
                f"Codex OAuth 图片调用失败: {exc.__class__.__name__}"
            ) from exc

        results = list(generated or [])
        if not results:
            raise CodexOAuthImageProviderError("Codex OAuth 图片调用未返回结果。")
        first = results[0]
        result_path = Path(str(getattr(first, "path", "") or ""))
        if not result_path.is_file():
            raise CodexOAuthImageProviderError("Codex OAuth 图片结果文件不可用。")
        return CodexOAuthImageResult(
            path=str(result_path),
            mode=action,
            media_type=str(getattr(first, "mime_type", "") or "image/png"),
            revised_prompt=str(getattr(first, "revised_prompt", "") or ""),
        )
