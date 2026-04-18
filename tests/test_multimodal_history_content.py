import asyncio
import importlib.util
import pathlib
import re
import sys
import types
import unittest
from enum import Enum


REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
UTILS_DIR = REPO_ROOT / "utils"


class _Logger:
    def info(self, *_args, **_kwargs):
        pass

    def warning(self, *_args, **_kwargs):
        pass

    def error(self, *_args, **_kwargs):
        pass


class _MessageType(Enum):
    OTHER_MESSAGE = "OtherMessage"
    GROUP_MESSAGE = "GroupMessage"
    FRIEND_MESSAGE = "FriendMessage"


class _MessageMember:
    def __init__(self, user_id="", nickname=""):
        self.user_id = user_id
        self.nickname = nickname


class _AstrBotMessage:
    def __init__(self):
        self.message_str = ""
        self.platform_name = ""
        self.timestamp = 0
        self.type = _MessageType.OTHER_MESSAGE
        self.group_id = None
        self.self_id = ""
        self.session_id = ""
        self.message_id = ""
        self.sender = None


class _Plain:
    def __init__(self, text=""):
        self.text = text


class _AstrMessageEvent:
    pass


def _load_context_manager_module():
    package_name = "group_chat_plus_utils_test"
    package_module = types.ModuleType(package_name)
    package_module.__path__ = [str(UTILS_DIR)]
    sys.modules[package_name] = package_module

    astrbot_module = types.ModuleType("astrbot")
    astrbot_api_module = types.ModuleType("astrbot.api")
    astrbot_api_module.logger = _Logger()
    astrbot_api_all_module = types.ModuleType("astrbot.api.all")
    astrbot_api_all_module.logger = astrbot_api_module.logger
    astrbot_api_all_module.AstrBotMessage = _AstrBotMessage
    astrbot_api_all_module.MessageMember = _MessageMember
    astrbot_api_all_module.MessageType = _MessageType
    astrbot_api_all_module.AstrMessageEvent = _AstrMessageEvent
    astrbot_message_components_module = types.ModuleType(
        "astrbot.api.message_components"
    )
    astrbot_message_components_module.Plain = _Plain

    sys.modules["astrbot"] = astrbot_module
    sys.modules["astrbot.api"] = astrbot_api_module
    sys.modules["astrbot.api.all"] = astrbot_api_all_module
    sys.modules["astrbot.api.message_components"] = astrbot_message_components_module

    session_guard_spec = importlib.util.spec_from_file_location(
        f"{package_name}._session_guard",
        UTILS_DIR / "_session_guard.py",
    )
    session_guard_module = importlib.util.module_from_spec(session_guard_spec)
    sys.modules[session_guard_spec.name] = session_guard_module
    session_guard_spec.loader.exec_module(session_guard_module)

    context_spec = importlib.util.spec_from_file_location(
        f"{package_name}.context_manager",
        UTILS_DIR / "context_manager.py",
    )
    context_module = importlib.util.module_from_spec(context_spec)
    sys.modules[context_spec.name] = context_module
    context_spec.loader.exec_module(context_module)
    return context_module


class MultimodalHistoryContentTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.context_module = _load_context_manager_module()
        cls.ContextManager = cls.context_module.ContextManager
        cls.AstrBotMessage = _AstrBotMessage
        cls.MessageMember = _MessageMember

    def test_format_context_for_ai_flattens_multimodal_history_content(self):
        msg = self.AstrBotMessage()
        msg.message_str = [
            {"type": "text", "text": "第一段"},
            {"type": "image_url", "image_url": {"url": "https://example.com/a.png"}},
            {"type": "text", "text": "第二段"},
        ]
        msg.sender = self.MessageMember(user_id="10001", nickname="群友A")
        msg.timestamp = 1713418380

        formatted = asyncio.run(
            self.ContextManager.format_context_for_ai(
                [msg],
                "当前消息",
                "bot",
                include_timestamp=False,
                include_sender_info=True,
            )
        )

        self.assertIn("第一段", formatted)
        self.assertIn("第二段", formatted)
        self.assertIn("[图片]", formatted)
        self.assertIn("群友A(ID:10001):", formatted)
        self.assertIn("当前消息", formatted)

    def test_dict_to_message_coerces_list_message_str_to_text(self):
        msg = self.ContextManager._dict_to_message(
            {
                "message_str": [
                    {"type": "text", "text": "文本片段"},
                    {"type": "image_url", "image_url": {"url": "https://example.com/a.png"}},
                ],
                "type": "OtherMessage",
            }
        )

        self.assertIsInstance(msg.message_str, str)
        self.assertIn("文本片段", msg.message_str)
        self.assertIn("[图片]", msg.message_str)

    def test_normalize_message_content_supports_platform_history_shape(self):
        normalized = self.ContextManager.normalize_message_content(
            [
                {"type": "text", "data": {"text": "平台文本"}},
                {"type": "image", "data": {"file": "demo.png"}},
                {"type": "image_url", "image_url": {"url": "https://example.com/a.png"}},
            ]
        )

        self.assertEqual(normalized, "平台文本[图片][图片]")

    def test_main_and_proactive_use_normalized_content(self):
        main_py = (REPO_ROOT / "main.py").read_text(encoding="utf-8")
        proactive = (REPO_ROOT / "utils" / "proactive_chat_manager.py").read_text(
            encoding="utf-8"
        )

        self.assertRegex(
            main_py,
            r"ContextManager\.normalize_message_content\(\s*msg\[\"content\"\]",
        )
        self.assertRegex(
            proactive,
            r"ContextManager\.normalize_message_content\(\s*msg\[\"content\"\]",
        )


if __name__ == "__main__":
    unittest.main()
