import json
from pathlib import Path
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]


class GroupOnlyBoundaryTest(unittest.TestCase):
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
        requirements = (REPO_ROOT / "requirements.txt").read_text(encoding="utf-8")

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
        readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")

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
        metadata = (REPO_ROOT / "metadata.yaml").read_text(encoding="utf-8")

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
        readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
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


if __name__ == "__main__":
    unittest.main()
