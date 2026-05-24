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


def _install_astrbot_stubs():
    logger = _Logger()
    astrbot_module = types.ModuleType("astrbot")
    astrbot_api_module = types.ModuleType("astrbot.api")
    astrbot_api_module.logger = logger
    astrbot_api_all_module = types.ModuleType("astrbot.api.all")
    astrbot_api_all_module.logger = logger
    astrbot_api_all_module.AstrMessageEvent = _AstrMessageEvent

    components_module = types.ModuleType("astrbot.api.message_components")
    components_module.Plain = _Plain
    components_module.At = _At
    components_module.AtAll = _AtAll
    components_module.Image = _Image
    components_module.Reply = _Reply

    core_module = types.ModuleType("astrbot.core")
    core_message_module = types.ModuleType("astrbot.core.message")
    core_components_module = types.ModuleType("astrbot.core.message.components")
    core_components_module.Forward = _Forward

    sys.modules["astrbot"] = astrbot_module
    sys.modules["astrbot.api"] = astrbot_api_module
    sys.modules["astrbot.api.all"] = astrbot_api_all_module
    sys.modules["astrbot.api.message_components"] = components_module
    sys.modules["astrbot.core"] = core_module
    sys.modules["astrbot.core.message"] = core_message_module
    sys.modules["astrbot.core.message.components"] = core_components_module


def _load_utils_module(module_name):
    _install_astrbot_stubs()
    spec = importlib.util.spec_from_file_location(
        module_name,
        UTILS_DIR / f"{module_name}.py",
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class EmptyAtAndPokeHelpersTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cleaner_module = _load_utils_module("message_cleaner")
        cls.processor_module = _load_utils_module("message_processor")

    def test_empty_at_only_ai_requires_no_other_mentions(self):
        mention_info = {
            "has_at_ai": True,
            "has_at_others": False,
            "has_at_all": False,
        }

        self.assertTrue(
            self.cleaner_module.MessageCleaner.is_empty_at_message(
                "[At:10001|机器人]",
                True,
                mention_info=mention_info,
                mode="only_ai",
            )
        )

        mixed_mentions = {
            "has_at_ai": True,
            "has_at_others": True,
            "has_at_all": False,
        }
        self.assertFalse(
            self.cleaner_module.MessageCleaner.is_empty_at_message(
                "[At:10001|机器人][At:20002|群友]",
                True,
                mention_info=mixed_mentions,
                mode="only_ai",
            )
        )

    def test_empty_at_contains_ai_accepts_mixed_mentions(self):
        mention_info = {
            "has_at_ai": True,
            "has_at_others": True,
            "has_at_all": True,
        }

        self.assertTrue(
            self.cleaner_module.MessageCleaner.is_empty_at_message(
                "[At:10001|机器人][At:all]",
                True,
                mention_info=mention_info,
                mode="contains_ai",
            )
        )

    def test_inline_mentions_and_direction_notice_use_structured_info(self):
        mention_info = {
            "has_at_ai": True,
            "has_at_others": True,
            "has_at_all": False,
            "mentions": [
                {
                    "user_id": "10001",
                    "user_name": "机器人",
                    "is_bot": True,
                    "resolved": True,
                },
                {
                    "user_id": "20002",
                    "user_name": "群友甲",
                    "is_bot": False,
                    "resolved": True,
                },
            ],
        }

        processed = self.processor_module.MessageProcessor.inline_resolve_mentions(
            "[At:10001] [At:20002]",
            mention_info,
        )
        notice = self.processor_module.MessageProcessor.build_mention_direction_notice(
            mention_info
        )

        self.assertIn("[At:10001|你]", processed)
        self.assertIn("[At:20002|群友甲]", processed)
        self.assertIn("除了@你", notice)

    def test_persistent_poke_text_is_appended_once(self):
        event_text = self.processor_module.MessageProcessor.build_persistent_poke_event_text(
            {
                "is_poke_bot": True,
                "sender_id": "20002",
                "sender_name": "群友甲",
            }
        )

        formatted = self.processor_module.MessageProcessor.format_message_for_context_display(
            "原始消息",
            persistent_poke_event_text=event_text,
        )
        formatted_again = self.processor_module.MessageProcessor.format_message_for_context_display(
            formatted,
            persistent_poke_event_text=event_text,
        )

        self.assertEqual(event_text, "[戳一戳事件]有人戳了你，发起者是群友甲(ID:20002)")
        self.assertEqual(formatted_again.count("[戳一戳事件]"), 1)


if __name__ == "__main__":
    unittest.main()
