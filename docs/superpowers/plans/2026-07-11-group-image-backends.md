# Group Chat Plus Configurable Image Backends Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a configurable Codex OAuth image backend to Group Chat Plus, keep StepFun available, and deploy Codex OAuth as the production default without exposing credentials or internal tool protocol.

**Architecture:** Keep the existing StepFun service intact, add a Codex OAuth adapter that calls AstrBot Provider `generate_image()`, and place a small `GroupImageService` facade between `main.py` and both backends. Existing LLM tool names remain unchanged; schema selects the backend and its Provider, while progress text and model-facing summaries become backend-aware.

**Tech Stack:** Python 3.12, AstrBot plugin API, `asyncio`, `unittest`, JSON schema, Dashboard API, Linux production container.

## Global Constraints

- Production default: `image_tool_backend=codex_oauth`.
- Production Provider: `openai_oauth/gpt-5.6-sol`; Codex main model: `gpt-5.6-sol`.
- Old configs without `image_tool_backend` must continue using `stepfun` until production config is explicitly migrated.
- Keep `gcp_step_image_generate` and `gcp_step_image_edit` as the LLM tool names.
- Keep `enable_step_image_tools` as the existing master switch.
- Do not depend on ImgFlow modules or plugin lifecycle.
- Do not read or store OAuth tokens, account IDs, request headers, API keys, Dashboard JWTs, or raw Provider responses.
- Do not add automatic Codex-to-StepFun retry.
- Do not restart the production `astrbot` container; reload only `astrbot_plugin_group_chat_plus`.
- Preserve the unrelated untracked file `docs/superpowers/plans/2026-04-17-matoi-guardian-ep5-plugin.md`.

---

### Task 1: Codex OAuth image adapter

**Files:**
- Create: `utils/codex_oauth_image_service.py`
- Create: `tests/test_codex_oauth_image_service.py`

**Interfaces:**
- Consumes: `context.get_provider_by_id(provider_id)` and Provider `generate_image(prompt, model, size, n, reference_images, action)`.
- Produces: `CodexOAuthImageService.generate()`, `CodexOAuthImageService.edit()`, `CodexOAuthImageResult`, `CodexOAuthImageUserError`, `CodexOAuthImageConfigError`, and `CodexOAuthImageProviderError`.

- [ ] **Step 1: Write the failing service tests**

Create `tests/test_codex_oauth_image_service.py` with a fake Provider and these concrete assertions:

