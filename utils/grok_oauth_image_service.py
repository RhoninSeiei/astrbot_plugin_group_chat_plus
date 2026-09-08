from __future__ import annotations

import asyncio
import base64
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class GrokOAuthImageUserError(Exception):
    pass


class GrokOAuthImageConfigError(Exception):
    pass


class GrokOAuthImageProviderError(Exception):
    def __init__(self, message: str, *, reason_code: str = "provider_call_failed"):
        super().__init__(message)
        self.reason_code = reason_code


@dataclass(frozen=True)
class GrokOAuthImageResult:
    path: str
    mode: str
    backend: str = "grok_oauth"
    media_type: str = "image/png"
    revised_prompt: str = ""


class GrokOAuthImageService:
    """Use the installed Grok OAuth provider SDK without owning its credentials."""

    MAX_PROMPT_CHARS = 2048
    MAX_REFERENCE_BYTES = 20 * 1024 * 1024
    ASPECT_RATIOS = {"auto", "1:1", "3:2", "2:3", "4:3", "3:4", "16:9", "9:16", "21:9", "5:2"}

    def __init__(self, *, context: Any, config: dict):
        self.context = context
        self.config = dict(config or {})

    def _geometry(self, size: str) -> tuple[str, str]:
        value = "".join(str(size or self.config.get("grok_image_aspect_ratio") or "1:1").lower().split())
        value = value.replace("×", "x")
        resolution = str(self.config.get("grok_image_resolution") or "1k")
        aliases = {
            "1024x1024": "1:1", "2048x2048": "1:1", "square": "1:1", "方图": "1:1",
            "1536x1024": "3:2", "1024x1536": "2:3",
            "1080p": "16:9", "1920x1080": "16:9", "1080x1920": "9:16",
            "landscape": "16:9", "横图": "16:9", "portrait": "9:16", "竖图": "9:16",
        }
        if value == "2048x2048":
            resolution = "2k"
        ratio = aliases.get(value, value)
        if ratio not in self.ASPECT_RATIOS:
            raise GrokOAuthImageUserError("Grok 图片比例不受支持，请使用 1:1、16:9、9:16 等比例。")
        if resolution not in {"1k", "2k"}:
            raise GrokOAuthImageConfigError("Grok 图片分辨率必须为 1k 或 2k。")
        return ratio, resolution

    async def generate(self, *, prompt: str, size: str = "") -> GrokOAuthImageResult:
        return await self._execute(prompt=prompt, size=size, image_path=None)

    async def edit(self, *, prompt: str, image_path: str) -> GrokOAuthImageResult:
        return await self._execute(prompt=prompt, size="", image_path=image_path)

    async def _execute(self, *, prompt: str, size: str, image_path: str | None) -> GrokOAuthImageResult:
        prompt = str(prompt or "").strip()
        if not prompt or len(prompt) > self.MAX_PROMPT_CHARS:
            raise GrokOAuthImageUserError("图片提示词不能为空，且最多 2048 个字符。")
        ratio, resolution = self._geometry(size)
        model = str(self.config.get("grok_image_model") or "grok-imagine-image-2.0").strip()
        if not model.startswith("grok-imagine-image"):
            raise GrokOAuthImageConfigError("Grok 绘图必须使用 Imagine 图片模型。")
        invalid_timeout = False
        try:
            raw_timeout = self.config.get("grok_image_timeout", 180)
            timeout = float(raw_timeout)
            invalid_timeout = isinstance(raw_timeout, bool) or not math.isfinite(timeout) or not 1 <= timeout <= 600
        except (TypeError, ValueError):
            invalid_timeout = True
        if invalid_timeout:
            raise GrokOAuthImageConfigError("Grok 图片超时必须在 1 至 600 秒之间。")

        provider_error = None
        try:
            provider_id = str(self.config.get("image_planner_provider_id") or "").strip()
            provider = self.context.get_provider_by_id(provider_id)
            if provider is None or provider.meta().type != "grok_oauth_chat_completion":
                raise GrokOAuthImageConfigError("请选择有效的 Grok OAuth 图片规划提供商。")
            capabilities = provider.capabilities
            capability = capabilities.get("image_editing" if image_path is not None else "image_generation", {})
            if not isinstance(capability, dict) or capability.get("implementation") is not True or capability.get("enabled") is not True:
                raise GrokOAuthImageConfigError("Grok 提供商的图片能力未启用。")
            generate_image = getattr(provider, "generate_image", None)
            if not callable(generate_image):
                raise GrokOAuthImageConfigError("Grok 提供商缺少图片 SDK。")
        except GrokOAuthImageConfigError:
            raise
        except Exception:
            provider_error = GrokOAuthImageProviderError("Grok 图片提供商查询失败。", reason_code="provider_lookup_failed")
        if provider_error is not None:
            raise provider_error from None

        references = None
        if image_path is not None:
            source_error = None
            try:
                # SDK file allowlists belong to the Grok plugin. Pass bounded original
                # bytes instead of changing that plugin's permitted filesystem roots.
                def read_reference():
                    with Path(image_path).open("rb") as stream:
                        return stream.read(self.MAX_REFERENCE_BYTES + 1)
                content = await asyncio.to_thread(read_reference)
            except (OSError, ValueError):
                source_error = GrokOAuthImageUserError("未找到可用于编辑的图片。")
            if source_error is not None:
                raise source_error from None
            if len(content) > self.MAX_REFERENCE_BYTES:
                raise GrokOAuthImageUserError("参考图片超过 20 MiB。")
            if content.startswith(b"\x89PNG\r\n\x1a\n"):
                mime = "image/png"
            elif content.startswith(b"\xff\xd8\xff"):
                mime = "image/jpeg"
            elif content.startswith((b"GIF87a", b"GIF89a")):
                mime = "image/gif"
            elif content.startswith(b"RIFF") and content[8:12] == b"WEBP":
                mime = "image/webp"
            else:
                raise GrokOAuthImageUserError("参考图片格式不受支持。")
            references = [f"data:{mime};base64," + base64.b64encode(content).decode("ascii")]

        action = "edit" if image_path is not None else "generate"
        failure = None
        try:
            # One SDK call only. Cancellation propagates; uncertain outcomes and
            # rate limits must never cause a second image request or backend switch.
            results = await asyncio.wait_for(generate_image(
                prompt=prompt, model=model, n=1, reference_images=references,
                action=action, timeout=timeout, aspect_ratio=ratio, resolution=resolution,
            ), timeout=timeout)
        except asyncio.TimeoutError:
            failure = GrokOAuthImageProviderError("Grok 图片请求超时。", reason_code="provider_timeout")
        except Exception:
            failure = GrokOAuthImageProviderError("Grok 图片服务调用失败。")
        if failure is not None:
            raise failure from None

        result_error = None
        try:
            if not results:
                raise ValueError("empty result")
            first = results[0]
            path = Path(first.path)
            if not await asyncio.to_thread(path.is_file):
                raise ValueError("missing output")
            result = GrokOAuthImageResult(
                path=str(path), mode=action,
                media_type=str(first.mime_type or "image/png"),
                revised_prompt=str(getattr(first, "revised_prompt", "") or ""),
            )
        except Exception:
            result_error = GrokOAuthImageProviderError("Grok 图片结果不可用。", reason_code="result_read_failed")
        if result_error is not None:
            raise result_error from None
        return result
