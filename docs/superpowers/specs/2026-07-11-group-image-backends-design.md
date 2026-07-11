# Group Chat Plus 多后端群聊生图设计

## 背景与目标

Group Chat Plus 当前通过 `StepImageService` 调用 StepFun `step-image-edit-2`，为群聊提供文生图和当前消息图片编辑能力。生产 AstrBot 同时部署了 Codex OAuth Provider；该 Provider 公开 `generate_image()`，ImgFlow 已经通过这一公共接口完成文生图与参考图编辑。

本次改造在保留现有 StepFun 能力的基础上增加 Codex OAuth 图像后端，并允许在 AstrBot 插件配置面板选择后端与 Provider。生产默认使用 `codex_oauth`，Provider 默认值为 `openai_oauth/gpt-5.6-sol`；StepFun 作为可切换的备用后端。

Codex OAuth Provider 的请求协议显式指定 Codex 主模型并启用 `image_generation` 工具，未提供可由调用方指定的 `gpt-image-2` 字段。因此配置、日志和用户可见文本统一称为“Codex OAuth 图像生成”。实际图像模型由 Codex 服务端管理。

## 范围

本次包含：

1. 文生图与单张参考图编辑的多后端选择。
2. Codex OAuth Provider ID、Codex 主模型、默认尺寸和超时配置。
3. 既有 StepFun 配置与调用行为兼容。
4. 后端相关进度文本、工具结果摘要和历史记录文本去除固定 StepFun 假设。
5. 单元测试、集成测试、生产热重载及两种后端真实调用验证。

本次不包含：

1. Group Chat Plus 依赖 ImgFlow 插件实例或 ImgFlow 任务存储。
2. 读取、复制或持久化 Codex OAuth token、账号 ID、请求头及刷新状态。
3. 多参考图编排、批量出图、质量参数和输出格式参数。
4. Codex OAuth 失败后自动调用 StepFun。后端切换由配置明确决定，避免一次用户请求产生两次计费调用或两组图片。
5. 更改现有 LLM 工具名称。`gcp_step_image_generate` 与 `gcp_step_image_edit` 继续作为内部兼容接口。

## 方案选择

采用 Group Chat Plus 自有图片服务门面，并通过 AstrBot Provider 的公共 `generate_image()` 接口调用 Codex OAuth。

未采用调用 ImgFlow 内部执行器的方案，因为该方式会引入插件加载顺序、内部模块版本和 ImgFlow 任务模型依赖。未采用读取 Provider OAuth 配置后自行请求的方案，因为鉴权、刷新和账号状态属于 Provider 的职责。

## 配置契约

保留 `enable_step_image_tools` 作为既有总开关，配置面板描述改为“启用群聊生图与修图工具”。保留字段名可以避免现有生产配置在升级后失效。

新增以下字段：

| 字段 | 类型 | 默认值 | 语义 |
| --- | --- | --- | --- |
| `image_tool_backend` | string | `codex_oauth` | 可选 `codex_oauth`、`stepfun` |
| `codex_oauth_image_provider_id` | string | `openai_oauth/gpt-5.6-sol` | AstrBot Provider ID，使用 `select_provider` |
| `codex_oauth_image_model` | string | `gpt-5.6-sol` | 传给 Provider 的 Codex 主模型 |
| `codex_oauth_image_default_size` | string | `1024x1024` | 可选 `1024x1024`、`1536x1024`、`1024x1536` |
| `codex_oauth_image_timeout` | int | `300` | 调用超时，限制为 30 至 900 秒 |

现有 `step_image_provider_id`、`step_image_model`、`step_image_default_size`、`step_image_timeout`、`step_image_proxy`、`step_image_cfg_scale`、`step_image_steps`、`step_image_seed` 和 `step_image_text_mode` 继续只服务 StepFun 后端。

兼容规则如下：

1. 新配置默认写入 `image_tool_backend=codex_oauth`。
2. 旧配置缺少 `image_tool_backend` 时使用 `stepfun`，确保升级瞬间维持原有生产行为。
3. 生产同步后通过 Dashboard 配置接口显式写入 `image_tool_backend=codex_oauth`，再进行插件热重载。
4. Codex OAuth Provider ID 必须显式存在，禁止回退到当前会话 Provider，避免群模型变化影响图片后端。
5. 配置值只保存 Provider ID、模型、尺寸和超时，不保存任何鉴权数据。

