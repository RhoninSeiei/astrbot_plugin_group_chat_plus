from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .codex_oauth_image_service import (
    CodexOAuthImageConfigError,
    CodexOAuthImageProviderError,
    CodexOAuthImageService,
    CodexOAuthImageUserError,
)
from .step_image_service import (
    DEFAULT_GENERATION_SIZE,
    StepImageConfigError,
    StepImageProviderError,
    StepImageService,
    StepImageUserError,
)


class GroupImageUserError(Exception):
    pass


class GroupImageConfigError(Exception):
    pass


class GroupImageProviderError(Exception):
    pass


@dataclass(frozen=True)
class GroupImageResult:
    path: str
    mode: str
    backend: str
    media_type: str = "image/png"
    revised_prompt: str = ""


class GroupImageService:
    BACKEND_STEPFUN = "stepfun"
    BACKEND_CODEX_OAUTH = "codex_oauth"

    def __init__(
        self,
        *,
        context: Any,
        config: dict,
        output_dir: Path | None,
        stepfun_factory: Callable[..., Any] = StepImageService,
        codex_factory: Callable[..., Any] = CodexOAuthImageService,
    ) -> None:
        self.context = context
        self.config = dict(config or {})
        self.output_dir = output_dir
        self._stepfun_factory = stepfun_factory
        self._codex_factory = codex_factory

    @staticmethod
    def is_enabled(config: dict) -> bool:
        return StepImageService.is_enabled(config or {})

    def backend_name(self) -> str:
        raw = self.config.get("image_tool_backend")
        name = "stepfun" if raw in (None, "") else str(raw).strip().lower()
        if name not in {self.BACKEND_STEPFUN, self.BACKEND_CODEX_OAUTH}:
            raise GroupImageConfigError("图片工具后端配置无效。")
        return name

    def display_name(self) -> str:
        if self.backend_name() == self.BACKEND_CODEX_OAUTH:
            return "OpenAI Codex 图像生成服务"
        return "阶跃星辰 Step Image Edit 2"

    def _backend(self) -> Any:
        if self.backend_name() == self.BACKEND_CODEX_OAUTH:
            return self._codex_factory(context=self.context, config=self.config)
        if self.output_dir is None:
            raise GroupImageConfigError("StepFun 图片输出目录未配置。")
        return self._stepfun_factory(
            context=self.context,
            config=self.config,
            output_dir=self.output_dir,
        )

    def _default_size(self) -> str:
        if self.backend_name() == self.BACKEND_CODEX_OAUTH:
            return str(
                self.config.get("codex_oauth_image_default_size") or "1024x1024"
            ).strip()
        return str(
            self.config.get("step_image_default_size") or DEFAULT_GENERATION_SIZE
        ).strip()

    @staticmethod
    def _convert_result(result: Any) -> GroupImageResult:
        return GroupImageResult(
            path=str(getattr(result, "path", "") or ""),
            mode=str(getattr(result, "mode", "") or ""),
            backend=str(getattr(result, "backend", "") or "stepfun"),
            media_type=str(getattr(result, "media_type", "") or "image/png"),
            revised_prompt=str(getattr(result, "revised_prompt", "") or ""),
        )

    async def generate(self, *, prompt: str, size: str = "") -> GroupImageResult:
        try:
            backend = self._backend()
            result = await backend.generate(
                prompt=prompt,
                size=str(size or self._default_size()).strip(),
            )
        except (StepImageUserError, CodexOAuthImageUserError) as exc:
            raise GroupImageUserError(str(exc)) from None
        except (StepImageConfigError, CodexOAuthImageConfigError):
            raise GroupImageConfigError("图片工具配置不可用。") from None
        except (StepImageProviderError, CodexOAuthImageProviderError):
            raise GroupImageProviderError("图片服务调用失败。") from None
        return self._convert_result(result)

    async def edit(self, *, prompt: str, image_path: str) -> GroupImageResult:
        try:
            backend = self._backend()
            result = await backend.edit(prompt=prompt, image_path=image_path)
        except (StepImageUserError, CodexOAuthImageUserError) as exc:
            raise GroupImageUserError(str(exc)) from None
        except (StepImageConfigError, CodexOAuthImageConfigError):
            raise GroupImageConfigError("图片工具配置不可用。") from None
        except (StepImageProviderError, CodexOAuthImageProviderError):
            raise GroupImageProviderError("图片服务调用失败。") from None
        return self._convert_result(result)
