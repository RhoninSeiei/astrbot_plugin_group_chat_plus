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
from .grok_oauth_image_service import (
    GrokOAuthImageConfigError,
    GrokOAuthImageProviderError,
    GrokOAuthImageService,
    GrokOAuthImageUserError,
)


class GroupImageUserError(Exception):
    pass


class GroupImageConfigError(Exception):
    pass


class GroupImageProviderError(Exception):
    def __init__(
        self,
        message: str,
        *,
        reason_code: str = "provider_call_failed",
        backend: str = "unknown",
    ) -> None:
        super().__init__(message)
        self.reason_code = (
            reason_code
            if reason_code in {
                "provider_call_failed",
                "provider_timeout",
                "provider_lookup_failed",
                "provider_metadata_failed",
                "source_file_check_failed",
                "result_read_failed",
                "empty_result",
                "result_file_missing",
            }
            else "provider_call_failed"
        )
        self.backend = backend if backend in {"stepfun", "codex_oauth", "grok_oauth"} else "unknown"


@dataclass(frozen=True)
class GroupImageResult:
    path: str
    mode: str
    backend: str
    media_type: str = "image/png"
    revised_prompt: str = ""


class _PlannerProviderContext:
    def __init__(self, context, provider_id, provider):
        self._context = context
        self._provider_id = provider_id
        self._provider = provider

    def get_provider_by_id(self, provider_id):
        if provider_id == self._provider_id:
            return self._provider
        raise GroupImageConfigError("图片调用不能切换至其他规划提供商。")

    def get_all_providers(self):
        return [self._provider]

    def __getattr__(self, name):
        return getattr(self._context, name)