```python
import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from utils.codex_oauth_image_service import (
    CodexOAuthImageConfigError,
    CodexOAuthImageProviderError,
    CodexOAuthImageService,
    CodexOAuthImageUserError,
)


class FakeProvider:
    capabilities = {"image_generate": True, "image_edit": True}

    def __init__(self, result_path: Path):
        self.result_path = result_path
        self.calls = []
        self.timeout = 120
        self.timeout_during_call = None

    async def generate_image(self, **kwargs):
        self.calls.append(kwargs)
        self.timeout_during_call = self.timeout
        return [
            SimpleNamespace(
                path=str(self.result_path),
                mime_type="image/png",
                revised_prompt="revised",
            )
        ]


class FakeContext:
    def __init__(self, provider):
        self.provider = provider

    def get_provider_by_id(self, provider_id):
        if provider_id == "openai_oauth/gpt-5.6-sol":
            return self.provider
        return None


class CodexOAuthImageServiceTest(unittest.TestCase):
    def make_service(self, provider):
        return CodexOAuthImageService(
            context=FakeContext(provider),
            config={
                "codex_oauth_image_provider_id": "openai_oauth/gpt-5.6-sol",
                "codex_oauth_image_model": "gpt-5.6-sol",
                "codex_oauth_image_default_size": "1024x1024",
                "codex_oauth_image_timeout": 300,
            },
        )

    def test_generate_calls_public_provider_api_and_restores_timeout(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result_path = Path(tmpdir) / "result.png"
            result_path.write_bytes(b"png")
            provider = FakeProvider(result_path)
            result = asyncio.run(
                self.make_service(provider).generate(prompt="orange cat", size="16:9")
            )

        self.assertEqual(provider.calls, [{
            "prompt": "orange cat",
            "model": "gpt-5.6-sol",
            "size": "1536x1024",
            "n": 1,
            "reference_images": None,
            "action": "generate",
        }])
        self.assertEqual(provider.timeout_during_call, 300.0)
        self.assertEqual(provider.timeout, 120)
        self.assertEqual(result.backend, "codex_oauth")
        self.assertEqual(result.revised_prompt, "revised")

    def test_edit_passes_reference_path_and_edit_action(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "source.png"
            result_path = Path(tmpdir) / "result.png"
            source.write_bytes(b"source")
            result_path.write_bytes(b"result")
            provider = FakeProvider(result_path)
            asyncio.run(
                self.make_service(provider).edit(
                    prompt="change the sky", image_path=str(source)
                )
            )

        self.assertEqual(provider.calls[0]["reference_images"], [str(source)])
        self.assertEqual(provider.calls[0]["action"], "edit")

    def test_size_aliases_are_width_by_height(self):
        self.assertEqual(CodexOAuthImageService.normalize_size("1:1"), "1024x1024")
        self.assertEqual(CodexOAuthImageService.normalize_size("1920x1080"), "1536x1024")
        self.assertEqual(CodexOAuthImageService.normalize_size("1080x1920"), "1024x1536")
        with self.assertRaises(CodexOAuthImageUserError):
            CodexOAuthImageService.normalize_size("768x1360")

    def test_missing_capability_and_missing_result_are_safe_errors(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            provider = FakeProvider(Path(tmpdir) / "missing.png")
            provider.capabilities = {"image_generate": False, "image_edit": False}
            with self.assertRaises(CodexOAuthImageConfigError):
                asyncio.run(self.make_service(provider).generate(prompt="cat", size="1:1"))

            provider.capabilities = {"image_generate": True, "image_edit": True}
            with self.assertRaises(CodexOAuthImageProviderError) as caught:
                asyncio.run(self.make_service(provider).generate(prompt="cat", size="1:1"))

        message = str(caught.exception)
        self.assertNotIn(str(provider.result_path), message)
        self.assertNotIn("openai_oauth/gpt-5.6-sol", message)
```

- [ ] **Step 2: Run tests and verify the intended failure**

Run:

```bash
wsl.exe --cd ~ -- bash -lc "cd /mnt/s/Projects/astrbot_plugin_group_chat_plus && python3 -m unittest tests.test_codex_oauth_image_service -v"
```

Expected: import failure because `utils.codex_oauth_image_service` does not exist.

- [ ] **Step 3: Implement the minimal Codex OAuth adapter**

Create `utils/codex_oauth_image_service.py` with these exact public types and behavior:

```python
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
        return await self._execute(prompt=prompt, size=size, reference_images=None, action="generate")

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
```

Complete the module with these methods and timeout guard:

```python
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
    # Keep __init__, normalize_size, generate, and edit from the preceding block.

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
```

- [ ] **Step 4: Run the service tests and the existing StepFun tests**

Run:

```bash
wsl.exe --cd ~ -- bash -lc "cd /mnt/s/Projects/astrbot_plugin_group_chat_plus && python3 -m unittest tests.test_codex_oauth_image_service tests.test_step_image_service -v"
```

Expected: all tests pass and existing StepFun request assertions remain unchanged.

- [ ] **Step 5: Commit Task 1**

```bash
git add utils/codex_oauth_image_service.py tests/test_codex_oauth_image_service.py
git commit -m "feat: add codex oauth image adapter"
```

---

### Task 2: Unified group image service facade

**Files:**
- Create: `utils/group_image_service.py`
- Create: `tests/test_group_image_service.py`

**Interfaces:**
- Consumes: `StepImageService`, Task 1 `CodexOAuthImageService`, and their result and exception types.
- Produces: `GroupImageService`, `GroupImageResult`, `GroupImageUserError`, `GroupImageConfigError`, and `GroupImageProviderError` for `main.py`.

