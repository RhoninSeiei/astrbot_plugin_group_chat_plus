# Group Chat Plus Quoted Image Handling Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Preserve quoted QQ images through Group Chat Plus image discovery, multimodal or image-to-text processing, wait-window caching, formal LLM requests, and official conversation history.

**Architecture:** Add an ordered `ResolvedMessageImage` representation and a shared asynchronous collector to `ImageHandler`. Use the collector in normal message processing and the wait-window branch, then reuse one history-content builder for cached and current user messages. Existing `PLUGIN_IMAGE_URLS`, `ReplyHandler.generate_reply()`, and `on_llm_request()` remain the transport mechanism.

**Tech Stack:** Python 3, AstrBot 4.27.2 message components, `astrbot.core.utils.quoted_message_parser`, `asyncio`, `unittest`, WSL, AstrBot Dashboard API.

## Global Constraints

1. Modify only `astrbot_plugin_group_chat_plus`; leave AstrBot Core, OAuth Provider, Matoi CC, NapCat, and aiocqhttp unchanged.
2. Support both empty `image_to_text_provider_id` multimodal mode and non-empty image-to-text Provider mode.
3. Prefer `Reply.chain` images and avoid `get_msg` when every embedded image resolves successfully.
4. Call `extract_quoted_message_images(event, reply_component)` when embedded images are missing or any embedded image fails to resolve.
5. Preserve top-level component order, quoted-image order, first-occurrence deduplication, and one shared `max_images_per_message` limit.
6. Preserve message text and successfully resolved images when one image fails.
7. Keep the existing four-value `process_message_images()` return interface.
8. Keep `PLUGIN_IMAGE_URLS`, `ReplyHandler.generate_reply()`, and `on_llm_request()` production behavior unchanged.
9. Logs may contain counts, fixed source names, stages, component indexes, and exception class names. Logs must exclude image URLs, Base64, local file contents, quoted text, group IDs, user IDs, Provider configuration, tokens, and keys.
10. Tests must exercise ordinary successful behavior. Exception compensation alone cannot satisfy acceptance.
11. Leave `docs/superpowers/plans/2026-04-17-matoi-guardian-ep5-plugin.md` untouched.

---

### Task 1: Ordered Top-Level and Quoted Image Collection

**Files:**
- Modify: `utils/image_handler.py`
- Create: `tests/test_image_handler_quoted_images.py`

**Interfaces:**
- Consumes: `Image.convert_to_file_path()` and `extract_quoted_message_images(event, reply_component)`.
- Produces: `ResolvedMessageImage(url: str, source: str, component_index: int)` and `ImageHandler.collect_message_images(event, message_chain=None, max_images=10) -> List[ResolvedMessageImage]`.

- [ ] **Step 1: Add an isolated ImageHandler test harness**

Create `tests/test_image_handler_quoted_images.py`. Stub `astrbot.api.all`, message components, `ImageDescriptionCache`, and the public quoted-message parser before loading `utils/image_handler.py`. The fake image and reply types must expose the production attributes used by the collector:

```python
class FakeImage:
    def __init__(self, value=None, error=None):
        self.value = value
        self.error = error

    async def convert_to_file_path(self):
        if self.error:
            raise self.error
        return self.value


class FakeReply:
    def __init__(self, reply_id="r1", chain=None, message_str="[引用消息]"):
        self.id = reply_id
        self.chain = list(chain or [])
        self.message_str = message_str
```

Provide a parser stub that records calls and returns values by reply ID:

```python
quoted_results = {}
quoted_calls = []

async def extract_quoted_message_images(event, reply_component):
    quoted_calls.append(reply_component.id)
    value = quoted_results.get(reply_component.id, [])
    if isinstance(value, Exception):
        raise value
    return list(value)
```

- [ ] **Step 2: Write failing collector tests**

Add tests with these exact assertions:

