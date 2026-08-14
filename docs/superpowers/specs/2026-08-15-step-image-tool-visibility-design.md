# StepImage 群聊工具可见性设计

## 背景

`gcp_step_image_generate` 与 `gcp_step_image_edit` 通过 AstrBot 的 LLM 工具装饰器注册，因此会进入平台构造的全量工具集合。当前执行函数会检查图片服务开关、群聊来源和 `enabled_groups`，但 `on_llm_request` 对普通主模型请求会在插件标记检查后提前返回。微信私聊、QQ 私聊和未授权群聊因此仍可能看到两项群聊专用工具，并在模型调用后收到执行期拒绝。

## 目标

两项 StepImage 工具只在图片服务启用且当前 UMO 属于授权群聊时进入模型请求。其他会话在模型推理前移除两项工具，ImgFlow、搜索和其他平台工具保持原有顺序与可执行状态。

执行函数中的 `_step_image_guard()` 继续保留，用于处理框架绕行、异常调用和运行期间配置变化。

## 设计

### 授权来源

工具可见性与执行鉴权共同使用以下条件：

1. `GroupImageService.is_enabled(self.step_image_config)` 返回真。
2. `_is_step_image_enabled_for_event(event)` 返回真。

第二项继续从事件字段、消息对象、`unified_msg_origin` 和 `session_id` 识别群号，并使用 `enabled_groups` 判断授权范围。私聊事件缺少群聊来源时返回假。

### 普通主模型请求

`on_llm_request` 在读取 `PLUGIN_REQUEST_MARKER` 之前计算 StepImage 可见性。权限未通过时，复制当前 `req.func_tool`，从副本移除 `STEP_IMAGE_TOOL_NAMES`，再写回请求。复制操作避免修改 AstrBot 全局工具管理器或其他并发请求可能共享的容器。

普通主模型请求随后仍可按现有逻辑提前返回，但传给模型的请求工具集合已经排除群聊专用工具。

### 插件正式回复请求

正式回复准备阶段构造 `ToolPolicy` 时，将 `allow_step_image` 设置为当前事件的完整授权结果。该值决定提示词工具列表和 `PLUGIN_VISIBLE_TOOL_NAMES`。

`on_llm_request` 合并 `PLUGIN_FUNC_TOOL` 时继续使用可见工具名过滤插件工具副本。合并完成后，再对最终 `req.func_tool` 应用当前事件的 StepImage 权限，防止完整插件工具集合重新加入两项专用工具。

### 工具容器兼容

`ToolPolicy` 增加复制后按名称排除工具的方法，复用现有容器克隆和过滤能力，并兼容以下形式：

1. `tools` 与 `remove_tool()`。
2. `func_list` 与 `remove_func()`。
3. 只有 `tools` 或 `func_list` 属性的旧式容器。
4. `None` 工具容器。

方法返回过滤后的容器和移除工具名，便于请求接入和日志验证。无关工具保持输入顺序。

### 日志

请求级过滤实际移除工具时记录结构化日志，仅包含平台名、私聊状态和移除工具名。日志不包含 UMO、群号、提示词、Provider 配置、访问令牌或密钥。

## 测试

单元测试覆盖两类工具容器、`None` 输入、原始容器保持原状，以及无关工具顺序保持原状。

请求级测试覆盖以下场景：

1. 授权 QQ 群保留 `gcp_step_image_generate`、`gcp_step_image_edit`、ImgFlow 和普通工具。
2. 未授权 QQ 群移除两项 GCP 工具，保留 ImgFlow 和普通工具。
3. 微信私聊移除两项 GCP 工具，保留 ImgFlow 和普通工具。
4. QQ 私聊移除两项 GCP 工具，保留 ImgFlow 和普通工具。
5. 图片服务关闭时，授权群也移除两项 GCP 工具。
6. 插件自身请求完成工具合并后仍满足相同权限规则。
7. 人格工具名单的普通允许分支继续生效，测试不得依赖执行期拒绝文本取得通过。

## 生产验证

本地相关测试、全量测试、Python 编译和 `git diff --check` 通过后，仅同步本次修改文件到生产插件目录。生产容器内编译修改后的 Python 文件，通过 Dashboard API 重载 `astrbot_plugin_group_chat_plus`，随后检查插件加载、命令注册和目标日志。

运行验证至少确认一个授权群请求保留两项 GCP 工具，以及一个私聊请求的最终工具集合排除两项 GCP 工具。验证过程不输出 Provider 配置和凭据。
