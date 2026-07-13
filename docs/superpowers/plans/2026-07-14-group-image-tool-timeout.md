# Group Chat Plus 图片工具独立超时 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 `gcp_step_image_generate` 与 `gcp_step_image_edit` 使用当前图片后端配置的工具执行超时，线上 Codex OAuth 为 300 秒，同时保留其他工具的 AstrBot 全局 180 秒设置。

**Architecture:** 新增一个只依赖标准库的工具执行器包装模块，通过工具名为两个 GCP 图片工具传入专用 `tool_call_timeout`。插件在 `initialize()` 注册句柄，在 `terminate()` 撤销句柄；多实例注册共用一个包装器，与已经存在的 Matoi 包装器按类方法描述符叠加。

**Tech Stack:** Python 3.12、`asyncio` 异步生成器、`unittest`、AstrBot `FunctionToolExecutor`、Dashboard API 单插件重载。

## Global Constraints

1. 只覆盖 `gcp_step_image_generate` 与 `gcp_step_image_edit`。
2. `image_tool_backend=codex_oauth` 时使用 `codex_oauth_image_timeout`，线上值为 300 秒。
3. `image_tool_backend=stepfun` 时使用 `step_image_timeout`。
4. 其他工具继续使用 `provider_settings.tool_call_timeout`，线上值为 180 秒。
5. 图片工具已经收到更长的显式超时时保留较长值；180 秒会提升为后端配置的 300 秒。
6. 不修改 AstrBot Core、Matoi、Pixiv、MCP、Skills、搜索或计算机控制工具。
7. 安装和撤销失败只记录固定操作码与错误类型，不输出 Provider 配置或凭据。
8. WSL 命令必须以 `wsl.exe --cd ~ --` 开始。
9. 生产环境只执行目标插件文件同步和 `POST /api/plugin/reload`，不重启容器。

## File Map

1. Create `utils/tool_timeout_override.py`: 后端超时解析、GCP 图片工具名匹配、执行器包装注册与撤销。
2. Create `tests/test_group_image_tool_timeout.py`: 纯标准库行为测试，不导入 AstrBot。
3. Modify `main.py`: 保存注册句柄，在插件加载与卸载阶段安装和撤销覆盖。
4. Modify `tests/test_step_image_tool_integration.py`: 检查生命周期接入、固定日志操作码与工具名范围。

---

### Task 1: 图片工具超时覆盖模块

**Files:**
- Create: `utils/tool_timeout_override.py`
- Create: `tests/test_group_image_tool_timeout.py`

**Interfaces:**
- Consumes: AstrBot 执行器类的 `@classmethod async def _execute_local(..., tool_call_timeout=None, **tool_args)`。
- Produces: `resolve_group_image_tool_timeout(config) -> int | float`、`install_group_image_tool_timeout_override(timeout_seconds, executor_cls=None) -> ToolTimeoutOverrideHandle`、`remove_group_image_tool_timeout_override(handle) -> None`。

- [ ] **Step 1: Write the failing behavior tests**

Create `tests/test_group_image_tool_timeout.py` with a path-based module loader, a fresh fake executor per test, and these assertions:

```python
import asyncio
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "utils" / "tool_timeout_override.py"


def load_module(module_name="gcp_tool_timeout_override_test_module"):
    if not MODULE_PATH.is_file():
        raise AssertionError("utils/tool_timeout_override.py is missing")
    spec = importlib.util.spec_from_file_location(
        module_name, MODULE_PATH
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def make_executor():
    class FakeExecutor:
        @classmethod
        async def _execute_local(
            cls,
            tool,
            run_context,
            *,
            tool_call_timeout=None,
            **tool_args,
        ):
            yield (
                tool_call_timeout
                if tool_call_timeout is not None
                else run_context.tool_call_timeout
            )

    return FakeExecutor


async def collect_timeout(
    executor,
    tool_name,
    context_timeout=180,
    explicit_timeout=None,
):
    values = []
    async for value in executor._execute_local(
        SimpleNamespace(name=tool_name),
        SimpleNamespace(tool_call_timeout=context_timeout),
        tool_call_timeout=explicit_timeout,
    ):
        values.append(value)
    return values


class GroupImageToolTimeoutTest(unittest.TestCase):
    def test_group_image_tools_receive_registered_timeout(self):
        module = load_module()
        executor = make_executor()
        handle = module.install_group_image_tool_timeout_override(300, executor)
        try:
            for tool_name in (
                "gcp_step_image_generate",
                "gcp_step_image_edit",
            ):
                self.assertEqual(
                    asyncio.run(collect_timeout(executor, tool_name)),
                    [300],
                )
        finally:
            module.remove_group_image_tool_timeout_override(handle)

    def test_unrelated_tool_keeps_context_timeout(self):
        module = load_module()
        executor = make_executor()
        handle = module.install_group_image_tool_timeout_override(300, executor)
        try:
            self.assertEqual(
                asyncio.run(collect_timeout(executor, "web_search")),
                [180],
            )
        finally:
            module.remove_group_image_tool_timeout_override(handle)

    def test_longer_explicit_timeout_is_preserved(self):
        module = load_module()
        executor = make_executor()
        handle = module.install_group_image_tool_timeout_override(300, executor)
        try:
            self.assertEqual(
                asyncio.run(
                    collect_timeout(
                        executor,
                        "gcp_step_image_generate",
                        explicit_timeout=420,
                    )
                ),
                [420],
            )
        finally:
            module.remove_group_image_tool_timeout_override(handle)

    def test_multiple_registrations_use_largest_timeout_and_restore(self):
        module = load_module()
        executor = make_executor()
        original = executor.__dict__["_execute_local"]
        first = module.install_group_image_tool_timeout_override(240, executor)
        second = module.install_group_image_tool_timeout_override(300, executor)
        self.assertEqual(
            asyncio.run(collect_timeout(executor, "gcp_step_image_generate")),
            [300],
        )
        module.remove_group_image_tool_timeout_override(second)
        self.assertEqual(
            asyncio.run(collect_timeout(executor, "gcp_step_image_generate")),
            [240],
        )
        module.remove_group_image_tool_timeout_override(first)
        self.assertIs(executor.__dict__["_execute_local"], original)

    def test_removal_restores_preexisting_wrapper(self):
        module = load_module()
        executor = make_executor()
        original = executor.__dict__["_execute_local"]

        async def existing_wrapper(
            cls,
            tool,
            run_context,
            *,
            tool_call_timeout=None,
            **tool_args,
        ):
            original_method = original.__get__(None, cls)
            async for value in original_method(
                tool,
                run_context,
                tool_call_timeout=tool_call_timeout,
                **tool_args,
            ):
                yield value

        existing_descriptor = classmethod(existing_wrapper)
        setattr(executor, "_execute_local", existing_descriptor)
        handle = module.install_group_image_tool_timeout_override(300, executor)
        module.remove_group_image_tool_timeout_override(handle)
        self.assertIs(executor.__dict__["_execute_local"], existing_descriptor)

    def test_later_wrapper_can_be_removed_before_or_after_override(self):
        module = load_module()
        executor = make_executor()
        original = executor.__dict__["_execute_local"]
        handle = module.install_group_image_tool_timeout_override(300, executor)
        timeout_descriptor = executor.__dict__["_execute_local"]

        async def later_wrapper(
            cls,
            tool,
            run_context,
            *,
            tool_call_timeout=None,
            **tool_args,
        ):
            wrapped_method = timeout_descriptor.__get__(None, cls)
            async for value in wrapped_method(
                tool,
                run_context,
                tool_call_timeout=tool_call_timeout,
                **tool_args,
            ):
                yield value

        later_descriptor = classmethod(later_wrapper)
        setattr(executor, "_execute_local", later_descriptor)
        module.remove_group_image_tool_timeout_override(handle)
        self.assertIs(executor.__dict__["_execute_local"], later_descriptor)
        self.assertEqual(
            asyncio.run(collect_timeout(executor, "gcp_step_image_generate")),
            [180],
        )

        setattr(executor, "_execute_local", timeout_descriptor)
        self.assertEqual(
            asyncio.run(collect_timeout(executor, "gcp_step_image_generate")),
            [180],
        )
        self.assertIs(executor.__dict__["_execute_local"], original)

        second_handle = module.install_group_image_tool_timeout_override(
            300, executor
        )
        second_timeout_descriptor = executor.__dict__["_execute_local"]
        setattr(executor, "_execute_local", later_descriptor)
        setattr(executor, "_execute_local", second_timeout_descriptor)
        module.remove_group_image_tool_timeout_override(second_handle)
        self.assertIs(executor.__dict__["_execute_local"], original)

    def test_separate_module_instances_share_hot_reload_state(self):
        first_module = load_module("gcp_timeout_module_before_reload")
        second_module = load_module("gcp_timeout_module_after_reload")
        executor = make_executor()
        original = executor.__dict__["_execute_local"]
        first = first_module.install_group_image_tool_timeout_override(
            240, executor
        )
        second = second_module.install_group_image_tool_timeout_override(
            300, executor
        )
        self.assertEqual(
            asyncio.run(collect_timeout(executor, "gcp_step_image_generate")),
            [300],
        )
        first_module.remove_group_image_tool_timeout_override(first)
        self.assertEqual(
            asyncio.run(collect_timeout(executor, "gcp_step_image_generate")),
            [300],
        )
        second_module.remove_group_image_tool_timeout_override(second)
        self.assertIs(executor.__dict__["_execute_local"], original)

    def test_backend_timeout_resolution(self):
        module = load_module()
        self.assertEqual(
            module.resolve_group_image_tool_timeout(
                {
                    "image_tool_backend": "codex_oauth",
                    "codex_oauth_image_timeout": 300,
                }
            ),
            300,
        )
        self.assertEqual(
            module.resolve_group_image_tool_timeout(
                {
                    "image_tool_backend": "stepfun",
                    "step_image_timeout": 60,
                }
            ),
            60,
        )
        for config in (
            {"image_tool_backend": "unknown"},
            {
                "image_tool_backend": "codex_oauth",
                "codex_oauth_image_timeout": 0,
            },
            {
                "image_tool_backend": "codex_oauth",
                "codex_oauth_image_timeout": float("nan"),
            },
        ):
            with self.subTest(config=config):
                with self.assertRaises(ValueError):
                    module.resolve_group_image_tool_timeout(config)

    def test_default_executor_resolution_prefers_current_core_class(self):
        module = load_module()
        executor = make_executor()
        imported_module = SimpleNamespace(FunctionToolExecutor=executor)
        with patch.object(
            module.importlib,
            "import_module",
            return_value=imported_module,
        ):
            self.assertIs(module._default_executor_cls(), executor)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the focused test and verify RED**

Run:

```powershell
wsl.exe --cd ~ -- bash -lc "cd /mnt/s/Projects/astrbot_plugin_group_chat_plus && mkdir -p .tmp/pycache .tmp/tmp && PYTHONPYCACHEPREFIX=.tmp/pycache TMPDIR=.tmp/tmp python3 -m unittest tests.test_group_image_tool_timeout -v"
```

Expected: FAIL because `utils/tool_timeout_override.py` is missing.

- [ ] **Step 3: Implement the minimal timeout module**

Create `utils/tool_timeout_override.py` with these elements:

```python
"""Scoped AstrBot tool timeout override for Group Chat Plus image tools."""

