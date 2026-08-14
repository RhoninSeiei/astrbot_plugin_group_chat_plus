# StepImage Group-Only Tool Visibility Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ensure `gcp_step_image_generate` and `gcp_step_image_edit` enter an LLM request only when the current UMO belongs to an authorized group chat.

**Architecture:** Add a non-destructive tool-container exclusion operation to `ToolPolicy`, then centralize StepImage request visibility in two `ChatPlus` helpers. Apply the helper before the non-plugin early return and again after plugin tool merging, while retaining `_step_image_guard()` for execution-time verification.

**Tech Stack:** Python 3, AstrBot plugin hooks, `unittest`, AST-based isolated method tests.

## Global Constraints

1. Preserve ImgFlow, search, MCP, skills, and all unrelated tools in their original order.
2. Preserve the input tool container whenever StepImage tools are denied.
3. Authorized group requests retain the original tool container and both StepImage tools.
4. Private chats, unauthorized groups, disabled group chat, and disabled image service exclude both StepImage tools before model inference.
5. Keep `_step_image_guard()` as execution-time verification.
6. Visibility-filter logs contain only platform, private-chat state, and removed tool names; do not expose UMO, group IDs, prompts, Provider configuration, access tokens, or keys.
7. Leave `docs/superpowers/plans/2026-04-17-matoi-guardian-ep5-plugin.md` untouched.

---

### Task 1: Non-Destructive Tool Container Exclusion

**Files:**
- Modify: `utils/tool_policy.py`
- Test: `tests/test_tool_policy.py`

**Interfaces:**
- Consumes: `ToolPolicy.clone_tool_container(tool_container)` and `ToolPolicy.filter_tool_container_for_visible_names(tool_container, visible_names)`.
- Produces: `ToolPolicy.clone_without_tool_names(tool_container, denied_names) -> tuple[object | None, list[str]]`.

- [ ] **Step 1: Write failing container tests**

Add tests that exercise real `ToolPolicy` code:

```python
def test_clone_without_tool_names_preserves_original_and_unrelated_order(self):
    tool_policy = _load_tool_policy()
    original = RemoveToolContainer(
        [
            "normal_search",
            "gcp_step_image_generate",
            "astrbot_plugin_imgflow_generate_image",
            "gcp_step_image_edit",
        ]
    )

    filtered, removed = tool_policy.clone_without_tool_names(
        original,
        {"gcp_step_image_generate", "gcp_step_image_edit"},
    )

    self.assertEqual(
        [tool.name for tool in original.tools],
        [
            "normal_search",
            "gcp_step_image_generate",
            "astrbot_plugin_imgflow_generate_image",
            "gcp_step_image_edit",
        ],
    )
    self.assertEqual(
        [tool.name for tool in filtered.tools],
        ["normal_search", "astrbot_plugin_imgflow_generate_image"],
    )
    self.assertEqual(
        removed,
        ["gcp_step_image_generate", "gcp_step_image_edit"],
    )
```

Add equivalent coverage for `FuncListContainer`, `None`, and an empty denied-name set.

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```bash
python3 -m unittest tests.test_tool_policy -v
```

Expected: the new tests fail because `clone_without_tool_names` does not exist.

- [ ] **Step 3: Implement the minimal container operation**

Add the following class method to `ToolPolicy`:

```python
@classmethod
def clone_without_tool_names(
    cls,
    tool_container,
    denied_names: Optional[Iterable[str]],
):
    if tool_container is None:
        return None, []

    denied_set = _normalize_names(denied_names)
    if not denied_set:
        return tool_container, []

    cloned = cls.clone_tool_container(tool_container)
    visible_names = {
        str(getattr(tool, "name", "")).strip()
        for tool in cls._get_container_tools(cloned)
        if str(getattr(tool, "name", "")).strip() not in denied_set
    }
    removed_names = cls.filter_tool_container_for_visible_names(
        cloned,
        visible_names,
    )
    return cloned, removed_names
```

- [ ] **Step 4: Run focused tests and verify GREEN**