```python
def test_embedded_reply_image_avoids_remote_fetch(self):
    chain = [FakeReply("r1", [FakeImage("quoted-a")])]
    result = asyncio.run(
        ImageHandler.collect_message_images(FakeEvent(chain), chain, 10)
    )
    self.assertEqual([item.url for item in result], ["quoted-a"])
    self.assertEqual([item.source for item in result], ["quoted_embedded"])
    self.assertEqual(quoted_calls, [])


def test_empty_reply_chain_uses_public_parser(self):
    quoted_results["r1"] = ["quoted-a"]
    chain = [FakeReply("r1")]
    result = asyncio.run(
        ImageHandler.collect_message_images(FakeEvent(chain), chain, 10)
    )
    self.assertEqual([item.url for item in result], ["quoted-a"])
    self.assertEqual([item.source for item in result], ["quoted_fetched"])
    self.assertEqual(quoted_calls, ["r1"])


def test_partial_embedded_failure_prefers_complete_parser_order(self):
    quoted_results["r1"] = ["quoted-a", "quoted-b"]
    chain = [
        FakeReply(
            "r1",
            [FakeImage("quoted-a"), FakeImage(error=ValueError("expired"))],
        )
    ]
    result = asyncio.run(
        ImageHandler.collect_message_images(FakeEvent(chain), chain, 10)
    )
    self.assertEqual([item.url for item in result], ["quoted-a", "quoted-b"])
    self.assertEqual([item.source for item in result], ["quoted_fetched"] * 2)
```

Add ordinary-success tests for a top-level image, mixed top-level and multiple replies, duplicate removal, a shared limit, and parser failure retaining successful embedded images:

```python
self.assertEqual(
    [(item.url, item.component_index) for item in result],
    [("top-a", 0), ("quoted-a", 1), ("top-b", 2)],
)
```

- [ ] **Step 3: Run the collector tests and verify RED**

Run from WSL home:

```bash
wsl.exe --cd ~ -- bash -lc "
  cd /mnt/s/Projects/astrbot_plugin_group_chat_plus &&
  mkdir -p .tmp/pycache .tmp/tmp &&
  PYTHONPYCACHEPREFIX=.tmp/pycache TMPDIR=.tmp/tmp \
    python3 -m unittest tests.test_image_handler_quoted_images -v
"
```

Expected: failures report missing `ResolvedMessageImage` or `collect_message_images`.

- [ ] **Step 4: Add the image record and safe resolver helpers**

Update imports in `utils/image_handler.py`:

```python
from dataclasses import dataclass
from typing import List, Optional, Tuple
from astrbot.core.utils.quoted_message_parser import extract_quoted_message_images


@dataclass(frozen=True)
class ResolvedMessageImage:
    url: str
    source: str
    component_index: int
```

Add helpers that return values without logging the URL:

```python
@staticmethod
async def _resolve_image_component(image, source: str, component_index: int):
    try:
        value = await image.convert_to_file_path()
    except Exception as exc:
        logger.warning(
            "[QUOTED_IMAGE_RESOLVE_FAILED] source=%s component_index=%s error_type=%s",
            source,
            component_index,
            type(exc).__name__,
        )
        return None
    value = str(value or "").strip()
    if not value:
        logger.warning(
            "[QUOTED_IMAGE_RESOLVE_FAILED] source=%s component_index=%s error_type=empty_result",
            source,
            component_index,
        )
        return None
    return ResolvedMessageImage(value, source, component_index)
```

- [ ] **Step 5: Implement ordered collection and fallback behavior**

Implement `collect_message_images()` with one append helper and one per-reply helper. For a partial embedded failure, discard the partial list when the public parser succeeds; use the partial list only when the parser returns no valid result or raises:

```python
@staticmethod
async def collect_message_images(event, message_chain=None, max_images=10):
    chain = list(message_chain or getattr(event.message_obj, "message", []) or [])
    limit = max(0, int(max_images))
    if limit == 0:
        return []

    results = []
    seen = set()

    def append_items(items):
        for item in items:
            if item.url in seen:
                continue
            seen.add(item.url)
            results.append(item)
            if len(results) >= limit:
                return True
        return False

    for component_index, component in enumerate(chain):
        if isinstance(component, Image):
            item = await ImageHandler._resolve_image_component(
                component, "top_level", component_index
            )
            if item and append_items([item]):
                break
            continue
        if not isinstance(component, Reply):
            continue

        items = await ImageHandler._collect_reply_images(
            event, component, component_index
        )
        if append_items(items):
            break

    logger.info(
        "[QUOTED_IMAGE_COLLECTED] top_level=%s quoted_embedded=%s quoted_fetched=%s total=%s",
        sum(item.source == "top_level" for item in results),
        sum(item.source == "quoted_embedded" for item in results),
        sum(item.source == "quoted_fetched" for item in results),
        len(results),
    )
    return results
```