from dataclasses import dataclass
import importlib
import math
from threading import RLock
from typing import Any, Mapping


GROUP_IMAGE_TOOL_NAMES = frozenset(
    {"gcp_step_image_generate", "gcp_step_image_edit"}
)
_STATE_ATTR = "_gcp_image_tool_timeout_override_state"
_LOCK_ATTR = "_gcp_image_tool_timeout_override_lock"


@dataclass(frozen=True)
class ToolTimeoutOverrideHandle:
    executor_cls: type
    token: object


def resolve_group_image_tool_timeout(config: Mapping[str, Any]) -> int | float:
    backend = str(config.get("image_tool_backend") or "").strip().lower()
    if backend == "codex_oauth":
        raw_timeout = config.get("codex_oauth_image_timeout")
    elif backend == "stepfun":
        raw_timeout = config.get("step_image_timeout")
    else:
        raise ValueError("unsupported image tool backend")
    timeout = float(raw_timeout)
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("image tool timeout must be a finite positive number")
    return int(timeout) if timeout.is_integer() else timeout


def _default_executor_cls() -> type:
    module = importlib.import_module("astrbot.core.astr_agent_tool_exec")
    executor_cls = getattr(module, "FunctionToolExecutor", None)
    if executor_cls is None:
        executor_cls = getattr(module, "AstrAgentToolExec", None)
    if executor_cls is None:
        raise ImportError("AstrBot function tool executor is unavailable")
    return executor_cls