- [ ] **Step 1: Write failing facade tests**

Create `tests/test_group_image_service.py` with service factories injected into the facade:

```python
import asyncio
import unittest
from types import SimpleNamespace

from utils.group_image_service import GroupImageConfigError, GroupImageService


class RecordingBackend:
    def __init__(self, name):
        self.name = name
        self.calls = []

    async def generate(self, **kwargs):
        self.calls.append(("generate", kwargs))
        return SimpleNamespace(
            path="result.png", mode="generate", backend=self.name,
            media_type="image/png", revised_prompt="",
        )

    async def edit(self, **kwargs):
        self.calls.append(("edit", kwargs))
        return SimpleNamespace(
            path="result.png", mode="edit", backend=self.name,
            media_type="image/png", revised_prompt="",
        )


class GroupImageServiceTest(unittest.TestCase):
    def test_old_config_without_backend_uses_stepfun(self):
        stepfun = RecordingBackend("stepfun")
        codex = RecordingBackend("codex_oauth")
        service = GroupImageService(
            context=object(), config={"enable_step_image_tools": True},
            output_dir=None,
            stepfun_factory=lambda **_: stepfun,
            codex_factory=lambda **_: codex,
        )
        asyncio.run(service.generate(prompt="cat", size=""))
        self.assertEqual(stepfun.calls[0][1]["size"], "768x1360")
        self.assertEqual(codex.calls, [])

    def test_explicit_codex_backend_uses_codex_defaults(self):
        stepfun = RecordingBackend("stepfun")
        codex = RecordingBackend("codex_oauth")
        config = {
            "enable_step_image_tools": True,
            "image_tool_backend": "codex_oauth",
            "codex_oauth_image_default_size": "1024x1024",
        }
        service = GroupImageService(
            context=object(), config=config, output_dir=None,
            stepfun_factory=lambda **_: stepfun,
            codex_factory=lambda **_: codex,
        )
        asyncio.run(service.generate(prompt="cat", size=""))
        self.assertEqual(codex.calls[0][1]["size"], "1024x1024")
        self.assertEqual(service.display_name(), "OpenAI Codex 图像生成服务")

    def test_master_switch_and_backend_validation(self):
        self.assertFalse(GroupImageService.is_enabled({"enable_step_image_tools": False}))
        self.assertTrue(GroupImageService.is_enabled({"enable_step_image_tools": "true"}))
        with self.assertRaises(GroupImageConfigError):
            GroupImageService(
                context=object(), config={"image_tool_backend": "unknown"}, output_dir=None
            ).display_name()
```

- [ ] **Step 2: Run and verify failure**

Run:

```bash
wsl.exe --cd ~ -- bash -lc "cd /mnt/s/Projects/astrbot_plugin_group_chat_plus && python3 -m unittest tests.test_group_image_service -v"
```

Expected: import failure because `utils.group_image_service` does not exist.

- [ ] **Step 3: Implement the facade and exception mapping**

Create `utils/group_image_service.py` with:

```python
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
        self, *, context: Any, config: dict, output_dir: Path | None,
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
        return (
            "OpenAI Codex 图像生成服务"
            if self.backend_name() == self.BACKEND_CODEX_OAUTH
            else "阶跃星辰 Step Image Edit 2"
        )
```

Complete `GroupImageService` with these methods:

```python
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
        backend = self._backend()
        try:
            result = await backend.generate(
                prompt=prompt,
                size=str(size or self._default_size()).strip(),
            )
        except (StepImageUserError, CodexOAuthImageUserError) as exc:
            raise GroupImageUserError(str(exc)) from exc
        except (StepImageConfigError, CodexOAuthImageConfigError) as exc:
            raise GroupImageConfigError(str(exc)) from exc
        except (StepImageProviderError, CodexOAuthImageProviderError) as exc:
            raise GroupImageProviderError(str(exc)) from exc
        return self._convert_result(result)

    async def edit(self, *, prompt: str, image_path: str) -> GroupImageResult:
        backend = self._backend()
        try:
            result = await backend.edit(prompt=prompt, image_path=image_path)
        except (StepImageUserError, CodexOAuthImageUserError) as exc:
            raise GroupImageUserError(str(exc)) from exc
        except (StepImageConfigError, CodexOAuthImageConfigError) as exc:
            raise GroupImageConfigError(str(exc)) from exc
        except (StepImageProviderError, CodexOAuthImageProviderError) as exc:
            raise GroupImageProviderError(str(exc)) from exc
        return self._convert_result(result)
```

