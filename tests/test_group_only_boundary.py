import json
from pathlib import Path
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]


class GroupOnlyBoundaryTest(unittest.TestCase):
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
        self.assertNotIn("Web", metadata)
        self.assertNotIn("Him666233", metadata)

    def test_readme_declares_current_group_chat_scope(self):
        readme_head = "\n".join(
            (REPO_ROOT / "README.md").read_text(encoding="utf-8").splitlines()[:80]
        )

        self.assertIn("当前自用版只面向指定 QQ 群聊场景", readme_head)
        self.assertIn("Web 面板与私聊模块已从运行入口移除", readme_head)
        self.assertNotIn("enable_private_chat", readme_head)
        self.assertNotIn("Web 管理面板怎么用", readme_head)


if __name__ == "__main__":
    unittest.main()
