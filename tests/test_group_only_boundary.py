import json
from pathlib import Path
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]


class GroupOnlyBoundaryTest(unittest.TestCase):
    def setUp(self):
        self.readme_source = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        self.config_reference_source = (
            REPO_ROOT / "docs" / "CONFIG_REFERENCE.md"
        ).read_text(encoding="utf-8")
        self.message_workflow_source = (
            REPO_ROOT / "docs" / "MESSAGE_WORKFLOW.md"
        ).read_text(encoding="utf-8")
        self.project_structure_source = (
            REPO_ROOT / "docs" / "PROJECT_STRUCTURE.md"
        ).read_text(encoding="utf-8")
        self.changelog_source = (REPO_ROOT / "CHANGELOG.md").read_text(
            encoding="utf-8"
        )
        self.metadata_source = (REPO_ROOT / "metadata.yaml").read_text(
            encoding="utf-8"
        )
        self.requirements_source = (REPO_ROOT / "requirements.txt").read_text(
            encoding="utf-8"
        )

    def test_legacy_web_and_private_modules_are_not_in_plugin_root(self):
        self.assertFalse((REPO_ROOT / "web").exists())
        self.assertFalse((REPO_ROOT / "private_chat").exists())
        self.assertTrue((REPO_ROOT / "legacy" / "web").is_dir())
        self.assertTrue((REPO_ROOT / "legacy" / "private_chat").is_dir())

    def test_main_runtime_does_not_register_web_or_private_entrypoints(self):
        source = (REPO_ROOT / "main.py").read_text(encoding="utf-8")

        forbidden_tokens = (
            "PrivateChatMain",
            "EventMessageType.PRIVATE_MESSAGE",
            "on_private_message",
            "enable_private_chat",
            "private_chat_handler",
            "private_command_messages",
            "_is_private_command_message",
            "WebPanelServer",
            "enable_web_panel",
            "web_panel_",
            "_web_server",
        )
        for token in forbidden_tokens:
            self.assertNotIn(token, source)

        self.assertIn("EventMessageType.GROUP_MESSAGE", source)
        self.assertIn("event.is_private_chat()", source)

    def test_schema_exposes_only_group_chat_configuration(self):
        schema = json.loads((REPO_ROOT / "_conf_schema.json").read_text(encoding="utf-8"))

        forbidden_prefixes = ("web_panel", "private_")
        forbidden_exact = {
            "_web_panel_section_header",
            "_private_chat_section_header",
            "_private_image_section_header",
            "enable_private_chat",
        }
        for key in schema:
            self.assertNotIn(key, forbidden_exact)
            self.assertFalse(
                key.startswith(forbidden_prefixes),
                f"schema still exposes removed boundary key: {key}",
            )

        self.assertIn("enable_group_chat", schema)
        self.assertIn("enabled_groups", schema)

    def test_runtime_requirements_exclude_web_and_test_only_dependencies(self):
        requirements = self.requirements_source

        for package_name in ("argon2-cffi", "pytest", "hypothesis"):
            self.assertNotIn(package_name, requirements)
        self.assertIn("pypinyin", requirements)
        self.assertIn("aiohttp", requirements)
        self.assertIn("httpx", requirements)
        self.assertIn("# StepFun HTTP 图片生成与编辑请求", requirements)
        self.assertNotIn("Codex OAuth", requirements)

    def test_utils_header_declares_rhonin_maintenance_scope(self):
        utils_init = (REPO_ROOT / "utils" / "__init__.py").read_text(encoding="utf-8")
        header = "\n".join(utils_init.splitlines()[:8])

        self.assertIn("RhoninSeiei", header)
        self.assertIn("基于 Him666233 原项目修改", header)
        self.assertNotIn("作者: Him666233", header)
        self.assertNotIn("版本: v1.2.1", header)

    def test_readme_runtime_dependency_instructions_match_requirements(self):
        readme = self.readme_source

        self.assertIn("pip install -r requirements.txt", readme)
        self.assertIn("`pypinyin` | >= 0.44.0 | 打字错误生成器（拼音相似性）", readme)
        self.assertIn(
            "`aiohttp` | >= 3.8.0 | AstrBot Dashboard 辅助请求与通用异步 HTTP 会话",
            readme,
        )
        self.assertIn(
            "`httpx` | >= 0.24.0 | StepFun Step Image Edit 2 图片生成与编辑请求",
            readme,
        )

    def test_metadata_declares_rhonin_group_chat_fork(self):
        metadata = self.metadata_source

        self.assertIn("author: RhoninSeiei", metadata)
        self.assertIn(
            "repo: https://github.com/RhoninSeiei/astrbot_plugin_group_chat_plus",
            metadata,
        )
        self.assertIn('astrbot_version: ">=4.24.0,<5"', metadata)
        self.assertIn("support_platforms:", metadata)
        self.assertIn("  - aiocqhttp", metadata)
        self.assertIn("可配置 Codex OAuth 与 StepFun 群聊生图与修图", metadata)
        self.assertNotIn("Web", metadata)
        self.assertNotIn("Him666233", metadata)

    def test_register_metadata_matches_rhonin_group_chat_fork(self):
        source = (REPO_ROOT / "main.py").read_text(encoding="utf-8")

        self.assertIn('"RhoninSeiei"', source)
        self.assertIn('"v1.2.1-rhonin.1"', source)
        self.assertIn("群聊增强插件已加载 - v1.2.1-rhonin.1", source)
        self.assertNotIn("群聊增强插件已加载 - v1.2.1\")", source)
        self.assertIn(
            '"https://github.com/RhoninSeiei/astrbot_plugin_group_chat_plus"',
            source,
        )
        self.assertNotIn(
            '"https://github.com/Him666233/astrbot_plugin_group_chat_plus"',
            source,
        )

    def test_readme_declares_current_group_chat_scope(self):
        readme = self.readme_source
        readme_head = "\n".join(readme.splitlines()[:80])

        self.assertIn("当前自用版只面向指定 QQ 群聊场景", readme_head)
        self.assertIn("Web 面板与私聊模块已从运行入口移除", readme_head)
        self.assertNotIn("enable_private_chat", readme_head)
        self.assertNotIn("Web 管理面板怎么用", readme_head)
        for marker in (
            "image_tool_backend",
            "codex_oauth",
            "openai_oauth/gpt-5.6-sol",
            "StepFun",
            "generate_image()",
        ):
            self.assertIn(marker, readme)

    def test_group_image_backend_documentation_contract(self):
        for marker in (
            "新安装在配置 schema 中默认使用 `image_tool_backend=codex_oauth`",
            "旧配置如果缺少 `image_tool_backend`，运行时会继续使用 StepFun",
            "设置 `image_tool_backend=stepfun`",
            "Provider 负责保存 OAuth 凭据并执行 `image_generation` 请求",
            "Codex OAuth 尺寸采用 `width x height`（宽x高）",
            "StepFun 继续采用 `height x width`（高x宽）",
            "`gcp_step_image_generate` 与 `gcp_step_image_edit`",
            "随后端变化的进度文本、一次图片结果和主模型按当前人格生成的自然语言收尾",
        ):
            self.assertIn(marker, self.readme_source)

        for marker in (
            "新安装会从 schema 取得 `image_tool_backend=codex_oauth`",
            "旧配置缺少 `image_tool_backend` 时，运行时继续使用 StepFun",
            "设置 `image_tool_backend=stepfun` 可以切换回 StepFun",
            "文生图需要 Provider 声明 `image_generate`；修图额外需要 `image_edit`",
            "Provider 负责 OAuth 凭据以及 `image_generation` 请求",
            "Codex OAuth 尺寸采用 `width x height`（宽x高）",
            "StepFun 尺寸继续采用 `height x width`（高x宽）",
            "`gcp_step_image_generate` 与 `gcp_step_image_edit`",
            "随当前后端变化的自然语言进度文本和一次图片结果",
            "当前群人格生成自然语言收尾",
        ):
            self.assertIn(marker, self.config_reference_source)
        self.assertNotIn(
            "必须选择声明 `image_generate` 与 `image_edit` 能力的 Provider",
            self.config_reference_source,
        )

    def test_group_image_history_documentation_contract(self):
        for source in (
            self.readme_source,
            self.config_reference_source,
            self.message_workflow_source,
            self.changelog_source,
        ):
            self.assertNotIn("操作类型、后端显示名和安全状态摘要", source)
            self.assertNotIn("模型上下文与历史保存前清除", source)
            self.assertNotIn("群聊与历史记录不会保存工具协议", source)
            self.assertNotIn("保存前清除工具协议", source)

        for marker in (
            "内部持久历史可按执行顺序保存交错的工具调用记录",
            "内部工具名和已脱敏参数占位",
            "图片工具摘要只包含操作类型、成功或失败状态和安全消息，不包含后端显示名",
            "后续格式化为模型上下文时会过滤这些工具协议块",
            "这些工具协议块不会发送到群聊",
            "Provider ID、凭据、API 地址、原始响应和文件路径不会进入安全摘要或群聊文本",
        ):
            self.assertIn(marker, self.message_workflow_source)
            self.assertIn(marker, self.changelog_source)

        for marker in (
            "内部持久历史可以按实际执行顺序保存交错的工具调用记录",
            "图片工具摘要只记录操作类型、成功或失败状态和安全消息，不记录后端显示名",
            "后续格式化模型上下文时会过滤工具协议块",
        ):
            self.assertIn(marker, self.readme_source)

        for marker in (
            "内部持久历史可以保留交错的工具调用记录、内部工具名和已脱敏参数占位",
            "图片工具摘要只包含操作类型、成功或失败状态和安全消息，不包含后端显示名",
            "格式化后续模型上下文时会过滤工具协议块",
        ):
            self.assertIn(marker, self.config_reference_source)

    def test_group_image_service_structure_documentation_contract(self):
        for marker in (
            "`codex_oauth_image_service.py` | `CodexOAuthImageService`",
            "AstrBot Provider 公共 `generate_image()` 接口",
            "Provider 管理 OAuth 凭据和 `image_generation` 请求",
            "`group_image_service.py` | `GroupImageService`",
            "按 `image_tool_backend` 选择 Codex OAuth 或 StepFun",
            "统一结果、显示名称与异常类型",
        ):
            self.assertIn(marker, self.project_structure_source)


if __name__ == "__main__":
    unittest.main()
