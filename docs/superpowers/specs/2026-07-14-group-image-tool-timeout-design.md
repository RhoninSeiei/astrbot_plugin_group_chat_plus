# Group Chat Plus 图片工具独立超时设计

## 目标

让 `astrbot_plugin_group_chat_plus` 的图片生成与编辑工具使用图片后端配置的超时时间。当前 Codex OAuth 后端配置为 300 秒，因此 `gcp_step_image_generate` 与 `gcp_step_image_edit` 的 AstrBot 外层工具执行上限也应为 300 秒。其他插件和普通工具继续使用 AstrBot 全局 `provider_settings.tool_call_timeout`，线上当前值为 180 秒。

## 范围

本次只处理以下两个本地 LLM 工具：

1. `gcp_step_image_generate`
2. `gcp_step_image_edit`

不修改 AstrBot Core，不修改全局工具超时，不修改 Matoi、Pixiv、MCP、Skills、搜索或计算机控制工具。

## 设计

新增独立的图片工具超时覆盖模块。模块在插件加载时包装 AstrBot 本地工具执行器的 `_execute_local` 类方法，并按工具名决定是否传入图片工具专用超时。

对于 Group Chat Plus 图片工具，超时值按当前图片后端读取：

1. `image_tool_backend=codex_oauth` 时使用 `codex_oauth_image_timeout`。
2. `image_tool_backend=stepfun` 时使用 `step_image_timeout`。
3. 配置值必须是有限正数；配置无效时跳过覆盖并记录不含敏感信息的警告，保留 AstrBot 全局行为。

对于其他工具，包装器原样传递调用参数，不改变全局超时。

如果调用方已经为 Group Chat Plus 图片工具显式传入更长的超时时间，包装器保留较长值；当前线上全局值 180 秒仍会提升为后端配置的 300 秒。这样可以避免覆盖后安装的其他组件无意缩短执行上限。

插件实例保存安装句柄。`terminate()` 使用该句柄撤销本实例注册，防止热重载后遗留旧状态。同一插件短暂存在多个实例时，图片工具使用所有有效注册值中的最大值；最后一个句柄撤销后恢复安装前的描述符。实现采用独立状态属性和锁，并兼容其他插件已经包装 `_execute_local` 的情况；撤销时只恢复本插件安装前捕获的描述符，不覆盖其他插件仍在使用的包装器。

## 调用过程

1. 插件初始化并解析图片后端配置。
2. `initialize()` 注册 Group Chat Plus 图片工具超时覆盖。
3. 正式回复模型调用 `gcp_step_image_generate` 或 `gcp_step_image_edit`。
4. 包装器识别工具名，将外层工具执行超时设为图片后端超时。
5. 图片服务自身继续保留同值的 `asyncio.wait_for`，两层超时含义一致。
6. 插件重载或卸载时，`terminate()` 撤销注册。

## 异常处理

安装覆盖失败时记录固定警告，插件其他群聊能力继续运行。撤销失败时同样记录固定警告，不阻断已有终止清理。日志只包含错误类型，不输出 Provider 配置、令牌、代理认证信息或图片文件内容。

## 测试

新增单元测试覆盖以下行为：

1. `gcp_step_image_generate` 使用 300 秒。
2. `gcp_step_image_edit` 使用 300 秒。
3. 普通工具保留运行上下文中的 180 秒。
4. 安装与撤销后恢复原执行器。
5. 多个插件实例注册时取当前有效注册值，并在最后一个句柄撤销后恢复。
6. 与已有包装器叠加时，撤销 Group Chat Plus 覆盖不会移除已有包装器。
7. 在本覆盖之后安装的包装器按两种卸载顺序均能保留各自状态并最终恢复原始描述符。
8. 两个独立模块实例模拟热重载时共享注册状态，最后一个句柄撤销后恢复原始描述符。
9. 调用方显式传入 420 秒时保留 420 秒，不缩短为 300 秒。
7. 生命周期源码测试确认 `initialize()` 安装、`terminate()` 撤销。

验证顺序为聚焦单元测试、Group Chat Plus 全量测试、配置 JSON 解析、变更 Python 文件编译和 `git diff --check`。生产验证仅同步本插件变更文件，通过 Dashboard API 重载 `astrbot_plugin_group_chat_plus`，检查加载日志、有效图片工具超时值与命令注册。最后执行一次最小图片生成请求，验证图片工具正常完成或由 300 秒上限处理；验证输出不包含凭据。

## 验收条件

1. Group Chat Plus 两个图片工具的外层执行超时与当前图片后端配置一致，线上 Codex OAuth 为 300 秒。
2. 非 Group Chat Plus 图片工具继续使用 AstrBot 全局 180 秒。
3. 热重载不会累积包装器或失去原执行器。
4. 图片成功、失败和超时后仍由正式回复模型生成自然语言收尾。