Implement the per-reply helper with the complete embedded-first decision:

```python
@staticmethod
async def _collect_reply_images(event, reply, component_index):
    embedded = [
        item for item in (getattr(reply, "chain", None) or [])
        if isinstance(item, Image)
    ]
    embedded_results = []
    embedded_failed = False
    for image in embedded:
        resolved = await ImageHandler._resolve_image_component(
            image,
            "quoted_embedded",
            component_index,
        )
        if resolved is None:
            embedded_failed = True
        else:
            embedded_results.append(resolved)

    if embedded and not embedded_failed:
        return embedded_results

    reason = "missing_embedded" if not embedded else "embedded_resolve_failed"
    logger.info("[QUOTED_IMAGE_FALLBACK] reason=%s", reason)
    try:
        fetched = await extract_quoted_message_images(event, reply)
    except Exception as exc:
        logger.warning(
            "[QUOTED_IMAGE_RESOLVE_FAILED] source=quoted_fetched "
            "component_index=%s error_type=%s",
            component_index,
            type(exc).__name__,
        )
        return embedded_results

    fetched_results = [
        ResolvedMessageImage(str(value).strip(), "quoted_fetched", component_index)
        for value in fetched or []
        if str(value or "").strip()
    ]
    return fetched_results or embedded_results
```

- [ ] **Step 6: Run collector tests and verify GREEN**

Run the Task 1 command again. Expected: every collector test passes, including the ordinary top-level-image branch and the embedded success branch that records zero parser calls.

- [ ] **Step 7: Commit Task 1**

```bash
git add utils/image_handler.py tests/test_image_handler_quoted_images.py
git commit -m "fix: collect images from quoted messages"
```

### Task 2: Multimodal, Image-to-Text, and Wait-Window Integration

**Files:**
- Modify: `utils/image_handler.py`
- Modify: `main.py`
- Modify: `tests/test_image_handler_quoted_images.py`
- Create: `tests/test_wait_window_quoted_images.py`
- Modify: `tests/test_tool_passthrough.py`

**Interfaces:**
- Consumes: `ImageHandler.collect_message_images(...)` and `ResolvedMessageImage` from Task 1.
- Produces: updated `process_message_images()` behavior, `_convert_images_to_text(..., resolved_images, ...)`, and wait-window cache entries with either `image_urls` or image descriptions.

- [ ] **Step 1: Write failing multimodal and image-to-text tests**

Extend `tests/test_image_handler_quoted_images.py` with a fake Context and Provider:

```python
class FakeProvider:
    def __init__(self):
        self.calls = []

    async def text_chat(self, **kwargs):
        self.calls.append(kwargs)
        image = kwargs["image_urls"][0]
        return types.SimpleNamespace(completion_text=f"desc:{image}")


class FakeContext:
    def __init__(self, provider):
        self.provider = provider

    def get_provider_by_id(self, provider_id):
        return self.provider if provider_id == "vision" else None
```

Add these required cases:

```python
def test_multimodal_mode_returns_quoted_image_url(self):
    result = asyncio.run(
        ImageHandler.process_message_images(
            event,
            FakeContext(None),
            True,
            "all",
            "",
            "describe",
            True,
            False,
        )
    )
    self.assertEqual(result[2], ["quoted-a"])
    self.assertTrue(result[3])


def test_image_to_text_places_quote_description_after_reply_marker(self):
    result = asyncio.run(
        ImageHandler.process_message_images(
            event,
            FakeContext(provider),
            True,
            "all",
            "vision",
            "describe",
            True,
            False,
        )
    )
    self.assertIn("[引用消息][引用图片内容: desc:quoted-a]问题正文", result[1])
    self.assertEqual(provider.calls[0]["image_urls"], ["quoted-a"])
    self.assertEqual(result[2], [])
    self.assertTrue(result[3])
```

Add a mixed-message case where one Provider call raises and the remaining description remains in the returned text.

- [ ] **Step 2: Write failing wait-window and request transport tests**

Create `tests/test_wait_window_quoted_images.py` using the established AST-based main-method harness. Compile `_maybe_intercept_for_wait_window()` with fake locks, cache manager, image handler, and event. Assert the ordinary successful wait-window branch stores:

```python
self.assertEqual(cached_message["image_urls"], ["quoted-a"])
self.assertIn("引用探针", cached_message["content"])
```