- [ ] **Step 4: Run facade and backend tests**

Run:

```bash
wsl.exe --cd ~ -- bash -lc "cd /mnt/s/Projects/astrbot_plugin_group_chat_plus && python3 -m unittest tests.test_group_image_service tests.test_codex_oauth_image_service tests.test_step_image_service -v"
```

Expected: all tests pass.

- [ ] **Step 5: Commit Task 2**

```bash
git add utils/group_image_service.py tests/test_group_image_service.py
git commit -m "feat: route group images across providers"
```

---

### Task 3: Main flow and schema integration

**Files:**
- Modify: `main.py:154-160,677-704,5814-5818,8295-8850,10237-10255`
- Modify: `_conf_schema.json:282-355`
- Modify: `tests/test_step_image_tool_integration.py`
- Modify: `tests/test_tool_policy.py`
- Modify: `tests/test_multimodal_history_content.py`

**Interfaces:**
- Consumes: Task 2 `GroupImageService` and `GroupImage*Error` types.
- Produces: backend-aware progress text, tools, ToolPolicy enablement, model-facing result text, safe history summary, and schema settings.

- [ ] **Step 1: Add failing schema and source integration tests**

Update `tests/test_step_image_tool_integration.py` to parse schema JSON and assert:

```python
import json

def test_schema_exposes_configurable_image_backends(self):
    schema = json.loads(self.schema_source)
    self.assertEqual(schema["image_tool_backend"]["default"], "codex_oauth")
    self.assertEqual(schema["image_tool_backend"]["options"], ["codex_oauth", "stepfun"])
    self.assertEqual(
        schema["codex_oauth_image_provider_id"]["default"],
        "openai_oauth/gpt-5.6-sol",
    )
    self.assertEqual(
        schema["codex_oauth_image_provider_id"]["_special"], "select_provider"
    )
    self.assertEqual(schema["codex_oauth_image_model"]["default"], "gpt-5.6-sol")
    self.assertEqual(
        schema["codex_oauth_image_default_size"]["options"],
        ["1024x1024", "1536x1024", "1024x1536"],
    )
    self.assertEqual(schema["codex_oauth_image_timeout"]["default"], 300)

def test_main_routes_existing_tools_through_group_image_service(self):
    self.assertIn("GroupImageService.is_enabled(self.step_image_config)", self.main_source)
    self.assertIn("return GroupImageService(", self.main_source)
    self.assertIn("self._get_step_image_service().display_name()", self.main_source)
    self.assertIn("except GroupImageUserError", self.main_source)
    self.assertIn("except GroupImageConfigError", self.main_source)
    self.assertIn("except GroupImageProviderError", self.main_source)
```

Replace the ToolPolicy assertion in `tests/test_tool_policy.py` with:

```python
self.assertIn(
    "allow_step_image=GroupImageService.is_enabled(self.step_image_config)",
    policy_block,
)
```

Replace the hard-coded progress sample in `tests/test_multimodal_history_content.py` with `正在用 OpenAI Codex 图像生成服务生成图片，稍等一下。`, and retain assertions excluding `[工具调用记录]`, both tool names, `The tool has no return value`, and `[SYSTEM NOTICE]`.

- [ ] **Step 2: Run and verify failure**

Run:

```bash
wsl.exe --cd ~ -- bash -lc "cd /mnt/s/Projects/astrbot_plugin_group_chat_plus && python3 -m unittest tests.test_step_image_tool_integration tests.test_tool_policy tests.test_multimodal_history_content -v"
```