def install_group_image_tool_timeout_override(
    timeout_seconds: Any,
    executor_cls: type | None = None,
) -> ToolTimeoutOverrideHandle:
    target_cls = executor_cls or _default_executor_cls()
    timeout = float(timeout_seconds)
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout_seconds must be a finite positive number")
    normalized_timeout = int(timeout) if timeout.is_integer() else timeout
    token = object()

    lock = target_cls.__dict__.get(_LOCK_ATTR)
    if lock is None:
        lock = RLock()
        setattr(target_cls, _LOCK_ATTR, lock)

    with lock:
        state = target_cls.__dict__.get(_STATE_ATTR)
        if state is None:
            original_descriptor = target_cls.__dict__.get("_execute_local")
            if not isinstance(original_descriptor, classmethod):
                raise TypeError("executor _execute_local must be a classmethod")
            state = {
                "original_descriptor": original_descriptor,
                "wrapper_descriptor": None,
                "timeouts": {},
                "lock": lock,
            }

            async def execute_local_with_group_image_timeout(
                cls,
                tool,
                run_context,
                *,
                tool_call_timeout=None,
                **tool_args,
            ):
                registered_timeout = None
                current_state = cls.__dict__.get(_STATE_ATTR)
                if isinstance(current_state, dict):
                    state_lock = current_state["lock"]
                    with state_lock:
                        if getattr(tool, "name", None) in GROUP_IMAGE_TOOL_NAMES:
                            registered_timeout = max(
                                current_state["timeouts"].values(),
                                default=None,
                            )
                        if (
                            registered_timeout is None
                            and not current_state["timeouts"]
                            and cls.__dict__.get("_execute_local")
                            is current_state["wrapper_descriptor"]
                        ):
                            setattr(
                                cls,
                                "_execute_local",
                                current_state["original_descriptor"],
                            )
                            delattr(cls, _STATE_ATTR)
                            if cls.__dict__.get(_LOCK_ATTR) is state_lock:
                                delattr(cls, _LOCK_ATTR)

                effective_timeout = tool_call_timeout
                if registered_timeout is not None:
                    if effective_timeout is None:
                        effective_timeout = registered_timeout
                    else:
                        try:
                            effective_timeout = max(
                                effective_timeout,
                                registered_timeout,
                            )
                        except TypeError:
                            effective_timeout = registered_timeout
                original_method = original_descriptor.__get__(None, cls)
                async for result in original_method(
                    tool,
                    run_context,
                    tool_call_timeout=effective_timeout,
                    **tool_args,
                ):
                    yield result

            wrapper_descriptor = classmethod(
                execute_local_with_group_image_timeout
            )
            state["wrapper_descriptor"] = wrapper_descriptor
            setattr(target_cls, _STATE_ATTR, state)
            setattr(target_cls, "_execute_local", wrapper_descriptor)
        elif not isinstance(state, dict) or not {
            "original_descriptor",
            "wrapper_descriptor",
            "timeouts",
            "lock",
        }.issubset(state):
            raise TypeError("executor timeout override state is invalid")

        state["timeouts"][token] = normalized_timeout

    return ToolTimeoutOverrideHandle(target_cls, token)