## 组件设计

### 图片服务门面

新增 `utils/group_image_service.py`，作为 `main.py` 使用的唯一图片服务入口。门面负责：

1. 读取 `image_tool_backend` 并创建对应后端。
2. 提供统一的 `is_enabled()`、`generate()`、`edit()`、`display_name()` 和默认尺寸查询。
3. 将后端异常映射为统一的用户输入错误、配置错误和 Provider 调用错误。
4. 返回统一结果对象，字段包括本地文件、操作类型、后端名称、媒体类型和修订提示词。

建议接口：

```python
@dataclass(frozen=True)
class GroupImageResult:
    path: str
    mode: str
    backend: str
    media_type: str = "image/png"
    revised_prompt: str = ""


class GroupImageService:
    @staticmethod
    def is_enabled(config: dict) -> bool: ...

    def display_name(self) -> str: ...

    async def generate(self, *, prompt: str, size: str) -> GroupImageResult: ...

    async def edit(self, *, prompt: str, image_path: str) -> GroupImageResult: ...
```

### StepFun 后端

现有 `utils/step_image_service.py` 保持 StepFun 专用职责。现有 Provider 配置合并、API Key 获取、HTTP 请求、尺寸别名、Base64 结果写入和错误脱敏继续保留。

`GroupImageService` 在 `stepfun` 模式下调用现有 `StepImageService`，再把 `StepImageResult` 转换为统一结果。该方式减少既有 StepFun 行为变化，并保留当前测试价值。

### Codex OAuth 后端

新增 `utils/codex_oauth_image_service.py`。该服务只使用 AstrBot Provider 公共接口：

```python
provider = context.get_provider_by_id(provider_id)
images = await provider.generate_image(
    prompt=prompt,
    model=model,
    size=size,
    n=1,
    reference_images=reference_images or None,
    action="edit" if reference_images else "generate",
)
```

调用前验证：

1. Provider 存在。
2. `provider.capabilities` 为字典且 `image_generate` 为真。
3. 编辑请求还要求 `image_edit` 为真。
4. `generate_image` 可调用。
5. Provider 返回至少一个包含有效本地文件的结果对象。

Codex OAuth 服务在调用期间通过异步锁临时覆盖 Provider `timeout`，结束后恢复原值。锁按 Provider 实例隔离，避免同一 Provider 的并发调用互相覆盖超时设置。

Provider 返回的 `path` 由 Codex OAuth Provider 写入 AstrBot 数据目录。Group Chat Plus 仅验证文件存在并发送该文件，不复制原始响应，不读取 OAuth 私有字段。

## 尺寸语义

StepFun 继续使用现有高乘宽语义和别名映射。

Codex OAuth 使用宽乘高语义：

| 用户输入 | Codex OAuth 尺寸 |
| --- | --- |
| `1:1`、`square`、`1024x1024` | `1024x1024` |
| `16:9`、`landscape`、`1920x1080`、未识别的横屏描述 | `1536x1024` |
| `9:16`、`portrait`、`1080x1920`、未识别的竖屏描述 | `1024x1536` |

工具参数中的空尺寸使用当前后端对应的默认尺寸。Codex OAuth 首期只接受上述三个尺寸，避免把 StepFun 的高乘宽值传入宽乘高接口。

## 群聊消息处理

现有工具调用顺序保持：

1. 正式回复模型选择图片工具并提交参数。
2. 插件发送一次包含当前后端名称的进度消息。
3. 图片后端执行生成或编辑。
4. 插件发送一次图片结果。
5. 工具向 Agent 返回脱敏后的成功或失败摘要。
6. 正式回复模型依据工具摘要和群人格生成一句自然语言收尾。
7. 历史记录只保存操作类型、后端显示名和安全状态摘要。

Codex OAuth 进度文本使用“正在用 OpenAI Codex 图像生成服务生成图片”或“正在用 OpenAI Codex 图像生成服务编辑这张图”。StepFun 继续使用“阶跃星辰 Step Image Edit 2”。

