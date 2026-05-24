import importlib.util
import pathlib
import sys
import types
import unittest


REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
UTILS_DIR = REPO_ROOT / "utils"


class _Logger:
    def info(self, *_args, **_kwargs):
        pass

    def warning(self, *_args, **_kwargs):
        pass

    def error(self, *_args, **_kwargs):
        pass


class _Plain:
    def __init__(self, text=""):
        self.text = text


class _At:
    def __init__(self, qq="", name=""):
        self.qq = qq
        self.name = name


class _AtAll:
    pass


class _Image:
    pass


class _Reply:
    pass


class _Forward:
    pass


class _AstrMessageEvent:
    pass


class _ProactiveChatManager:
    @staticmethod
    def filter_expired_cached_messages(messages):
        return messages


class _ContextManager:
    @staticmethod
    def normalize_message_content(content):
        if isinstance(content, str):
            return content
        return str(content)

    @staticmethod
    def _content_to_safe_text(content):
        return _ContextManager.normalize_message_content(content)


def _load_cache_manager_module():
    package_name = "group_chat_plus_cache_test"
    package_module = types.ModuleType(package_name)
    package_module.__path__ = [str(UTILS_DIR)]
    sys.modules[package_name] = package_module

    logger = _Logger()
    astrbot_module = types.ModuleType("astrbot")
    astrbot_api_module = types.ModuleType("astrbot.api")
    astrbot_api_module.logger = logger
    astrbot_api_all_module = types.ModuleType("astrbot.api.all")
    astrbot_api_all_module.logger = logger
    astrbot_api_all_module.AstrMessageEvent = _AstrMessageEvent
    astrbot_event_module = types.ModuleType("astrbot.api.event")
    astrbot_event_module.AstrMessageEvent = _AstrMessageEvent
    astrbot_platform_module = types.ModuleType("astrbot.api.platform")
    astrbot_platform_module.AstrBotMessage = type("AstrBotMessage", (), {})
    astrbot_platform_module.MessageMember = type("MessageMember", (), {})
    astrbot_platform_module.MessageType = types.SimpleNamespace(
        GROUP_MESSAGE="GroupMessage",
        FRIEND_MESSAGE="FriendMessage",
    )
    components_module = types.ModuleType("astrbot.api.message_components")
    components_module.Plain = _Plain
    components_module.At = _At
    components_module.AtAll = _AtAll
    components_module.Image = _Image
    components_module.Reply = _Reply
    core_components_module = types.ModuleType("astrbot.core.message.components")
    core_components_module.Forward = _Forward

    sys.modules["astrbot"] = astrbot_module
    sys.modules["astrbot.api"] = astrbot_api_module
    sys.modules["astrbot.api.all"] = astrbot_api_all_module
    sys.modules["astrbot.api.event"] = astrbot_event_module
    sys.modules["astrbot.api.platform"] = astrbot_platform_module
    sys.modules["astrbot.api.message_components"] = components_module
    sys.modules["astrbot.core.message.components"] = core_components_module

    proactive_module = types.ModuleType(f"{package_name}.proactive_chat_manager")
    proactive_module.ProactiveChatManager = _ProactiveChatManager
    sys.modules[proactive_module.__name__] = proactive_module

    context_module = types.ModuleType(f"{package_name}.context_manager")
    context_module.ContextManager = _ContextManager
    sys.modules[context_module.__name__] = context_module

    for module_name in ("message_processor", "message_cleaner", "message_cache_manager"):
        spec = importlib.util.spec_from_file_location(
            f"{package_name}.{module_name}",
            UTILS_DIR / f"{module_name}.py",
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)

    return sys.modules[f"{package_name}.message_cache_manager"]


class MessageCachePersistentPokeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cache_module = _load_cache_manager_module()

    def test_prepare_cache_for_save_appends_persistent_poke_text(self):
        manager = self.cache_module.MessageCacheManager(
            include_timestamp=False,
            include_sender_info=False,
        )
        manager.pending_messages_cache["g1"] = [
            {
                "role": "user",
                "content": "戳一下",
                "timestamp": 10,
                "message_id": "m1",
                "sender_id": "20002",
                "sender_name": "群友甲",
                "persistent_poke_event_text": "[戳一戳事件]有人戳了你",
            }
        ]

        prepared = manager.prepare_cache_for_save(
            chat_id="g1",
            current_msg_id="m2",
            current_msg_timestamp=20,
            processing_msg_ids=set(),
        )

        self.assertEqual(len(prepared), 1)
        self.assertIn("[戳一戳事件]有人戳了你", prepared[0]["content"])
        self.assertEqual(prepared[0]["message_id"], "m1")

    def test_prepare_window_buffered_for_save_adds_smart_merged_marker(self):
        manager = self.cache_module.MessageCacheManager(
            include_timestamp=False,
            include_sender_info=False,
        )
        manager.pending_messages_cache["g1"] = [
            {
                "role": "user",
                "content": "后续补充",
                "timestamp": 10,
                "message_id": "m1",
                "sender_id": "20002",
                "sender_name": "群友甲",
                "window_buffered": True,
                "smart_merged": True,
            }
        ]

        prepared = manager.prepare_window_buffered_for_save("g1", set())

        self.assertEqual([item["role"] for item in prepared], ["user", "assistant"])
        self.assertIn("[Smart合并]", prepared[1]["content"])


if __name__ == "__main__":
    unittest.main()