def remove_group_image_tool_timeout_override(
    handle: ToolTimeoutOverrideHandle,
) -> None:
    lock = handle.executor_cls.__dict__.get(_LOCK_ATTR)
    if lock is None:
        return
    with lock:
        state = handle.executor_cls.__dict__.get(_STATE_ATTR)
        if not isinstance(state, dict):
            return
        state["timeouts"].pop(handle.token, None)
        if state["timeouts"]:
            return
        if (
            handle.executor_cls.__dict__.get("_execute_local")
            is state["wrapper_descriptor"]
        ):
            setattr(
                handle.executor_cls,
                "_execute_local",
                state["original_descriptor"],
            )
            delattr(handle.executor_cls, _STATE_ATTR)
            if handle.executor_cls.__dict__.get(_LOCK_ATTR) is lock:
                delattr(handle.executor_cls, _LOCK_ATTR)
```

- [ ] **Step 4: Run focused tests and verify GREEN**

Run the Step 2 command again.

Expected: all tests pass.

- [ ] **Step 5: Commit the isolated module**

```powershell
git add -- utils/tool_timeout_override.py tests/test_group_image_tool_timeout.py
git commit -m "feat: scope image tool timeout"
```

---

### Task 2: 插件生命周期接入

**Files:**
- Modify: `main.py:115-150`
- Modify: `main.py:274-290`
- Modify: `main.py:2539-2586`
- Modify: `tests/test_step_image_tool_integration.py`

**Interfaces:**
- Consumes: Task 1 的三个公共函数与 `ToolTimeoutOverrideHandle`。
- Produces: 加载日志 `GCP_IMAGE_TOOL_TIMEOUT_OVERRIDE_INSTALLED timeout=<value>`，失败日志 `GCP_IMAGE_TOOL_TIMEOUT_OVERRIDE_INSTALL_FAILED error_type=<type>`，撤销失败日志 `GCP_IMAGE_TOOL_TIMEOUT_OVERRIDE_REMOVE_FAILED error_type=<type>`。

- [ ] **Step 1: Write the failing lifecycle integration test**

Add this test to `StepImageToolIntegrationTest` in `tests/test_step_image_tool_integration.py`:

```python
def test_group_image_tool_timeout_override_is_lifecycle_scoped(self):
    initialize_source = self._method_source("initialize")
    terminate_source = self._method_source("terminate")
    self.assertIn("resolve_group_image_tool_timeout", initialize_source)
    self.assertIn(
        "install_group_image_tool_timeout_override", initialize_source
    )
    self.assertIn(
        "GCP_IMAGE_TOOL_TIMEOUT_OVERRIDE_INSTALLED", initialize_source
    )
    self.assertIn(
        "remove_group_image_tool_timeout_override", terminate_source
    )
    self.assertIn(
        "GCP_IMAGE_TOOL_TIMEOUT_OVERRIDE_REMOVE_FAILED", terminate_source
    )