class GroupImageService:
    BACKEND_STEPFUN = "stepfun"
    BACKEND_CODEX_OAUTH = "codex_oauth"
    BACKEND_GROK_OAUTH = "grok_oauth"

    def __init__(
        self,
        *,
        context: Any,
        config: dict,
        output_dir: Path | None,
        stepfun_factory: Callable[..., Any] = StepImageService,
        codex_factory: Callable[..., Any] = CodexOAuthImageService,
        grok_factory: Callable[..., Any] = GrokOAuthImageService,
        planner_provider: Any = None,
        require_planner: bool = False,
    ) -> None:
        self.context = context
        self.config = dict(config or {})
        self.output_dir = output_dir
        self._stepfun_factory = stepfun_factory
        self._codex_factory = codex_factory
        self._grok_factory = grok_factory
        self._planner = planner_provider
        self._require_planner = require_planner
        if planner_provider is not None:
            provider_id = str(getattr(planner_provider, "provider_config", {}).get("id") or "").strip()
            if not provider_id:
                raise GroupImageConfigError("无法确定实际图片规划模型。")
            self.config["image_planner_provider_id"] = provider_id
            # Pin the exact instance that issued this tool call. A provider
            # reload must not redirect an in-flight call through a new lookup.
            self.context = _PlannerProviderContext(context, provider_id, planner_provider)

    @staticmethod
    def is_enabled(config: dict) -> bool:
        return StepImageService.is_enabled(config or {})

    def backend_name(self) -> str:
        if self._require_planner and self._planner is None:
            raise GroupImageConfigError("无法确定实际图片规划模型，未发起绘图请求。")
        planner_id = str(self.config.get("image_planner_provider_id") or "").strip()
        if planner_id:
            lookup_failed = False
            try:
                if self._planner is None:
                    self._planner = self.context.get_provider_by_id(planner_id)
                adapter_type = self._planner.meta().type if self._planner is not None else ""
            except Exception:
                lookup_failed = True
            if lookup_failed:
                raise GroupImageConfigError("图片规划提供商查询失败。") from None
            if adapter_type == "openai_oauth_chat_completion":
                return self.BACKEND_CODEX_OAUTH
            if adapter_type == "grok_oauth_chat_completion":
                return self.BACKEND_GROK_OAUTH
            raise GroupImageConfigError("图片规划模型必须选择 Codex OAuth 或 Grok OAuth 提供商。")
        raw = self.config.get("image_tool_backend")
        name = "stepfun" if raw in (None, "") else str(raw).strip().lower()
        if name not in {self.BACKEND_STEPFUN, self.BACKEND_CODEX_OAUTH}:
            raise GroupImageConfigError("图片工具后端配置无效。")
        return name

    def display_name(self) -> str:
        if self.backend_name() == self.BACKEND_GROK_OAUTH:
            return "Grok Imagine 图像生成服务"
        if self.backend_name() == self.BACKEND_CODEX_OAUTH:
            return "OpenAI Codex 图像生成服务"
        return "阶跃星辰 Step Image Edit 2"

    def max_prompt_chars(self) -> int:
        if self.backend_name() == self.BACKEND_GROK_OAUTH:
            return GrokOAuthImageService.MAX_PROMPT_CHARS
        if self.backend_name() == self.BACKEND_CODEX_OAUTH:
            return CodexOAuthImageService.MAX_PROMPT_CHARS
        return StepImageService.MAX_PROMPT_CHARS

    def _validate_prompt(self, prompt: str) -> None:
        clean_prompt = str(prompt or "").strip()
        if not clean_prompt:
            raise GroupImageUserError("图片提示词不能为空。")
        max_prompt_chars = self.max_prompt_chars()
        if len(clean_prompt) > max_prompt_chars:
            raise GroupImageUserError(
                f"图片提示词最多 {max_prompt_chars} 个字符。"
            )

    def _backend(self) -> Any:
        if self.backend_name() == self.BACKEND_GROK_OAUTH:
            return self._grok_factory(context=self.context, config=self.config)
        if self.backend_name() == self.BACKEND_CODEX_OAUTH:
            config = dict(self.config)
            if self._planner is not None:
                config["codex_oauth_image_provider_id"] = config["image_planner_provider_id"]
                model_error = False
                try:
                    config["codex_oauth_image_model"] = self._planner.get_model()
                except Exception:
                    model_error = True
                if model_error:
                    raise GroupImageConfigError("图片规划模型读取失败。") from None
            return self._codex_factory(context=self.context, config=config)
        if self.output_dir is None:
            raise GroupImageConfigError("StepFun 图片输出目录未配置。")
        return self._stepfun_factory(
            context=self.context,
            config=self.config,
            output_dir=self.output_dir,
        )

    def _default_size(self) -> str:
        if self.backend_name() == self.BACKEND_GROK_OAUTH:
            return str(self.config.get("grok_image_aspect_ratio") or "1:1").strip()
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
        self._validate_prompt(prompt)
        provider_error = None
        try:
            backend = self._backend()
            result = await backend.generate(
                prompt=prompt,
                size=str(size or self._default_size()).strip(),
            )
        except (StepImageUserError, CodexOAuthImageUserError, GrokOAuthImageUserError) as exc:
            raise GroupImageUserError(str(exc)) from None
        except (StepImageConfigError, CodexOAuthImageConfigError, GrokOAuthImageConfigError):
            raise GroupImageConfigError("图片工具配置不可用。") from None
        except StepImageProviderError:
            provider_error = GroupImageProviderError(
                "图片服务调用失败。",
                backend=self.BACKEND_STEPFUN,
            )
        except GrokOAuthImageProviderError as exc:
            provider_error = GroupImageProviderError(
                "图片服务调用失败。", reason_code=exc.reason_code,
                backend=self.BACKEND_GROK_OAUTH,
            )
        except CodexOAuthImageProviderError as exc:
            provider_error = GroupImageProviderError(
                "图片服务调用失败。",
                reason_code=exc.reason_code,
                backend=self.BACKEND_CODEX_OAUTH,
            )
        if provider_error is not None:
            raise provider_error from None
        return self._convert_result(result)

    async def edit(self, *, prompt: str, image_path: str) -> GroupImageResult:
        self._validate_prompt(prompt)
        provider_error = None
        try:
            backend = self._backend()
            result = await backend.edit(prompt=prompt, image_path=image_path)
        except (StepImageUserError, CodexOAuthImageUserError, GrokOAuthImageUserError) as exc:
            raise GroupImageUserError(str(exc)) from None
        except (StepImageConfigError, CodexOAuthImageConfigError, GrokOAuthImageConfigError):
            raise GroupImageConfigError("图片工具配置不可用。") from None
        except StepImageProviderError:
            provider_error = GroupImageProviderError(
                "图片服务调用失败。",
                backend=self.BACKEND_STEPFUN,
            )
        except GrokOAuthImageProviderError as exc:
            provider_error = GroupImageProviderError(
                "图片服务调用失败。", reason_code=exc.reason_code,
                backend=self.BACKEND_GROK_OAUTH,
            )
        except CodexOAuthImageProviderError as exc:
            provider_error = GroupImageProviderError(
                "图片服务调用失败。",
                reason_code=exc.reason_code,
                backend=self.BACKEND_CODEX_OAUTH,
            )
        if provider_error is not None:
            raise provider_error from None
        return self._convert_result(result)