Add a non-empty Provider case that returns a description and asserts `image_urls == []` while the cached content contains `[引用图片内容: ...]`.

Extend `tests/test_tool_passthrough.py` with a source-order or AST assertion proving that non-empty current and wait-window image arrays reach `PLUGIN_IMAGE_URLS`, and that `req.image_urls = plugin_image_urls` remains after request restoration.

- [ ] **Step 3: Run Task 2 tests and verify RED**

```bash
wsl.exe --cd ~ -- bash -lc "
  cd /mnt/s/Projects/astrbot_plugin_group_chat_plus &&
  mkdir -p .tmp/pycache .tmp/tmp &&
  PYTHONPYCACHEPREFIX=.tmp/pycache TMPDIR=.tmp/tmp \
    python3 -m unittest \
      tests.test_image_handler_quoted_images \
      tests.test_wait_window_quoted_images \
      tests.test_tool_passthrough -v
"
```

Expected: the new integration tests fail because normal processing and the wait window do not consume the shared collector.

- [ ] **Step 4: Refactor normal message processing to use resolved records**

At the start of `process_message_images()` after reading `message_chain`, collect images once:

```python
resolved_images = await ImageHandler.collect_message_images(
    event,
    message_chain,
    max_images_per_message,
)
has_image = bool(resolved_images)
has_text = ImageHandler._has_text_content(message_chain)
```

Split the existing text detection from `_analyze_message()` into this method so `Reply` continues to count as textual context:

```python
@staticmethod
def _has_text_content(message_chain):
    return any(
        isinstance(component, Reply)
        or (
            isinstance(component, Plain)
            and bool(str(getattr(component, "text", "") or "").strip())
        )
        for component in message_chain
    )
```

In multimodal mode return:

```python
image_urls = [item.url for item in resolved_images]
return True, ImageHandler._extract_text_only(message_chain), image_urls, bool(image_urls)
```

Change `_convert_images_to_text()` to consume `resolved_images`. Store successful descriptions by list index, then group them by `component_index`. During message reconstruction, append top-level descriptions at the `Image` component and quoted descriptions immediately after formatting the `Reply` component:

```python
for chain_index, component in enumerate(message_chain):
    if isinstance(component, Reply):
        formatted = ImageHandler._format_special_component(component)
        if formatted:
            result_parts.append(formatted)
        for description in descriptions_by_component.get(chain_index, []):
            result_parts.append(f"[引用图片内容: {description}]")
```

Preserve `[引用图片]` for an unresolved Provider result attached to a Reply, and preserve other successful descriptions.

- [ ] **Step 5: Integrate the collector into the wait-window branch**

Inside `_maybe_intercept_for_wait_window()`, collect images after the optional `At` removal and before choosing platform-caption behavior:

```python
resolved_images = await ImageHandler.collect_message_images(
    event,
    event.get_messages(),
    self.max_images_per_message,
)
resolved_image_urls = [item.url for item in resolved_images]
has_image = bool(resolved_image_urls) or PlatformLTMHelper.has_image_in_message(event)
cached_image_urls = []
```

For empty `image_to_text_provider_id`, preserve `original_message_text` and set:

```python
cached_image_urls = resolved_image_urls
image_retained = bool(cached_image_urls)
```

For non-empty `image_to_text_provider_id`, keep platform caption and local cache checks first. If neither yields a description and `resolved_images` is non-empty, call the same resolved-image conversion helper. Store the resulting description as content and leave `cached_image_urls=[]`.

Replace the fixed cache value with:

```python
"image_urls": cached_image_urls,
```

The existing wait-window image merge code remains unchanged and receives populated arrays in multimodal mode.

- [ ] **Step 6: Remove unsafe image-address debug output**

Replace logs such as `提取到图片 ...: {image_path}` and `正在转换图片 ...: {image_path}` with index, source, cache state, and count fields. Keep exception class names and remove exception messages when an adapter could include an address.

- [ ] **Step 7: Run Task 2 tests and related image regressions**

```bash
wsl.exe --cd ~ -- bash -lc "
  cd /mnt/s/Projects/astrbot_plugin_group_chat_plus &&
  mkdir -p .tmp/pycache .tmp/tmp &&
  PYTHONPYCACHEPREFIX=.tmp/pycache TMPDIR=.tmp/tmp \
    python3 -m unittest \
      tests.test_image_handler_quoted_images \
      tests.test_wait_window_quoted_images \
      tests.test_tool_passthrough \
      tests.test_step_image_tool_integration \
      tests.test_group_image_service \
      tests.test_codex_oauth_image_service -v
"
```