```

- [ ] **Step 2: Run the integration test and verify RED**

```powershell
wsl.exe --cd ~ -- bash -lc "cd /mnt/s/Projects/astrbot_plugin_group_chat_plus && mkdir -p .tmp/pycache .tmp/tmp && PYTHONPYCACHEPREFIX=.tmp/pycache TMPDIR=.tmp/tmp python3 -m unittest tests.test_step_image_tool_integration.StepImageToolIntegrationTest.test_group_image_tool_timeout_override_is_lifecycle_scoped -v"
```

Expected: FAIL because `initialize()` and `terminate()` have no timeout override calls.

- [ ] **Step 3: Add imports and instance state**

Import the Task 1 helpers near the other utility imports:

```python
from .utils.tool_timeout_override import (
    install_group_image_tool_timeout_override,
    remove_group_image_tool_timeout_override,
    resolve_group_image_tool_timeout,
)
```

Initialize the handle after `self.step_image_config` is built:

```python
self._group_image_tool_timeout_override_handle = None
```

- [ ] **Step 4: Install the override during initialize**

Add this block near the start of `initialize()` before background tasks:

```python
if (
    self.enable_group_chat
    and GroupImageService.is_enabled(self.step_image_config)
    and self._group_image_tool_timeout_override_handle is None
):
    try:
        image_tool_timeout = resolve_group_image_tool_timeout(
            self.step_image_config
        )
        self._group_image_tool_timeout_override_handle = (
            install_group_image_tool_timeout_override(image_tool_timeout)
        )
        logger.info(
            "GCP_IMAGE_TOOL_TIMEOUT_OVERRIDE_INSTALLED timeout=%s",
            image_tool_timeout,
        )
    except Exception as exc:
        logger.warning(
            "GCP_IMAGE_TOOL_TIMEOUT_OVERRIDE_INSTALL_FAILED error_type=%s",
            exc.__class__.__name__,
        )
```

- [ ] **Step 5: Remove the override during terminate**

Add this block at the start of `terminate()`:

```python
timeout_override_handle = getattr(
    self, "_group_image_tool_timeout_override_handle", None
)
if timeout_override_handle is not None:
    try:
        remove_group_image_tool_timeout_override(timeout_override_handle)
    except Exception as exc:
        logger.warning(
            "GCP_IMAGE_TOOL_TIMEOUT_OVERRIDE_REMOVE_FAILED error_type=%s",
            exc.__class__.__name__,
        )
    finally:
        self._group_image_tool_timeout_override_handle = None