Run:

```bash
python3 -m unittest tests.test_tool_policy -v
```

Expected: all `tests.test_tool_policy` tests pass with no warnings.

- [ ] **Step 5: Commit Task 1**

```bash
git add utils/tool_policy.py tests/test_tool_policy.py
git commit -m "fix: add non-destructive tool exclusion"
```

### Task 2: UMO-Aware StepImage Request Filtering

**Files:**
- Modify: `main.py`
- Modify: `docs/CONFIG_REFERENCE.md`
- Test: `tests/test_step_image_tool_integration.py`
- Test: `tests/test_tool_passthrough.py`

**Interfaces:**
- Consumes: `ToolPolicy.clone_without_tool_names(...)`, `GroupImageService.is_enabled(...)`, `_is_step_image_enabled_for_event(event)`, and `STEP_IMAGE_TOOL_NAMES`.
- Produces: `ChatPlus._can_expose_step_image_tools(event) -> bool` and `ChatPlus._filter_step_image_tools_for_request(event, tool_container) -> tuple[object | None, list[str]]`.

- [ ] **Step 1: Write failing authorization and filtering tests**

Extend the AST-based method harness in `tests/test_step_image_tool_integration.py` to compile the existing group-ID helpers together with the two new methods. Use real `ToolPolicy` behavior and fake events for:

```text
aiocqhttp:GroupMessage:10001  authorized group
aiocqhttp:GroupMessage:20002  unauthorized group
aiocqhttp:FriendMessage:42    QQ private chat
gewechat:FriendMessage:wxid   WeChat private chat
```

Each tool container contains, in order:

```python
[
    "normal_search",
    "gcp_step_image_generate",
    "astrbot_plugin_imgflow_generate_image",
    "gcp_step_image_edit",
]
```

Required assertions:

```python
self.assertIs(filtered, original)  # authorized non-fallback branch
self.assertEqual(removed, [])
```

for an authorized group, and:

```python
self.assertIsNot(filtered, original)
self.assertEqual(
    [tool.name for tool in filtered.tools],
    ["normal_search", "astrbot_plugin_imgflow_generate_image"],
)
self.assertEqual(
    [tool.name for tool in original.tools],
    [
        "normal_search",
        "gcp_step_image_generate",
        "astrbot_plugin_imgflow_generate_image",
        "gcp_step_image_edit",
    ],
)
```

for each denied scenario. Add cases for `enable_group_chat=False` and an image service disabled result.

Update `tests/test_tool_passthrough.py` with source-order assertions proving that the request filter executes before `if not is_plugin_request: return`, and that a second request filter executes after plugin tool merging and before `current_tools` is read.

- [ ] **Step 2: Run focused tests and verify RED**

Run:

```bash
python3 -m unittest \
  tests.test_step_image_tool_integration \
  tests.test_tool_passthrough \
  -v
```

Expected: new tests fail because the two request-visibility helpers and hook calls do not exist.

- [ ] **Step 3: Add the centralized visibility helpers**

Add beside `_is_step_image_enabled_for_event`:

```python
def _can_expose_step_image_tools(self, event: AstrMessageEvent) -> bool:
    return (
        self.enable_group_chat
        and GroupImageService.is_enabled(self.step_image_config)
        and self._is_step_image_enabled_for_event(event)
    )

def _filter_step_image_tools_for_request(self, event, tool_container):
    if self._can_expose_step_image_tools(event):
        return tool_container, []
    return ToolPolicy.clone_without_tool_names(
        tool_container,
        STEP_IMAGE_TOOL_NAMES,
    )
```

Change formal reply policy construction to:

```python
allow_step_image=self._can_expose_step_image_tools(event),
```

- [ ] **Step 4: Filter every LLM request before plugin-marker return**

At the start of `on_llm_request`, after image sanitization and before reading `PLUGIN_REQUEST_MARKER`, apply:

