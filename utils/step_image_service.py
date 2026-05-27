"""
StepFun image generation and editing service.

This module intentionally keeps API keys, endpoints, response payloads, and
temporary file details away from LLM prompts and tool return text.
"""

import base64
import os
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

import httpx


DEFAULT_MODEL = "step-image-edit-2"
DEFAULT_API_BASE = "https://api.stepfun.com/v1"
DEFAULT_GENERATION_SIZE = "768x1360"
GENERATION_SIZE_OPTIONS = (
    "768x1360",
    "1360x768",
    "896x1184",
    "1184x896",
    "1024x1024",
)
VALID_GENERATION_SIZES = set(GENERATION_SIZE_OPTIONS)
GENERATION_SIZE_ALIASES = {
    "16:9": "768x1360",
    "16比9": "768x1360",
    "1080p": "768x1360",
    "720p": "768x1360",
    "2k": "768x1360",
    "4k": "768x1360",
    "横屏": "768x1360",
    "横图": "768x1360",
    "宽屏": "768x1360",
    "landscape": "768x1360",
    "wide": "768x1360",
    "widescreen": "768x1360",
    "1280x720": "768x1360",
    "1366x768": "768x1360",
    "1600x900": "768x1360",
    "1920x1080": "768x1360",
    "2560x1440": "768x1360",
    "3840x2160": "768x1360",
    "9:16": "1360x768",
    "9比16": "1360x768",
    "竖屏": "1360x768",
    "竖图": "1360x768",
    "portrait": "1360x768",
    "720x1280": "1360x768",
    "1080x1920": "1360x768",
    "1440x2560": "1360x768",
    "2160x3840": "1360x768",
    "4:3": "896x1184",
    "4比3": "896x1184",
    "传统横屏": "896x1184",
    "1024x768": "896x1184",
    "1600x1200": "896x1184",
    "3:4": "1184x896",
    "3比4": "1184x896",
    "传统竖屏": "1184x896",
    "768x1024": "1184x896",
    "1200x1600": "1184x896",
    "1:1": "1024x1024",
    "1比1": "1024x1024",
    "方图": "1024x1024",
    "方形": "1024x1024",
    "square": "1024x1024",
    "头像": "1024x1024",
}
GENERATION_SIZE_HINT = (
    "16:9/1080p=768x1360, 9:16=1360x768, 4:3=896x1184, "
    "3:4=1184x896, 1:1=1024x1024"
)


class StepImageUserError(Exception):
    """User-facing validation error."""


class StepImageConfigError(Exception):
    """Configuration error safe to show after sanitization."""


class StepImageProviderError(Exception):
    """Provider request failed; message must already be sanitized."""


class StepImageSettings:
    def __init__(
        self,
        *,
        provider_id: str,
        api_key: str,
        api_base: str,
        model: str,
        timeout: float,
        proxy: str,
        cfg_scale: float,
        steps: int,
        seed: Optional[int],
        text_mode: bool,
    ) -> None:
        self.provider_id = provider_id
        self.api_key = api_key
        self.api_base = api_base
        self.model = model
        self.timeout = timeout
        self.proxy = proxy
        self.cfg_scale = cfg_scale
        self.steps = steps
        self.seed = seed
        self.text_mode = text_mode


class StepImageResult:
    def __init__(self, *, path: str, mode: str) -> None:
        self.path = path
        self.mode = mode