```

- [ ] **Step 6: Run focused lifecycle and module tests**

```powershell
wsl.exe --cd ~ -- bash -lc "cd /mnt/s/Projects/astrbot_plugin_group_chat_plus && mkdir -p .tmp/pycache .tmp/tmp && PYTHONPYCACHEPREFIX=.tmp/pycache TMPDIR=.tmp/tmp python3 -m unittest tests.test_group_image_tool_timeout tests.test_step_image_tool_integration -v"
```

Expected: all focused tests pass.

- [ ] **Step 7: Commit lifecycle integration**

```powershell
git add -- main.py tests/test_step_image_tool_integration.py
git commit -m "fix: extend group image tool timeout"
```

---

### Task 3: 本地回归验证

**Files:**
- Verify only; no planned source edits.

**Interfaces:**
- Consumes: Tasks 1 and 2 commits.
- Produces: fresh test, JSON, compile and whitespace evidence.

- [ ] **Step 1: Run the full test suite**

```powershell
wsl.exe --cd ~ -- bash -lc "cd /mnt/s/Projects/astrbot_plugin_group_chat_plus && mkdir -p .tmp/pycache .tmp/tmp && PYTHONPYCACHEPREFIX=.tmp/pycache TMPDIR=.tmp/tmp python3 -m unittest discover -s tests -v"
```

Expected: zero failures and zero errors.

- [ ] **Step 2: Validate schema and compile changed Python files**

```powershell
wsl.exe --cd ~ -- bash -lc "cd /mnt/s/Projects/astrbot_plugin_group_chat_plus && python3 -m json.tool _conf_schema.json >/dev/null && PYTHONPYCACHEPREFIX=.tmp/pycache python3 -m py_compile main.py utils/tool_timeout_override.py tests/test_group_image_tool_timeout.py tests/test_step_image_tool_integration.py"
```

Expected: exit code 0.

- [ ] **Step 3: Check the final diff**

```powershell
git status --short
git diff --check HEAD~2..HEAD
git diff --stat HEAD~2..HEAD
```

Expected: only the intended implementation and test files plus the separately committed design and plan documents; the unrelated untracked Matoi plan remains untouched.

---

### Task 4: 生产同步与验证

**Files:**
- Sync: `main.py`
- Sync: `utils/tool_timeout_override.py`

**Interfaces:**
- Consumes: locally verified implementation.
- Produces: target plugin reload with `GCP_IMAGE_TOOL_TIMEOUT_OVERRIDE_INSTALLED timeout=300` and unchanged global `provider_settings.tool_call_timeout=180`.

- [ ] **Step 1: Create a timestamped remote backup**

Create a tar backup under `/volume1/docker/astrbot/data/plugin_data/astrbot_plugin_group_chat_plus/backups/` containing the deployed `main.py` and `utils` directory. Print only the backup path and archive size.

- [ ] **Step 2: Sync only the two production files**

```powershell
wsl.exe --cd ~ -- bash -lc "cd /mnt/s/Projects/astrbot_plugin_group_chat_plus && tar -cf - main.py utils/tool_timeout_override.py | ssh -o UserKnownHostsFile=/dev/null -o StrictHostKeyChecking=no -p 44012 wty1996@192.168.1.17 'tar -xf - -C /volume1/docker/astrbot/data/plugins/astrbot_plugin_group_chat_plus'"
```

Expected: exit code 0.

- [ ] **Step 3: Compile deployed files inside the AstrBot container**

Compile `/AstrBot/data/plugins/astrbot_plugin_group_chat_plus/main.py` and `/AstrBot/data/plugins/astrbot_plugin_group_chat_plus/utils/tool_timeout_override.py` with `compile(..., "exec")` so no production `__pycache__` is created.

Expected: exit code 0.

- [ ] **Step 4: Reload only Group Chat Plus**

Generate a short-lived Dashboard JWT inside the container and call:

```http
POST http://127.0.0.1:6185/api/plugin/reload
Content-Type: application/json

{"name":"astrbot_plugin_group_chat_plus"}
```

Expected: Dashboard response status `ok`.

- [ ] **Step 5: Verify runtime state**

Check the reload-period log for exactly one target unload and load, no target traceback, and this line:

```text
GCP_IMAGE_TOOL_TIMEOUT_OVERRIDE_INSTALLED timeout=300
```

Read `cmd_config.json` without printing credentials and confirm:

```text
provider_settings.tool_call_timeout=180
```

Call `/api/plugin/get?name=astrbot_plugin_group_chat_plus` and `/api/commands`; confirm the plugin is active and its existing commands remain registered.

- [ ] **Step 6: Run a minimal real image smoke test**

Read only the configured OAuth credential expiry timestamp first. When the credential remains valid for at least 30 minutes, instantiate the configured `openai_oauth/gpt-5.6-sol` Provider and `CodexOAuthImageService` inside a temporary container process, then call `generate(prompt="一枚简洁的蓝色圆形图标，纯色背景，无文字", size="1024x1024")`. Open the returned file with Pillow, report only status, dimensions, byte count and duration, and delete the returned file in a `finally` block. When the credential expires within 30 minutes, report `status=skipped_expiring_credential` without starting the request. No token, Provider config, proxy credential or Dashboard JWT may be printed.

Expected: image generation succeeds, or a genuine backend error is reported without `execution timeout after 180 seconds`.

---

### Task 5: Publish the self-maintained branch

**Files:**
- Git metadata only.

**Interfaces:**
- Consumes: verified local commits and production evidence.
- Produces: `origin/main` containing the design, plan, implementation and tests.

- [ ] **Step 1: Review commit scope**

```powershell
git status --short
git log --oneline --decorate -5
```

Expected: only the unrelated untracked Matoi plan remains outside commits.

- [ ] **Step 2: Push main**

```powershell
git push origin main
```

Expected: push succeeds and local `main` matches `origin/main`.