Expected: failures for missing schema keys and missing `GroupImageService` integration.

- [ ] **Step 3: Add schema fields and config loading**

Add these schema entries immediately after `enable_step_image_tools`:

```json
"image_tool_backend": {
  "description": "🖼️ 群聊图片后端",
  "type": "string",
  "options": ["codex_oauth", "stepfun"],
  "hint": "新配置默认使用 Codex OAuth 图像生成；旧配置缺少该字段时继续使用 StepFun。",
  "default": "codex_oauth"
},
"codex_oauth_image_provider_id": {
  "description": "🖼️ Codex OAuth 图片 Provider",
  "type": "string",
  "hint": "选择声明 image_generate 和 image_edit 能力的 Codex OAuth Provider。",
  "default": "openai_oauth/gpt-5.6-sol",
  "_special": "select_provider"
},
"codex_oauth_image_model": {
  "description": "🖼️ Codex OAuth 主模型",
  "type": "string",
  "default": "gpt-5.6-sol"
},
"codex_oauth_image_default_size": {
  "description": "🖼️ Codex OAuth 默认尺寸",
  "type": "string",
  "options": ["1024x1024", "1536x1024", "1024x1536"],
  "default": "1024x1024"
},
"codex_oauth_image_timeout": {
  "description": "🖼️ Codex OAuth 调用超时（秒）",
  "type": "int",
  "hint": "允许 30 至 900 秒，默认 300 秒。",
  "default": 300
}
```

Add these exact entries to `self.step_image_config` in `main.py`; `image_tool_backend` intentionally has no Python default so old persisted configs retain StepFun behavior:

```python
"image_tool_backend": config.get("image_tool_backend"),
"codex_oauth_image_provider_id": config.get(
    "codex_oauth_image_provider_id", "openai_oauth/gpt-5.6-sol"
),
"codex_oauth_image_model": config.get(
    "codex_oauth_image_model", "gpt-5.6-sol"
),
"codex_oauth_image_default_size": config.get(
    "codex_oauth_image_default_size", "1024x1024"
),
"codex_oauth_image_timeout": config.get("codex_oauth_image_timeout", 300),
```

- [ ] **Step 4: Route main flow through the facade**

Replace StepImage imports with:

```python
from .utils.group_image_service import (
    GroupImageConfigError,
    GroupImageProviderError,
    GroupImageService,
    GroupImageUserError,
)
```

Keep `DEFAULT_GENERATION_SIZE` only where legacy StepFun compatibility still needs it. Replace `_get_step_image_service()` with:

```python
    def _get_step_image_service(self) -> GroupImageService:
        return GroupImageService(
            context=self.context,
            config=self.step_image_config,
            output_dir=self.step_image_output_dir,
        )
```

Change ToolPolicy and guard checks from `StepImageService.is_enabled(...)` to `GroupImageService.is_enabled(...)`. Replace progress construction and generation size selection with:

```python
    def _build_step_image_progress_text(self, action: Optional[str] = None) -> str:
        backend_name = self._get_step_image_service().display_name()
        verb = "编辑这张图" if action == "edit" else "生成图片"
        return f"正在用{backend_name}{verb}，稍等一下。"

    # In gcp_step_image_generate:
    result = await self._get_step_image_service().generate(
        prompt=prompt,
        size=str(size or "").strip(),
    )
```

Catch `GroupImageUserError`, `GroupImageConfigError`, and `GroupImageProviderError` in both tools in the same branches currently used for StepImage exceptions.

Change model-facing and directive text from fixed “Step Image Edit 2” wording to “群聊图片工具” while preserving these requirements:

```text
先提交工具参数并等待工具结果。
成功时图片由工具发送一次。
根据工具结果和当前人格输出一句自然语言回复。
禁止输出工具协议、参数、Provider ID、文件路径、API 细节或内部状态。
```

- [ ] **Step 5: Run the integration test set**

Run:

```bash
wsl.exe --cd ~ -- bash -lc "cd /mnt/s/Projects/astrbot_plugin_group_chat_plus && python3 -m unittest tests.test_step_image_tool_integration tests.test_tool_policy tests.test_multimodal_history_content tests.test_tool_call_leakage_guard tests.test_tool_passthrough -v"
```

Expected: all tests pass, existing tool names remain registered, and schema JSON assertions pass.

- [ ] **Step 6: Commit Task 3**

```bash
git add main.py _conf_schema.json tests/test_step_image_tool_integration.py tests/test_tool_policy.py tests/test_multimodal_history_content.py
git commit -m "feat: configure group image backend"
```

---

### Task 4: User and maintainer documentation

**Files:**
- Modify: `README.md`
- Modify: `docs/CONFIG_REFERENCE.md`
- Modify: `docs/PROJECT_STRUCTURE.md`
- Modify: `docs/MESSAGE_WORKFLOW.md`
- Modify: `CHANGELOG.md`
- Modify: `metadata.yaml`
- Modify: `requirements.txt`
- Modify: `tests/test_group_only_boundary.py`

**Interfaces:**
- Consumes: final schema names and runtime behavior from Tasks 1 through 3.
- Produces: installation, configuration, architecture, message sequence, release note, and metadata descriptions matching runtime behavior.

- [ ] **Step 1: Add failing documentation contract assertions**

Update `tests/test_group_only_boundary.py` to assert that README contains all of:

```python
for marker in (
    "image_tool_backend",
    "codex_oauth",
    "openai_oauth/gpt-5.6-sol",
    "StepFun",
    "generate_image()",
):
    self.assertIn(marker, self.readme_source)
```

Add source assertions that `metadata.yaml` describes configurable Codex OAuth and StepFun image generation, and that `requirements.txt` describes `httpx` as the StepFun HTTP dependency rather than the Codex OAuth transport.

- [ ] **Step 2: Run and verify failure**

Run:

```bash
wsl.exe --cd ~ -- bash -lc "cd /mnt/s/Projects/astrbot_plugin_group_chat_plus && python3 -m unittest tests.test_group_only_boundary -v"
```

Expected: failures for missing backend configuration documentation.

- [ ] **Step 3: Update documentation with exact behavior**

Document these points in README and configuration reference:

```text
- New installs default to codex_oauth.
- Existing config files without image_tool_backend keep using stepfun until saved or migrated.
- Codex OAuth settings store only Provider ID, Codex main model, size, and timeout.
- The Provider handles OAuth credentials and the image_generation request.
- Codex sizes use width x height; StepFun retains height x width.
- StepFun remains available by setting image_tool_backend=stepfun.
- The internal LLM tool names remain gcp_step_image_generate and gcp_step_image_edit.
```

Update project structure to list `utils/codex_oauth_image_service.py` and `utils/group_image_service.py`. Update message documentation to show backend-aware progress, one image result, and a final personality-aware natural-language reply. Add a changelog entry and update metadata help text. No dependency is added; `httpx` remains for StepFun only.

- [ ] **Step 4: Run documentation and schema checks**

Run:

```bash
wsl.exe --cd ~ -- bash -lc "cd /mnt/s/Projects/astrbot_plugin_group_chat_plus && python3 -m unittest tests.test_group_only_boundary tests.test_step_image_tool_integration -v && python3 -m json.tool _conf_schema.json >/dev/null"
```

Expected: all tests pass and schema parsing exits zero.

- [ ] **Step 5: Commit Task 4**

```bash
git add README.md docs/CONFIG_REFERENCE.md docs/PROJECT_STRUCTURE.md docs/MESSAGE_WORKFLOW.md CHANGELOG.md metadata.yaml requirements.txt tests/test_group_only_boundary.py
git commit -m "docs: describe configurable image providers"
```

---

### Task 5: Full local verification and production release

**Files:**
- Modify only if verification finds a scoped defect in Tasks 1 through 4.
- Production sync: only files changed by Tasks 1 through 4.
- Production config: `/volume1/docker/astrbot/data/config/astrbot_plugin_group_chat_plus_config.json` through Dashboard API.