Expected: all tests pass; the embedded success test still verifies zero public-parser calls.

- [ ] **Step 8: Commit Task 2**

```bash
git add utils/image_handler.py main.py \
  tests/test_image_handler_quoted_images.py \
  tests/test_wait_window_quoted_images.py \
  tests/test_tool_passthrough.py
git commit -m "fix: preserve quoted images through reply processing"
```

### Task 3: Current User Multimodal Conversation History

**Files:**
- Modify: `utils/context_manager.py`
- Modify: `main.py`
- Modify: `tests/test_multimodal_history_content.py`
- Create: `tests/test_current_user_image_save_calls.py`

**Interfaces:**
- Consumes: current-message `last_cached.get("image_urls", [])` from Task 2.
- Produces: `ContextManager.build_user_history_content(text, image_urls=None)` and `save_to_official_conversation_with_cache(..., user_image_urls=None)`.

- [ ] **Step 1: Write failing history-content tests**

Add to `tests/test_multimodal_history_content.py`:

```python
def test_build_user_history_content_preserves_text_and_unique_images(self):
    content = self.ContextManager.build_user_history_content(
        "引用探针",
        ["quoted-a", "", "quoted-a", "quoted-b"],
    )
    self.assertEqual(
        content,
        [
            {"type": "text", "text": "引用探针"},
            {"type": "image_url", "image_url": {"url": "quoted-a"}},
            {"type": "image_url", "image_url": {"url": "quoted-b"}},
        ],
    )


def test_build_user_history_content_without_images_returns_string(self):
    self.assertEqual(
        self.ContextManager.build_user_history_content("普通消息", []),
        "普通消息",
    )
```

Add an asynchronous fake conversation manager test that calls `save_to_official_conversation_with_cache(..., user_image_urls=["quoted-a"])`, captures the updated history list, and asserts the current user entry uses list content.

- [ ] **Step 2: Write failing call-site coverage**

Create `tests/test_current_user_image_save_calls.py`. Parse `main.py` with `ast` and locate every `ContextManager.save_to_official_conversation_with_cache` call in these methods:

```text
after_message_sent
_save_user_messages_on_duplicate_block
_finalize_bot_reply_save
```

For each phase-one call that passes a non-empty current user message, assert the call includes the `user_image_urls` keyword. For phase-two calls with `user_message=None`, assert the keyword is absent or explicitly empty. This verifies ordinary completion as well as both compensation branches.

- [ ] **Step 3: Run Task 3 tests and verify RED**

```bash
wsl.exe --cd ~ -- bash -lc "
  cd /mnt/s/Projects/astrbot_plugin_group_chat_plus &&
  mkdir -p .tmp/pycache .tmp/tmp &&
  PYTHONPYCACHEPREFIX=.tmp/pycache TMPDIR=.tmp/tmp \
    python3 -m unittest \
      tests.test_multimodal_history_content \
      tests.test_current_user_image_save_calls -v
"
```

Expected: failures report the missing content builder, parameter, and call-site keywords.

- [ ] **Step 4: Add and reuse the history-content builder**

Add to `ContextManager`:

```python
@staticmethod
def build_user_history_content(text, image_urls=None):
    clean_urls = []
    seen = set()
    for value in image_urls or []:
        value = str(value or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        clean_urls.append(value)
    if not clean_urls:
        return text

    content = []
    if text:
        content.append({"type": "text", "text": text})
    content.extend(
        {"type": "image_url", "image_url": {"url": value}}
        for value in clean_urls
    )
    return content
```

Replace the cached-message inline multimodal builder with this method so cached and current messages share filtering and shape.

- [ ] **Step 5: Extend official save and pass snapshot images**

Extend the function signature after `context`:

```python
async def save_to_official_conversation_with_cache(
    event,
    cached_messages,
    user_message,
    bot_message,
    context,
    user_image_urls=None,
):
```

Append the current user message with:

```python
user_content = ContextManager.build_user_history_content(
    user_message,
    user_image_urls,
)
history_list.append({"role": "user", "content": user_content})
```

