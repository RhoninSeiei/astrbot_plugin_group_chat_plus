# Group Image Backend-Specific Prompt Limits Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 Codex OAuth 图片提示词支持最多 2048 字符，同时让 StepFun 继续使用 512 字符上限，并使工具说明与运行时校验一致。

**Architecture:** 后端适配器继续保存各自的字符上限与最终校验。`GroupImageService` 增加当前后端上限查询和调用前校验，两个 LLM 工具在参数说明中明确列出两种后端的上限。

**Tech Stack:** Python 3、`asyncio`、AstrBot LLM Tool、标准库 `unittest`。

## Global Constraints

Codex OAuth 图片提示词上限固定为 2048 字符。

StepFun Step Image Edit 2 图片提示词上限保持 512 字符。

Provider、OAuth 配置、图片尺寸、工具超时、进度消息、图片发送和自然语言收尾保持现有行为。

两个后端适配器继续保留各自校验，统一图片服务增加调用前校验。

生产验证输出禁止包含 Provider 配置、OAuth 凭据、Dashboard token、完整提示词或图片原始数据。

---

### Task 1: Codex OAuth 适配器字符上限

**Files:**
- Modify: `tests/test_codex_oauth_image_service.py`
- Modify: `utils/codex_oauth_image_service.py`

**Interfaces:**
- Consumes: `CodexOAuthImageService.generate(prompt: str, size: str)`
- Produces: `CodexOAuthImageService.MAX_PROMPT_CHARS == 2048`

- [ ] **Step 1: 增加失败测试**

在 `CodexOAuthImageServiceTest` 中加入以下边界测试：

```python
def test_generate_accepts_prompt_with_exactly_2048_characters(self):
    with tempfile.TemporaryDirectory() as tmpdir:
        result_path = Path(tmpdir) / "result.png"
        result_path.write_bytes(b"result")
        provider = FakeProvider(result_path)
        prompt = "a" * 2048

        asyncio.run(
            self.make_service(provider).generate(prompt=prompt, size="1:1")
        )

    self.assertEqual(provider.calls[0]["prompt"], prompt)

def test_generate_rejects_prompt_with_2049_characters(self):
    provider = FakeProvider(Path("unused.png"))

    with self.assertRaisesRegex(CodexOAuthImageUserError, "2048"):
        asyncio.run(
            self.make_service(provider).generate(
                prompt="a" * 2049,
                size="1:1",
            )
        )

    self.assertEqual(provider.calls, [])
```

- [ ] **Step 2: 运行测试并确认旧实现失败**

Run:

```powershell
wsl.exe --cd ~ -- bash -lc "cd /mnt/s/Projects/astrbot_plugin_group_chat_plus && mkdir -p .tmp/pycache .tmp/tmp && PYTHONPYCACHEPREFIX=.tmp/pycache TMPDIR=.tmp/tmp python3 -m unittest tests.test_codex_oauth_image_service.CodexOAuthImageServiceTest.test_generate_accepts_prompt_with_exactly_2048_characters tests.test_codex_oauth_image_service.CodexOAuthImageServiceTest.test_generate_rejects_prompt_with_2049_characters -v"
```

Expected: 2048 字符用例因当前 512 字符上限失败。

- [ ] **Step 3: 修改 Codex OAuth 上限与错误文本**

在 `CodexOAuthImageService` 中修改：

```python
class CodexOAuthImageService:
    MAX_PROMPT_CHARS = 2048

    def _validate_prompt(self, prompt: str) -> str:
        clean_prompt = str(prompt or "").strip()
        if not clean_prompt:
            raise CodexOAuthImageUserError("图片提示词不能为空。")
        if len(clean_prompt) > self.MAX_PROMPT_CHARS:
            raise CodexOAuthImageUserError(
                f"图片提示词最多 {self.MAX_PROMPT_CHARS} 个字符。"
            )
        return clean_prompt
```

- [ ] **Step 4: 运行 Codex OAuth 图片服务测试**

Run:

```powershell
wsl.exe --cd ~ -- bash -lc "cd /mnt/s/Projects/astrbot_plugin_group_chat_plus && mkdir -p .tmp/pycache .tmp/tmp && PYTHONPYCACHEPREFIX=.tmp/pycache TMPDIR=.tmp/tmp python3 -m unittest tests.test_codex_oauth_image_service -v"
```

