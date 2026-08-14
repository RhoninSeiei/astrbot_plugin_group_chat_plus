# Group Chat Plus 引用图片处理设计

## 背景

QQ 群引用图片消息在 AstrBot 中以顶层 `Reply` 组件进入事件。适配器可能已经把原消息转换到 `Reply.chain`，也可能只保留引用消息编号。完整组件位于 `event.message_obj.message`，`message_str` 只提供扁平文本表示。

当前 `ImageHandler` 只收集顶层 `Image`。`Reply` 仅被视为文本组件，因此被引用图片没有进入 `image_urls`。插件自行构造 `ProviderRequest`，并在 `on_llm_request()` 中用插件保存的图片数组恢复 `req.image_urls`；初始数组为空时，AstrBot 主代理无法补充引用附件。

等待窗口在常规图片处理之前缓存消息，并将 `image_urls` 固定为空。即使常规消息处理支持引用图片，等待窗口内的消息仍会丢失图片。

官方会话保存能够为缓存消息构造 `text + image_url` 内容，但当前用户消息仍按纯文本保存。当前轮可以看图后，后续重新读取会话时仍可能失去图片地址。

## 目标

1. 顶层图片和引用图片使用同一套采集规则，保持消息组件出现顺序、去重和总数限制。
2. 优先读取 `Reply.chain`，仅在内嵌图片缺失或解析异常时调用 AstrBot 公共引用图片提取器。
3. 空 `image_to_text_provider_id` 时，把解析后的引用图片传给正式多模态模型。
4. 非空 `image_to_text_provider_id` 时，把同一组解析结果交给指定图片转文字提供商，并把描述放在对应图片或引用标记附近。
5. 等待窗口缓存保留引用图片信息，并遵循相同的多模态或图片转文字配置。
6. 当前用户消息以多模态内容保存到官方会话，后续读取仍能发现图片。
7. 单张图片解析失败时继续处理正文与其余图片。
8. 日志仅包含计数、来源、处理阶段和异常类型，不记录完整 URL、本地文件内容或 Base64。

## 范围

本次只修改 `astrbot_plugin_group_chat_plus`。AstrBot Core、OAuth Provider、Matoi CC、NapCat 和 aiocqhttp 适配器保持原状。

`reply_handler.py`、`PLUGIN_IMAGE_URLS` 与 `on_llm_request()` 已能传递非空图片数组，功能代码保持原状，仅增加回归测试。

## 设计

### 统一图片记录

在 `utils/image_handler.py` 中增加不可变内部记录：

```python
@dataclass(frozen=True)
class ResolvedMessageImage:
    url: str
    source: str
    component_index: int
```

`source` 只使用以下固定值：

1. `top_level`
2. `quoted_embedded`
3. `quoted_fetched`

`component_index` 指向顶层消息组件。顶层图片指向自身位置，引用图片指向对应 `Reply` 位置。图片转文字模式据此把描述插入原消息的相应位置。

公开给插件内部调用的采集接口为：

```python
@staticmethod
async def collect_message_images(
    event: AstrMessageEvent,
    message_chain: Optional[List[BaseMessageComponent]] = None,
    max_images: int = 10,
) -> List[ResolvedMessageImage]:
    ...
```

该方法只负责图片发现、解析、排序、去重和数量限制，不执行图片转文字，也不改变消息正文。

### 采集算法

采集器按顶层消息组件顺序执行：

1. 遇到顶层 `Image` 时调用 `convert_to_file_path()`，成功后加入结果。
2. 遇到 `Reply` 时读取 `Reply.chain` 中的直接 `Image`，并逐张解析。
3. 内嵌图片全部解析成功时使用内嵌结果，不调用公共提取器。
4. 内嵌图片为空时调用 `extract_quoted_message_images(event, reply_component)`。
5. 内嵌图片部分解析失败时调用公共提取器重新解析整条引用。公共提取器返回有效结果时使用完整返回值；公共提取器也失败时保留已经解析成功的内嵌图片。
6. 公共提取器负责处理引用编号补取、转发节点、HTTP 地址、本地文件和 Base64。
7. 每个有效地址首次出现时加入结果，重复地址忽略。
8. 达到 `max_images` 后停止继续采集；非正数上限返回空列表。

部分内嵌解析失败时使用公共提取器的完整结果，可以维持被引用消息内部的原始图片顺序。公共提取器失败后才使用部分内嵌结果。

### 正文分析

`process_message_images()` 先调用 `collect_message_images()`，再根据返回记录判断消息是否包含图片。`_analyze_message()` 继续负责文字检测，不再承担图片解析。

`_extract_text_only()` 保留 `Reply` 的现有文本标记。引用图片存在时，正文至少包含 `[引用消息]` 或对应引用文本，正式模型能够区分当前文字与被引用内容。

图片处理关闭、应用范围未命中、图片解析全部失败等分支仍保留正文。纯图片消息只有在现有配置明确要求过滤时才丢弃。

### 多模态模式

`image_to_text_provider_id` 为空时：

```python
image_urls = [item.url for item in resolved_images]
```

`process_message_images()` 继续返回现有四元组：

```python
(should_continue, processed_text, image_urls, image_retained)
```

只要至少一张图片成功解析，`image_retained=True`。后续缓存、`ReplyHandler.generate_reply()`、`PLUGIN_IMAGE_URLS` 与 `on_llm_request()` 使用现有机制传递图片。

### 图片转文字模式

`image_to_text_provider_id` 非空时，`_convert_images_to_text()` 改为接收 `List[ResolvedMessageImage]`，不再重复调用图片组件的 `convert_to_file_path()`：