class StepImageService:
    MAX_PROMPT_CHARS = 512

    def __init__(
        self,
        *,
        context: Any,
        config: dict,
        output_dir: Path,
        client_factory: Optional[Callable[..., Any]] = None,
    ) -> None:
        self.context = context
        self.config = config
        self.output_dir = output_dir
        self._client_factory = client_factory or self._default_client_factory

    @staticmethod
    def is_enabled(config: dict) -> bool:
        return StepImageService._as_bool(
            (config or {}).get("enable_step_image_tools"), False
        )

    def resolve_settings(self) -> StepImageSettings:
        model = str(self.config.get("step_image_model") or DEFAULT_MODEL).strip()
        provider = self._resolve_provider(model)
        provider_config = self._merge_provider_source_config(
            getattr(provider, "provider_config", {}) or {}
        )

        provider_id = str(provider_config.get("id") or "").strip()
        api_key = self._extract_api_key(provider, provider_config)
        api_base = self._normalize_api_base(
            str(
                self.config.get("step_image_api_base")
                or provider_config.get("api_base")
                or DEFAULT_API_BASE
            )
        )

        if not api_key:
            raise StepImageConfigError("StepFun 图片模型缺少 API Key 配置")

        return StepImageSettings(
            provider_id=provider_id,
            api_key=api_key,
            api_base=api_base,
            model=model,
            timeout=self._as_float(
                self.config.get("step_image_timeout")
                or provider_config.get("timeout"),
                60,
                "step_image_timeout",
            ),
            proxy=str(
                self.config.get("step_image_proxy") or provider_config.get("proxy") or ""
            ),
            cfg_scale=self._as_float(
                self.config.get("step_image_cfg_scale"), 1.0, "step_image_cfg_scale"
            ),
            steps=self._as_int(
                self.config.get("step_image_steps"), 8, "step_image_steps"
            ),
            seed=self._optional_int(self.config.get("step_image_seed")),
            text_mode=self._as_bool(self.config.get("step_image_text_mode"), True),
        )

    async def generate(
        self, *, prompt: str, size: str = DEFAULT_GENERATION_SIZE
    ) -> StepImageResult:
        self._validate_prompt(prompt)
        normalized_size = self.normalize_generation_size(size)
        if normalized_size not in VALID_GENERATION_SIZES:
            raise StepImageUserError(
                "图片尺寸仅支持: "
                + ", ".join(GENERATION_SIZE_OPTIONS)
                + "。可使用别名: "
                + GENERATION_SIZE_HINT
            )

        settings = self.resolve_settings()
        payload = {
            "model": settings.model,
            "prompt": prompt.strip(),
            "response_format": "b64_json",
            "size": normalized_size,
            "cfg_scale": settings.cfg_scale,
            "steps": settings.steps,
            "text_mode": settings.text_mode,
        }
        if settings.seed is not None:
            payload["seed"] = settings.seed

        response_payload = await self._post_json(
            f"{settings.api_base}/images/generations",
            payload,
            settings,
        )
        path = self._write_b64_image(response_payload, "generate")
        return StepImageResult(path=str(path), mode="generate")

    async def edit(self, *, prompt: str, image_path: str) -> StepImageResult:
        self._validate_prompt(prompt)
        source_path = Path(image_path)
        if not source_path.is_file():
            raise StepImageUserError("未找到可用于编辑的图片")

        settings = self.resolve_settings()
        data = {
            "model": settings.model,
            "prompt": prompt.strip(),
            "response_format": "b64_json",
            "cfg_scale": str(settings.cfg_scale),
            "steps": str(settings.steps),
            "text_mode": str(settings.text_mode).lower(),
        }
        if settings.seed is not None:
            data["seed"] = str(settings.seed)

        response_payload = await self._post_multipart(
            f"{settings.api_base}/images/edits",
            data,
            source_path,
            settings,
        )
        path = self._write_b64_image(response_payload, "edit")
        return StepImageResult(path=str(path), mode="edit")

    def _resolve_provider(self, model: str) -> Any:
        provider_id = str(self.config.get("step_image_provider_id") or "").strip()
        if provider_id:
            provider = self.context.get_provider_by_id(provider_id)
            if not provider:
                raise StepImageConfigError("未找到配置的 StepFun 图片模型 provider")
            return provider

        if hasattr(self.context, "get_all_providers"):
            for provider in self.context.get_all_providers():
                provider_config = self._merge_provider_source_config(
                    getattr(provider, "provider_config", {}) or {}
                )
                if str(provider_config.get("model") or "").strip() == model:
                    return provider

        raise StepImageConfigError("未找到 step-image-edit-2 provider")

    @staticmethod
    def normalize_generation_size(size: Any) -> str:
        raw_size = str(size or "").strip()
        if not raw_size:
            return DEFAULT_GENERATION_SIZE

        normalized = (
            raw_size.lower()
            .replace("×", "x")
            .replace("＊", "x")
            .replace("*", "x")
            .replace("：", ":")
        )
        normalized = "".join(normalized.split())

        if normalized in VALID_GENERATION_SIZES:
            return normalized
        return GENERATION_SIZE_ALIASES.get(normalized, raw_size)

    def _merge_provider_source_config(self, provider_config: dict) -> dict:
        provider_source_id = str(provider_config.get("provider_source_id") or "").strip()
        if not provider_source_id:
            return provider_config

        provider_manager = getattr(self.context, "provider_manager", None)
        provider_sources = getattr(provider_manager, "provider_sources_config", []) or []
        for source_config in provider_sources:
            if source_config.get("id") == provider_source_id:
                merged_config = {**source_config, **provider_config}
                merged_config["id"] = provider_config.get("id", merged_config.get("id"))
                return merged_config
        return provider_config

    @staticmethod
    def _extract_api_key(provider: Any, provider_config: dict) -> str:
        for attr in ("chosen_api_key", "api_key"):
            value = getattr(provider, attr, None)
            if isinstance(value, str) and value.strip():
                return value.strip()

        keys = provider_config.get("key") or provider_config.get("api_key")
        if isinstance(keys, str):
            return keys.strip()
        if isinstance(keys, list):
            for key in keys:
                if isinstance(key, str) and key.strip():
                    return key.strip()
        return ""

    @staticmethod
    def _normalize_api_base(api_base: str) -> str:
        normalized = (api_base or DEFAULT_API_BASE).strip().rstrip("/")
        if not normalized:
            normalized = DEFAULT_API_BASE
        if normalized.endswith("/v1"):
            return normalized
        return f"{normalized}/v1"

    @staticmethod
    def _optional_int(value: Any) -> Optional[int]:
        if value is None or value == "":
            return None
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise StepImageConfigError("step_image_seed 必须是整数") from exc

    @staticmethod
    def _as_int(value: Any, default: int, field_name: str) -> int:
        if value is None or value == "":
            return default
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise StepImageConfigError(f"{field_name} 必须是整数") from exc

    @staticmethod
    def _as_float(value: Any, default: float, field_name: str) -> float:
        if value is None or value == "":
            return default
        try:
            return float(value)
        except (TypeError, ValueError) as exc:
            raise StepImageConfigError(f"{field_name} 必须是数字") from exc

    @staticmethod
    def _as_bool(value: Any, default: bool) -> bool:
        if value is None or value == "":
            return default
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"true", "1", "yes", "on"}:
                return True
            if normalized in {"false", "0", "no", "off"}:
                return False
        return bool(value)

    def _validate_prompt(self, prompt: str) -> None:
        clean_prompt = (prompt or "").strip()
        if not clean_prompt:
            raise StepImageUserError("图片提示词不能为空")
        if len(clean_prompt) > self.MAX_PROMPT_CHARS:
            raise StepImageUserError("图片提示词最多 512 个字符")

    async def _post_json(
        self,
        url: str,
        payload: dict,
        settings: StepImageSettings,
    ) -> dict:
        headers = self._headers(settings)
        try:
            async with self._client_factory(
                headers=headers,
                timeout=settings.timeout,
                proxy=settings.proxy,
            ) as client:
                response = await client.post(url, json=payload)
                response.raise_for_status()
                return response.json()
        except Exception as exc:
            raise StepImageProviderError(self._sanitize_error(exc, settings)) from exc

    async def _post_multipart(
        self,
        url: str,
        data: dict,
        source_path: Path,
        settings: StepImageSettings,
    ) -> dict:
        headers = self._headers(settings)
        try:
            with source_path.open("rb") as image_file:
                files = {"image": (source_path.name, image_file)}
                async with self._client_factory(
                    headers=headers,
                    timeout=settings.timeout,
                    proxy=settings.proxy,
                ) as client:
                    response = await client.post(url, data=data, files=files)
                    response.raise_for_status()
                    return response.json()
        except Exception as exc:
            raise StepImageProviderError(self._sanitize_error(exc, settings)) from exc

    @staticmethod
    def _headers(settings: StepImageSettings) -> dict:
        return {"Authorization": f"Bearer {settings.api_key}"}

    @staticmethod
    def _default_client_factory(*, headers: dict, timeout: float, proxy: str):
        kwargs = {"headers": headers, "timeout": timeout}
        if proxy:
            kwargs["proxy"] = proxy
        return httpx.AsyncClient(**kwargs)

    def _write_b64_image(self, payload: dict, prefix: str) -> Path:
        try:
            b64_json = payload["data"][0]["b64_json"]
            image_bytes = base64.b64decode(b64_json)
        except Exception as exc:
            raise StepImageProviderError("StepFun 图片接口返回格式异常") from exc

        self.output_dir.mkdir(parents=True, exist_ok=True)
        image_path = self.output_dir / f"step_image_{prefix}_{uuid.uuid4().hex}.png"
        image_path.write_bytes(image_bytes)
        return image_path

    @staticmethod
    def _sanitize_error(exc: Exception, settings: StepImageSettings) -> str:
        text = str(exc) or exc.__class__.__name__
        secrets = [
            settings.api_key,
            os.environ.get("STEP_API_KEY", ""),
        ]
        for secret in secrets:
            if secret:
                text = text.replace(secret, "[REDACTED]")
        if len(text) > 300:
            text = text[:300] + "..."
        return f"StepFun 图片调用失败: {text}"