```python
req.func_tool, removed_step_image_tools = (
    self._filter_step_image_tools_for_request(event, req.func_tool)
)
if removed_step_image_tools:
    logger.info(
        "GCP_TOOL_VISIBILITY_FILTERED platform=%s private=%s removed=%s",
        event.get_platform_name(),
        event.is_private_chat(),
        removed_step_image_tools,
    )
```

After merging `PLUGIN_FUNC_TOOL` and before `current_tools = _get_compatible_tools(req.func_tool)`, call the same helper again. This second call prevents plugin tool merging or a visible-tool metadata failure from reintroducing either StepImage tool.

- [ ] **Step 5: Update configuration documentation**

Amend the StepImage section in `docs/CONFIG_REFERENCE.md` to state that the two GCP tools are removed from private-chat and unauthorized-group `ProviderRequest` tool sets before model inference, while the execution guard remains active.

- [ ] **Step 6: Run focused tests and verify GREEN**

Run:

```bash
python3 -m unittest \
  tests.test_tool_policy \
  tests.test_step_image_tool_integration \
  tests.test_tool_passthrough \
  tests.test_group_only_boundary \
  tests.test_group_image_tool_timeout \
  -v
```

Expected: all focused tests pass with no warnings.

- [ ] **Step 7: Commit Task 2**

```bash
git add main.py docs/CONFIG_REFERENCE.md \
  tests/test_step_image_tool_integration.py tests/test_tool_passthrough.py
git commit -m "fix: hide group image tools outside authorized chats"
```

### Task 3: Review, Full Verification, and Production Release

**Files:**
- Verify: all changed files from Tasks 1 and 2
- Production sync: `main.py`, `utils/tool_policy.py`
- Production documentation sync: `docs/CONFIG_REFERENCE.md`

**Interfaces:**
- Consumes: commits from Tasks 1 and 2.
- Produces: reviewed changes, complete test evidence, one target-plugin reload, and scoped runtime evidence.

- [ ] **Step 1: Run task and whole-branch reviews**

Review the diff against the design specification. Required checks include private requests, unauthorized groups, authorized groups, input-container immutability, preservation of unrelated tools, and hook ordering.

- [ ] **Step 2: Run the full local verification suite in WSL**

Run from a home-based WSL shell:

```bash
wsl.exe --cd ~ -- bash -lc "
  cd /mnt/s/Projects/astrbot_plugin_group_chat_plus &&
  mkdir -p .tmp/pycache .tmp/tmp &&
  PYTHONPYCACHEPREFIX=.tmp/pycache TMPDIR=.tmp/tmp \
    python3 -m unittest discover -s tests -v &&
  python3 -m json.tool _conf_schema.json >/dev/null &&
  python3 -m py_compile main.py utils/tool_policy.py &&
  git diff --check
"
```

Expected: zero test failures, valid JSON, successful Python compilation, and no whitespace errors.

- [ ] **Step 3: Synchronize only changed production files**

Copy `main.py`, `utils/tool_policy.py`, and `docs/CONFIG_REFERENCE.md` to `/volume1/docker/astrbot/data/plugins/astrbot_plugin_group_chat_plus` without deleting any remote file. Compile `main.py` and `utils/tool_policy.py` inside the `astrbot` container using `compile(...)` with no `__pycache__` write.

- [ ] **Step 4: Reload only the target plugin**

Use the Dashboard API endpoint:

```http
POST /api/plugin/reload
Content-Type: application/json

{"name":"astrbot_plugin_group_chat_plus"}
```

Expected: one termination and one load event for the target plugin, with no container restart.

- [ ] **Step 5: Verify production tool visibility**

Use a read-only request-construction probe or existing AstrBot request inspection facility to verify:

```text
authorized group request: both GCP StepImage tools present
private request: both GCP StepImage tools absent
unrelated ImgFlow tool: present in both requests when otherwise enabled
```

Inspect target logs for `GCP_TOOL_VISIBILITY_FILTERED`, plugin load errors, tracebacks, command registration, and StepImage initialization. Do not send a paid image request unless request construction cannot prove the final tool set.