In each phase-one caller, capture image addresses before consuming `last_cached`:

```python
user_image_urls = list(last_cached.get("image_urls", []) or []) if last_cached else []
```

Pass them by keyword:

```python
user_image_urls=user_image_urls,
```

Keep phase-two cache conversion calls unchanged. Log only `image_count` for current user multimodal saves.

- [ ] **Step 6: Run Task 3 tests and related history regressions**

```bash
wsl.exe --cd ~ -- bash -lc "
  cd /mnt/s/Projects/astrbot_plugin_group_chat_plus &&
  mkdir -p .tmp/pycache .tmp/tmp &&
  PYTHONPYCACHEPREFIX=.tmp/pycache TMPDIR=.tmp/tmp \
    python3 -m unittest \
      tests.test_multimodal_history_content \
      tests.test_current_user_image_save_calls \
      tests.test_message_cache_persistent_poke \
      tests.test_main_reply_decline_cache -v
"
```

Expected: all tests pass and the ordinary phase-one call carries current image addresses.

- [ ] **Step 7: Commit Task 3**

```bash
git add utils/context_manager.py main.py \
  tests/test_multimodal_history_content.py \
  tests/test_current_user_image_save_calls.py
git commit -m "fix: save current user images in conversation history"
```

### Task 4: Review, Full Verification, and Production Release

**Files:**
- Verify: all files changed by Tasks 1 through 3
- Production sync: `main.py`, `utils/image_handler.py`, `utils/context_manager.py`

**Interfaces:**
- Consumes: reviewed commits from Tasks 1 through 3.
- Produces: complete local verification evidence, one target-plugin reload, two quoted-image runtime probes, and safe logs.

- [ ] **Step 1: Review each task diff against the specification**

Check these properties from source and tests:

```text
embedded success performs zero public-parser calls
empty or partially failed embedded extraction invokes the public parser
ordinary top-level images retain prior behavior
one shared order, deduplication rule, and image limit applies to all sources
multimodal and image-to-text modes consume the same resolved records
wait-window messages retain image information
ordinary and compensation saves include current user images
logs contain no image address or Base64 value
```

- [ ] **Step 2: Run focused tests**

```bash
wsl.exe --cd ~ -- bash -lc "
  cd /mnt/s/Projects/astrbot_plugin_group_chat_plus &&
  mkdir -p .tmp/pycache .tmp/tmp &&
  PYTHONPYCACHEPREFIX=.tmp/pycache TMPDIR=.tmp/tmp \
    python3 -m unittest \
      tests.test_image_handler_quoted_images \
      tests.test_wait_window_quoted_images \
      tests.test_multimodal_history_content \
      tests.test_current_user_image_save_calls \
      tests.test_tool_passthrough \
      tests.test_step_image_tool_integration -v
"
```

Expected: zero failures and zero errors.

- [ ] **Step 3: Run full local verification**

```bash
wsl.exe --cd ~ -- bash -lc "
  cd /mnt/s/Projects/astrbot_plugin_group_chat_plus &&
  mkdir -p .tmp/pycache .tmp/tmp &&
  PYTHONPYCACHEPREFIX=.tmp/pycache TMPDIR=.tmp/tmp \
    python3 -m unittest discover -s tests -v &&
  python3 -m json.tool _conf_schema.json >/dev/null &&
  python3 -m py_compile main.py utils/image_handler.py utils/context_manager.py &&
  git diff --check
"
```

Expected: zero test failures, valid JSON, successful compilation, and no whitespace errors.

- [ ] **Step 4: Inspect repository state before release**

```bash
git status --short --branch
git log --oneline --decorate -5
git diff origin/main...HEAD -- \
  main.py utils/image_handler.py utils/context_manager.py \
  tests/test_image_handler_quoted_images.py \
  tests/test_wait_window_quoted_images.py \
  tests/test_multimodal_history_content.py \
  tests/test_current_user_image_save_calls.py \
  tests/test_tool_passthrough.py
```

Expected: only planned files appear in the implementation diff; the unrelated Matoi plan remains untracked and untouched.

- [ ] **Step 5: Synchronize only runtime files to production**

Use WSL from home and copy only the three runtime files:

```bash
wsl.exe --cd ~ -- bash -lc "
  cd /mnt/s/Projects/astrbot_plugin_group_chat_plus &&
  scp -P 44012 main.py \
    wty1996@192.168.1.17:/volume1/docker/astrbot/data/plugins/astrbot_plugin_group_chat_plus/main.py &&
  scp -P 44012 utils/image_handler.py \
    wty1996@192.168.1.17:/volume1/docker/astrbot/data/plugins/astrbot_plugin_group_chat_plus/utils/image_handler.py &&
  scp -P 44012 utils/context_manager.py \
    wty1996@192.168.1.17:/volume1/docker/astrbot/data/plugins/astrbot_plugin_group_chat_plus/utils/context_manager.py
"
```

Do not remove any production file or synchronize the unrelated untracked document.

- [ ] **Step 6: Compile in the production container**

```bash
ssh -p 44012 wty1996@192.168.1.17 \
  "docker exec astrbot python -c \"from pathlib import Path; files=['/AstrBot/data/plugins/astrbot_plugin_group_chat_plus/main.py','/AstrBot/data/plugins/astrbot_plugin_group_chat_plus/utils/image_handler.py','/AstrBot/data/plugins/astrbot_plugin_group_chat_plus/utils/context_manager.py']; [compile(Path(p).read_text(encoding='utf-8'), p, 'exec') for p in files]; print('compile_ok=3')\""
```

Expected output: `compile_ok=3`.

- [ ] **Step 7: Reload only Group Chat Plus**

Run one remote script that reads Dashboard credentials without printing them, reloads the named plugin, and checks command registration:

```bash
ssh -p 44012 wty1996@192.168.1.17 "python3 - <<'PY'
import json
import urllib.request
from pathlib import Path

conf = json.loads(
    Path('/volume1/docker/astrbot/data/cmd_config.json').read_text(
        encoding='utf-8-sig'
    )
)
base = 'http://127.0.0.1:16185/api'
login = urllib.request.Request(
    f'{base}/auth/login',
    data=json.dumps({
        'username': conf['dashboard']['username'],
        'password': conf['dashboard']['password'],
    }).encode(),
    headers={'Content-Type': 'application/json'},
)
with urllib.request.urlopen(login, timeout=20) as response:
    token = json.loads(response.read().decode())['data']['token']

headers = {
    'Authorization': f'Bearer {token}',
    'Content-Type': 'application/json',
}
reload_request = urllib.request.Request(
    f'{base}/plugin/reload',
    data=json.dumps({'name': 'astrbot_plugin_group_chat_plus'}).encode(),
    headers=headers,
    method='POST',
)
with urllib.request.urlopen(reload_request, timeout=120) as response:
    reload_body = json.loads(response.read().decode())
print('reload_status=' + str(reload_body.get('status', 'unknown')))

commands_request = urllib.request.Request(
    f'{base}/commands',
    headers={'Authorization': f'Bearer {token}'},
)
with urllib.request.urlopen(commands_request, timeout=20) as response:
    commands_body = json.loads(response.read().decode())
items = commands_body.get('data', {}).get('items', [])
count = sum(
    str(item.get('plugin', '')) == 'astrbot_plugin_group_chat_plus'
    for item in items
)
print(f'plugin_command_count={count}')
PY"
```

Expected: `reload_status=ok`, a non-negative command count, one termination and one load event for `astrbot_plugin_group_chat_plus`, with no AstrBot container restart.

- [ ] **Step 8: Inspect safe runtime evidence**

Read `/volume1/docker/astrbot/data/logs/astrbot.log` and filter the target reload interval for:

```text
astrbot_plugin_group_chat_plus
QUOTED_IMAGE_COLLECTED
QUOTED_IMAGE_FALLBACK
QUOTED_IMAGE_RESOLVE_FAILED
USER_HISTORY_MULTIMODAL_SAVED
ERROR
Traceback
```

Confirm plugin load, command registration, and absence of new target-plugin exceptions. Do not print complete image addresses, Provider configuration, tokens, or keys.

- [ ] **Step 9: Run two authorized-group probes**

Use one authorized QQ group and one fresh image message:

```text
Probe A: quote the image and ask for a short description
Probe B: quote the same image and request a visible edit through the configured image tool
```

For Probe A, verify the request hook reports at least one image and the final model gives an image-grounded answer. For Probe B, verify the edit tool receives at least one source image, returns one image result, and the model provides a natural-language completion. Record only status, image count, dimensions, byte count, duration, and tool status.