Expected: PASS。

- [ ] **Step 5: 提交适配器修改**

```powershell
git add tests/test_codex_oauth_image_service.py utils/codex_oauth_image_service.py
git commit -m "feat: expand Codex image prompt limit"
```

### Task 2: 统一图片服务校验与工具说明

**Files:**
- Modify: `tests/test_group_image_service.py`
- Modify: `tests/test_step_image_tool_integration.py`
- Modify: `utils/group_image_service.py`
- Modify: `main.py`

**Interfaces:**
- Consumes: `CodexOAuthImageService.MAX_PROMPT_CHARS`、`StepImageService.MAX_PROMPT_CHARS`
- Produces: `GroupImageService.max_prompt_chars() -> int`
- Produces: `GroupImageService._validate_prompt(prompt: str) -> None`

- [ ] **Step 1: 增加统一服务失败测试**

在 `GroupImageServiceTest` 中加入：

```python
def test_max_prompt_chars_uses_active_backend_adapter_limit(self):
    codex = self.make_service(config={"image_tool_backend": "codex_oauth"})
    stepfun = self.make_service(config={"image_tool_backend": "stepfun"})

    self.assertEqual(codex.max_prompt_chars(), 2048)
    self.assertEqual(stepfun.max_prompt_chars(), 512)

def test_codex_generate_accepts_2048_characters(self):
    backend = RecordingBackend("codex_oauth")
    service = self.make_service(
        config={"image_tool_backend": "codex_oauth"},
        codex=backend,
    )

    asyncio.run(service.generate(prompt="a" * 2048, size="1:1"))

    self.assertEqual(backend.calls[0][1]["prompt"], "a" * 2048)

def test_codex_generate_rejects_2049_characters_before_backend_call(self):
    backend = RecordingBackend("codex_oauth")
    service = self.make_service(
        config={"image_tool_backend": "codex_oauth"},
        codex=backend,
    )

    with self.assertRaisesRegex(GroupImageUserError, "2048"):
        asyncio.run(service.generate(prompt="a" * 2049, size="1:1"))

    self.assertEqual(backend.calls, [])

def test_stepfun_edit_rejects_513_characters_before_backend_call(self):
    backend = RecordingBackend("stepfun")
    service = self.make_service(
        config={"image_tool_backend": "stepfun"},
        stepfun=backend,
    )

    with self.assertRaisesRegex(GroupImageUserError, "512"):
        asyncio.run(
            service.edit(prompt="a" * 513, image_path="input.png")
        )

    self.assertEqual(backend.calls, [])
```

在 `test_tool_description_requires_model_refined_prompt` 中加入：

```python
self.assertEqual(self.main_source.count("Codex OAuth 后端最多 2048 个字符"), 2)
self.assertEqual(self.main_source.count("StepFun 后端最多 512 个字符"), 2)
```

- [ ] **Step 2: 运行新增测试并确认缺少统一接口与新工具说明**

Run:

```powershell
wsl.exe --cd ~ -- bash -lc "cd /mnt/s/Projects/astrbot_plugin_group_chat_plus && mkdir -p .tmp/pycache .tmp/tmp && PYTHONPYCACHEPREFIX=.tmp/pycache TMPDIR=.tmp/tmp python3 -m unittest tests.test_group_image_service tests.test_step_image_tool_integration.StepImageToolIntegrationTest.test_tool_description_requires_model_refined_prompt -v"
```

Expected: `max_prompt_chars` 缺失，超过上限的 fake backend 仍收到调用，工具说明仍包含固定 512 字符文本。

- [ ] **Step 3: 增加后端专属上限查询与前置校验**

在 `GroupImageService` 中加入：

```python
def max_prompt_chars(self) -> int:
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
```

在 `generate()` 和 `edit()` 的第一行调用：

```python
self._validate_prompt(prompt)
```

调用后端时继续传入原始 `prompt`，由后端完成现有的空白清理和最终校验。

- [ ] **Step 4: 修改两个 LLM 工具参数说明**

将两个工具的 `prompt(string)` 说明分别修改为生成或编辑语义，并追加相同限制：

```python
Codex OAuth 后端最多 2048 个字符，StepFun 后端最多 512 个字符。
```

- [ ] **Step 5: 运行统一服务与图片工具测试**

Run:

```powershell
wsl.exe --cd ~ -- bash -lc "cd /mnt/s/Projects/astrbot_plugin_group_chat_plus && mkdir -p .tmp/pycache .tmp/tmp && PYTHONPYCACHEPREFIX=.tmp/pycache TMPDIR=.tmp/tmp python3 -m unittest tests.test_group_image_service tests.test_step_image_tool_integration tests.test_codex_oauth_image_service -v"
```

Expected: PASS。

- [ ] **Step 6: 提交统一服务与工具说明修改**

```powershell
git add tests/test_group_image_service.py tests/test_step_image_tool_integration.py utils/group_image_service.py main.py
git commit -m "feat: apply backend-specific image prompt limits"
```

### Task 3: 文档、全量验证与生产发布

**Files:**
- Modify: `README.md`
- Modify: `docs/CONFIG_REFERENCE.md`
- Modify: `docs/MESSAGE_WORKFLOW.md`
- Modify: `CHANGELOG.md`

**Interfaces:**
- Consumes: Codex OAuth 2048 字符与 StepFun 512 字符的运行时行为
- Produces: 与运行时一致的用户文档和发布记录

- [ ] **Step 1: 更新文档**

在 README 图片后端说明、配置参考和消息处理文档中加入以下语义：

```markdown
图片提示词上限随后端分别校验：Codex OAuth 最多 2048 个字符，StepFun 最多 512 个字符。
```

在 `CHANGELOG.md` 当前自用版本条目加入：

```markdown
- Codex OAuth 图片提示词上限调整为 2048 字符，StepFun 保持 512 字符；统一图片服务和 LLM 工具说明按当前后端分别校验
```

- [ ] **Step 2: 运行全量单元测试**

Run:

```powershell
wsl.exe --cd ~ -- bash -lc "cd /mnt/s/Projects/astrbot_plugin_group_chat_plus && mkdir -p .tmp/pycache .tmp/tmp && PYTHONPYCACHEPREFIX=.tmp/pycache TMPDIR=.tmp/tmp python3 -m unittest discover -s tests -v"
```

Expected: 全部测试通过。

- [ ] **Step 3: 执行编译与差异检查**

Run:

```powershell
wsl.exe --cd ~ -- bash -lc "cd /mnt/s/Projects/astrbot_plugin_group_chat_plus && mkdir -p .tmp/pycache .tmp/tmp && PYTHONPYCACHEPREFIX=.tmp/pycache TMPDIR=.tmp/tmp python3 -m py_compile main.py utils/codex_oauth_image_service.py utils/group_image_service.py tests/test_codex_oauth_image_service.py tests/test_group_image_service.py tests/test_step_image_tool_integration.py"
```

Expected: 命令退出码为 0。

Run:

```powershell
git diff --check
```

Expected: 无输出，退出码为 0。

- [ ] **Step 4: 提交文档并推送主分支**

```powershell
git add README.md docs/CONFIG_REFERENCE.md docs/MESSAGE_WORKFLOW.md CHANGELOG.md
git commit -m "docs: document image prompt limits"
git push origin main
```

- [ ] **Step 5: 同步生产文件并编译**

仅同步以下文件到 `/volume1/docker/astrbot/data/plugins/astrbot_plugin_group_chat_plus`：

```text
main.py
utils/codex_oauth_image_service.py
utils/group_image_service.py
README.md
docs/CONFIG_REFERENCE.md
docs/MESSAGE_WORKFLOW.md
CHANGELOG.md
```

在 `astrbot` 容器内执行 Python 编译，输出仅保留文件名与成功状态。

- [ ] **Step 6: 重载插件并检查运行状态**

通过 Dashboard API 调用：

```http
POST /api/plugin/reload
Content-Type: application/json

{"name":"astrbot_plugin_group_chat_plus"}
```

检查重载时间段内目标插件单次卸载与加载、`ERROR`、`Traceback`、图片工具注册和命令数量。读取 `/api/plugin/get` 确认插件处于启用状态，读取 `/api/commands` 确认命令注册数量保持现值。

- [ ] **Step 7: 检查生产代码常量**

在容器内导入图片服务模块并输出以下非敏感值：

```text
CodexOAuthImageService.MAX_PROMPT_CHARS=2048
StepImageService.MAX_PROMPT_CHARS=512
```

生产验证完成后，检查本地 `git status --short`，仅允许保留任务开始前已有的未跟踪文件。