```python
async def _convert_images_to_text(
    message_chain: List[BaseMessageComponent],
    context: Context,
    provider_id: str,
    prompt: str,
    resolved_images: List[ResolvedMessageImage],
    timeout: int = 60,
    image_description_cache: Optional[ImageDescriptionCache] = None,
) -> Optional[str]:
    ...
```

每张图片继续独立使用现有缓存、Provider 调用与超时控制。描述按 `component_index` 分组：

1. 顶层图片在原图片位置写入 `[图片内容: ...]`。
2. 引用图片先保留 `Reply` 文本，再依次写入 `[引用图片内容: ...]`。
3. 同一引用包含多张图片时保持采集顺序。
4. 单张转换失败时写入 `[图片]` 或 `[引用图片]`，其余成功描述继续保留。
5. 全部转换失败时沿用现有失败处理，保留正文并移除不可用图片。

图片描述缓存键继续使用解析后的图片地址。调试日志只输出图片序号和缓存命中状态。

### 等待窗口

`main.py::_maybe_intercept_for_wait_window()` 在构造缓存消息前调用 `ImageHandler.collect_message_images()`。

空图片转文字提供商模式：

1. `processed_text` 保留当前正文。
2. 缓存消息的 `image_urls` 保存解析后的地址。
3. 等待窗口结束后，现有图片合并逻辑把这些地址加入正式请求。

非空图片转文字提供商模式：

1. 保留现有平台图片描述与本地描述缓存的优先级。
2. 平台描述与本地缓存均未提供有效描述时，使用统一解析结果调用配置的图片转文字提供商。
3. 转换成功后缓存文字描述，`image_urls` 为空。
4. 转换失败时保留正文；纯图片消息沿用现有占位或过滤规则。

等待窗口中的引用图片与普通即时消息因此使用相同的图片发现规则，同时保留现有节省调用次数的处理顺序。

### 官方会话历史

`ContextManager` 增加统一内容构造器：

```python
@staticmethod
def build_user_history_content(
    text: str,
    image_urls: Optional[List[str]] = None,
):
    ...
```

无有效图片时返回原文本字符串。有图片时返回：

```python
[
    {"type": "text", "text": text},
    {"type": "image_url", "image_url": {"url": image_url}},
]
```

构造器过滤空地址并按首次出现顺序去重。缓存消息与当前用户消息共用该方法。

`save_to_official_conversation_with_cache()` 增加可选参数：

```python
user_image_urls: Optional[List[str]] = None
```

普通发送后保存、重复消息保护保存和 agent 最终保存补偿分支都从 `_message_cache_snapshots` 中取得当前消息图片地址，并通过关键字参数传入。第二阶段只转换等待窗口缓存，不传当前用户图片。

### 错误处理与日志

固定日志事件包括：

1. `QUOTED_IMAGE_COLLECTED`：记录顶层、内嵌引用、远程补取和去重后的数量。
2. `QUOTED_IMAGE_FALLBACK`：记录触发原因 `missing_embedded` 或 `embedded_resolve_failed`。
3. `QUOTED_IMAGE_RESOLVE_FAILED`：记录来源、组件序号和异常类名。
4. `USER_HISTORY_MULTIMODAL_SAVED`：记录当前用户消息保存的图片数量。

日志不包含图片地址、引用消息正文、群号、用户号、Provider 配置和认证信息。现有调试日志中输出完整图片地址的代码同步改为安全计数日志。

## 测试

### 图片采集单元测试

新增 `tests/test_image_handler_quoted_images.py`，覆盖：

1. 顶层 `Image` 正常解析。
2. `Reply.chain=[Image]` 保留引用图片。
3. 内嵌图片完整成功时公共提取器调用次数为零。
4. 空 `Reply.chain` 通过公共提取器取得图片。
5. 内嵌图片部分失败时使用公共提取器完整结果。
6. 公共提取器失败时保留成功的内嵌图片。
7. 顶层与引用图片重复时只保留一份。
8. 多个 `Reply` 和顶层图片保持组件出现顺序。
9. 所有来源共享 `max_images` 上限。
10. 单张解析失败时正文与其余图片继续处理。

### 两种处理模式

同一测试文件验证：

1. 空 Provider 配置返回引用图片 URL。
2. 非空 Provider 配置接收同一图片 URL。
3. 引用描述位于对应 `Reply` 文本之后。
4. 多张引用图片描述顺序保持原状。
5. 单张图片转文字失败时其余描述仍进入正文。
6. 全部转换失败时保留文本分支。

### 等待窗口与正式请求

扩展等待窗口测试，验证缓存消息保存 `image_urls`，图片转文字模式保存描述。扩展请求测试，验证 `on_llm_request()` 执行后 `req.image_urls` 仍包含引用图片。

测试必须覆盖普通成功分支。仅依赖异常补偿或最终保存补偿取得通过不计为有效验证。

### 历史保存

扩展 `tests/test_multimodal_history_content.py`，覆盖：

1. 当前用户消息保存为 `text + image_url`。
2. 无图片时继续保存字符串内容。
3. 空地址与重复地址被过滤。
4. 缓存消息和当前消息同时保存时顺序正确。
5. 普通发送、重复消息保护和 agent 最终保存补偿分支均传入当前图片。
6. 保存后的多模态内容能够被现有规范化方法重新读取为文本和图片标记。

## 验证与发布

本地验证包括相关单元测试、全量 `unittest`、配置 JSON 校验、变更 Python 文件编译和 `git diff --check`。

生产发布只同步本次修改文件到 `/volume1/docker/astrbot/data/plugins/astrbot_plugin_group_chat_plus`。容器内编译通过后，通过 Dashboard API 重载目标插件，不重启 AstrBot 容器。

运行验证使用授权 QQ 群完成两次最小探针：引用图片询问内容，以及引用图片调用编辑工具。日志只检查图片数量、请求图片数组数量、工具状态和回复状态。