**Interfaces:**
- Consumes: completed local implementation and production Dashboard API.
- Produces: test evidence, scoped plugin reload, Codex generate/edit smoke results, StepFun fallback smoke result, and final production Codex configuration.

- [ ] **Step 1: Run complete local verification**

Run:

```bash
wsl.exe --cd ~ -- bash -lc "cd /mnt/s/Projects/astrbot_plugin_group_chat_plus && mkdir -p .tmp/pycache .tmp/tmp && PYTHONPYCACHEPREFIX=.tmp/pycache TMPDIR=.tmp/tmp python3 -m unittest discover -s tests -v && PYTHONPYCACHEPREFIX=.tmp/pycache TMPDIR=.tmp/tmp python3 -m json.tool _conf_schema.json >/dev/null && PYTHONPYCACHEPREFIX=.tmp/pycache TMPDIR=.tmp/tmp python3 -m py_compile main.py utils/step_image_service.py utils/codex_oauth_image_service.py utils/group_image_service.py && git diff --check"
```

Expected: zero test failures, valid JSON, successful compilation, and no whitespace errors.

- [ ] **Step 2: Inspect release scope**

Run:

```bash
git status --short
git diff HEAD~4 --stat
git diff HEAD~4 --name-only
```

Expected: only planned Group Chat Plus files plus the unrelated pre-existing untracked Matoi plan. Confirm `metadata.yaml` name remains `astrbot_plugin_group_chat_plus`.

- [ ] **Step 3: Back up and sync only changed files**

Create a timestamped tar backup under:

```text
/volume1/docker/astrbot/data/plugin_data/astrbot_plugin_group_chat_plus/backups/
```

Synchronize only the implementation, tests, schema, and documentation files changed by this plan into:

```text
/volume1/docker/astrbot/data/plugins/astrbot_plugin_group_chat_plus
```

Exclude `.git`, `.tmp`, `__pycache__`, `*.pyc`, and the unrelated Matoi plan.

- [ ] **Step 4: Compile production files and update configuration safely**

Inside the `astrbot` container, call `compile()` on `main.py`, `utils/step_image_service.py`, `utils/codex_oauth_image_service.py`, and `utils/group_image_service.py` without writing bytecode.

Use Dashboard authentication in memory, then update only these plugin settings while preserving every other field:

```json
{
  "enable_step_image_tools": true,
  "image_tool_backend": "codex_oauth",
  "codex_oauth_image_provider_id": "openai_oauth/gpt-5.6-sol",
  "codex_oauth_image_model": "gpt-5.6-sol",
  "codex_oauth_image_default_size": "1024x1024",
  "codex_oauth_image_timeout": 300
}
```

Never print the complete config or Dashboard JWT.

- [ ] **Step 5: Reload only Group Chat Plus and verify runtime state**

Call:

```http
POST /api/plugin/reload
{"name":"astrbot_plugin_group_chat_plus"}
```

Verify the response is successful, `/api/plugin/get` reports the plugin enabled, `/api/commands` still contains `gcp_clear_image_cache`, `gcp_reset`, and `gcp_reset_here`, and the reload window contains one target termination, one target load, zero target `Traceback`, and zero target `ERROR`.

- [ ] **Step 6: Run real backend smoke tests**

Use the production plugin service without printing Provider configuration or prompts beyond a neutral minimal test description.

Record only:

```text
codex_generate: status, dimensions, bytes
codex_edit: status, dimensions, bytes
stepfun_generate: status, dimensions, bytes
```

For StepFun validation, temporarily switch `image_tool_backend` to `stepfun`, reload only Group Chat Plus, execute one minimal generation, then restore `codex_oauth` and reload the target plugin again. Delete smoke-test input and output artifacts created specifically for verification after recording dimensions and byte counts.

- [ ] **Step 7: Final production audit and push**

Confirm the final persisted settings select `codex_oauth`, production logs after the final reload contain no target plugin error, and the three expected plugin commands remain registered. Run `git status --short`, commit any scoped verification fix with its tests, then push `main` to `origin` using `git push origin main`.