系统提示中的能力描述改为后端无关文本，并要求模型根据工具结果生成自然语言回复。工具参数、Provider ID、模型内部状态、临时文件和原始响应不得进入用户可见内容。

## 错误处理

统一异常类别：

1. `GroupImageUserError`：空提示词、尺寸无效、编辑请求缺少图片。
2. `GroupImageConfigError`：后端值无效、Provider 不存在、能力字段缺失、方法缺失。
3. `GroupImageProviderError`：超时、网络异常、Provider 返回空结果或结果文件缺失。

日志允许记录后端名称、操作类型、群号、耗时、异常类型和脱敏后的短消息。日志禁止记录 OAuth token、API Key、请求头、完整 Provider 配置、完整原始响应和提示词全文。

失败时不发送图片，工具返回安全摘要，正式回复模型依据摘要向群聊说明本次生成失败。自动切换到 StepFun 不在本次范围内。

## 测试设计

### 服务测试

1. 旧配置缺少 `image_tool_backend` 时选择 StepFun。
2. 新配置选择 `codex_oauth` 时创建 Codex OAuth 服务。
3. Codex 文生图向假 Provider 传递正确的 prompt、model、size、n、reference_images 和 action。
4. Codex 编辑传递参考图文件及 `action=edit`。
5. Provider 能力缺失、方法缺失、空结果和文件缺失分别映射为配置或调用错误。
6. Provider 超时在调用期间生效，完成与异常后均恢复。
7. Codex 尺寸别名与三个允许值正确归一化。
8. 错误文本不包含测试 token、Provider 配置或本地文件绝对位置。

### 主流程测试

1. ToolPolicy 在任一图片后端启用时开放现有两个工具。
2. 进度消息根据后端显示名生成，且每次调用只发送一次。
3. 图片结果只发送一次。
4. 成功与失败摘要返回 Agent，最终自然语言回复保持可发送。
5. 保存历史时清除工具协议文本、参数和文件位置。
6. 配置 schema 可由 JSON 解析，并包含后端、Provider、模型、尺寸和超时约束。

### 回归验证

运行图片服务、工具集成、工具泄露防护、多模态历史和 ToolPolicy 定向测试，再运行完整 `unittest` 测试集。随后解析 `_conf_schema.json`，编译变更 Python 文件并执行 `git diff --check`。

## 生产发布与验收

1. 本地验证全部通过后，备份生产插件中本次变更文件。
2. 只同步 Group Chat Plus 的变更文件，不改动 ImgFlow、Codex OAuth Provider 或 AstrBot 核心。
3. 在生产容器中使用 `compile()` 校验变更 Python 文件。
4. 通过 Dashboard API 保存 `image_tool_backend=codex_oauth`、Provider ID、模型、尺寸与超时。
5. 调用 `POST /api/plugin/reload`，请求体固定为 `{"name":"astrbot_plugin_group_chat_plus"}`。
6. 检查目标插件单次卸载和加载、插件启用状态、命令注册以及重载窗口内的错误。
7. 执行 Codex OAuth 最小文生图，记录状态、尺寸和字节数。
8. 执行 Codex OAuth 单图编辑，记录状态、尺寸和字节数。
9. 临时切换到 StepFun，执行最小文生图，确认备用后端仍可工作。
10. 恢复 `codex_oauth` 作为生产默认后端，并再次确认插件状态和日志。

生产验收过程中禁止输出 Provider 配置、OAuth token、API Key、Dashboard JWT、原始响应和完整提示词。

## 完成标准

以下条件全部满足后，本次功能视为完成：

1. 配置面板可选择 `codex_oauth` 或 `stepfun`，并可指定对应 Provider。
2. 生产默认使用 `openai_oauth/gpt-5.6-sol` 的 Codex OAuth 图像生成。
3. 群聊文生图和当前消息单图编辑均可通过 Codex OAuth 成功发送一次图片。
4. StepFun 文生图仍可通过配置切换成功调用。
5. 图片工具命中后保留进度消息、图片结果和人格化自然语言收尾。
6. 群消息、模型上下文和普通日志中均无凭据、原始请求、工具协议或本地文件位置泄露。
7. 本地完整测试、schema 解析、Python 编译和差异检查全部通过。
8. 生产仅执行目标插件热重载，重载窗口内无目标插件 Traceback 或 ERROR。
