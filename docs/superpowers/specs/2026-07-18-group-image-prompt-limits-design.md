# Group Chat Plus 图片提示词后端专属上限设计

## 目标

让群聊图片工具根据当前图片后端使用不同的提示词字符上限。Codex OAuth 使用 2048 字符，StepFun Step Image Edit 2 继续使用 512 字符。工具说明、统一图片服务和后端适配器应表达相同的限制。

## 范围

本次修改涵盖以下运行代码：

1. `utils/codex_oauth_image_service.py`
2. `utils/group_image_service.py`
3. `main.py` 中的 `gcp_step_image_generate` 与 `gcp_step_image_edit` 工具说明

StepFun 的 512 字符限制保持原值。Provider、OAuth 配置、图片尺寸、工具超时、进度消息、图片发送和自然语言收尾均保持现有行为。

## 实现设计

`CodexOAuthImageService.MAX_PROMPT_CHARS` 从 512 调整为 2048。长度错误消息引用该常量，避免常量与提示文本再次产生差异。

`GroupImageService` 增加 `max_prompt_chars()`，按照 `image_tool_backend` 返回当前后端适配器声明的上限：

1. `codex_oauth` 返回 `CodexOAuthImageService.MAX_PROMPT_CHARS`。
2. `stepfun` 返回 `StepImageService.MAX_PROMPT_CHARS`。

统一图片服务在调用后端前完成一次字符数检查，使工具层能够获得当前后端对应的用户错误。两个后端适配器继续保留各自校验，确保绕过统一服务调用时仍具有相同保护。

两个内部 LLM 工具继续沿用既有名称。工具参数说明改为同时列出 Codex OAuth 2048 字符与 StepFun 512 字符，正式回复模型据此保留较完整的 Codex 图像描述，并在 StepFun 模式下遵守较短上限。

## 错误处理

空提示词继续返回现有用户错误。超过当前后端上限时，错误消息显示实际字符数上限。Provider 调用尚未开始，因此不会产生图片请求、费用或额外发送行为。

## 测试

单元测试覆盖以下行为：

1. Codex OAuth 接受恰好 2048 字符的提示词。
2. Codex OAuth 拒绝 2049 字符，并且错误消息包含 2048。
3. `GroupImageService.max_prompt_chars()` 按当前后端分别返回 2048 和 512。
4. 统一图片服务在 Codex OAuth 模式下接受 2048 字符并拒绝 2049 字符。
5. 统一图片服务在 StepFun 模式下拒绝 513 字符。
6. 两个工具说明同时包含 Codex OAuth 2048 字符与 StepFun 512 字符。

验证顺序为相关图片服务测试、图片工具集成测试、全量单元测试、变更 Python 文件编译和 `git diff --check`。

## 生产验证

本地验证通过后，仅同步本次运行代码与文档到生产插件目录。容器内编译变更 Python 文件，通过 Dashboard API 重载 `astrbot_plugin_group_chat_plus`，再检查插件加载日志、命令注册数量和图片工具状态。生产验证输出禁止包含 Provider 配置、OAuth 凭据、Dashboard token、完整提示词或图片原始数据。

## 验收条件

1. Codex OAuth 图片提示词可达到 2048 字符。
2. StepFun 图片提示词仍限制为 512 字符。
3. 工具说明与运行时校验一致。
4. 图片生成、图片编辑、进度提示、图片发送和主模型自然语言收尾保持现有行为。
