import importlib.util
import pathlib
import sys
import types
import unittest


REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _install_astrbot_stubs():
    astrbot_module = types.ModuleType("astrbot")
    astrbot_api_module = types.ModuleType("astrbot.api")
    astrbot_api_all_module = types.ModuleType("astrbot.api.all")
    astrbot_api_event_module = types.ModuleType("astrbot.api.event")

    logger = types.SimpleNamespace(
        info=lambda *_args, **_kwargs: None,
        warning=lambda *_args, **_kwargs: None,
        error=lambda *_args, **_kwargs: None,
        debug=lambda *_args, **_kwargs: None,
    )
    astrbot_api_module.logger = logger
    astrbot_api_all_module.logger = logger
    astrbot_api_all_module.Context = object
    astrbot_api_all_module.AstrMessageEvent = object
    astrbot_api_event_module.AstrMessageEvent = object

    sys.modules.setdefault("astrbot", astrbot_module)
    sys.modules.setdefault("astrbot.api", astrbot_api_module)
    sys.modules.setdefault("astrbot.api.all", astrbot_api_all_module)
    sys.modules.setdefault("astrbot.api.event", astrbot_api_event_module)


def _load_module(module_name, relative_path):
    _install_astrbot_stubs()
    module_path = REPO_ROOT / relative_path
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class MemoryIdentityHardeningTest(unittest.TestCase):
    def test_livingmemory_metadata_outputs_confirmed_and_nickname_only_members(self):
        module = _load_module("memory_injector_identity_test", "utils/memory_injector.py")
        mem = types.SimpleNamespace(
            content="Alice 说自己喜欢红色机体。",
            metadata={
                "importance": 0.8,
                "create_time": 0,
                "platform_id": "aiocqhttp",
                "group_id": "851926461",
                "sender_id": "1001",
                "sender_name": "Alice",
                "participants": [
                    {"nickname": "Alice", "user_id": "1001"},
                    {"nickname": "Bob"},
                    {"nickname": "Carol", "platform_id": "aiocqhttp"},
                ],
            },
        )

        text = module.MemoryInjector._format_livingmemory_memory(mem, 1)

        self.assertIn("Alice(ID:1001)", text)
        self.assertIn("Bob(仅昵称来源，身份未确认)", text)
        self.assertIn("Carol(仅昵称来源，身份未确认)", text)
        self.assertNotIn("Carol(ID:aiocqhttp)", text)
        self.assertIn("群组ID: 851926461", text)
        self.assertIn("身份可信度: 含用户ID的成员引用可用于区分同名成员", text)
        self.assertIn("仅昵称来源的成员引用只作弱参考", text)

    def test_memory_injection_adds_member_identity_rule(self):
        module = _load_module("memory_injector_identity_rule_test", "utils/memory_injector.py")

        injected = module.MemoryInjector.inject_memories_to_message(
            "=== 当前消息 ===\nAlice(ID:1001): 之前谁说喜欢红色？",
            "1. Alice 说自己喜欢红色机体。",
        )

        self.assertIn("成员身份识别规则", injected)
        self.assertIn("涉及具体成员时以用户ID为准", injected)
        self.assertIn("仅昵称来源", injected)


    def test_livingmemory_metadata_supports_compat_shapes(self):
        module = _load_module("memory_injector_identity_compat_test", "utils/memory_injector.py")
        mem = types.SimpleNamespace(
            content="\u517c\u5bb9\u7ed3\u6784\u8bb0\u5fc6",
            metadata={
                "sender": {"sender_id": "2002", "sender_name": "Eve"},
                "participants": {
                    "lead": {"user_id": "3003", "display_name": "Mallory"},
                    "guest": "Oscar",
                },
                "members": [{"qq": "4004", "name": "Trudy"}],
            },
        )

        text = module.MemoryInjector._format_livingmemory_memory(mem, 1)

        self.assertIn("Eve(ID:2002)", text)
        self.assertIn("Mallory(ID:3003)", text)
        self.assertIn("Oscar(\u4ec5\u6635\u79f0\u6765\u6e90\uff0c\u8eab\u4efd\u672a\u786e\u8ba4)", text)
        self.assertIn("Trudy(ID:4004)", text)

    def test_livingmemory_metadata_missing_structure_marks_weak_reference(self):
        module = _load_module("memory_injector_identity_empty_meta_test", "utils/memory_injector.py")
        mem = types.SimpleNamespace(content="\u65e7\u8bb0\u5fc6", metadata=None)

        text = module.MemoryInjector._format_livingmemory_memory(mem, 1)

        self.assertIn(
            "\u8be5\u8bb0\u5fc6\u672a\u63d0\u4f9b\u7ed3\u6784\u5316\u8eab\u4efd\u5b57\u6bb5",
            text,
        )
        self.assertIn("\u4ec5\u80fd\u6309\u6b63\u6587\u5f31\u53c2\u8003", text)


class PlatformLTMIdentityHardeningTest(unittest.TestCase):
    def test_sender_id_disambiguates_same_nickname_records(self):
        module = _load_module("platform_ltm_identity_test", "utils/platform_ltm_helper.py")
        ltm = types.SimpleNamespace(
            session_chats={
                "aiocqhttp:GroupMessage:851926461": [
                    "[Alice(ID:2002)/12:00:00]: [Image: 错误图片描述]",
                    "[Alice(ID:1001)/12:00:00]: [Image: 正确图片描述]",
                ]
            }
        )

        ok, text = module.PlatformLTMHelper._try_extract_caption(
            ltm,
            "aiocqhttp:GroupMessage:851926461",
            "Alice",
            "[图片]",
            "12:00:00",
            sender_id="1001",
        )

        self.assertTrue(ok)
        self.assertEqual("[图片内容: 正确图片描述]", text)

    def test_nickname_only_ltm_record_remains_current_message_supplement(self):
        module = _load_module(
            "platform_ltm_nickname_only_identity_test", "utils/platform_ltm_helper.py"
        )
        ltm = types.SimpleNamespace(
            session_chats={
                "aiocqhttp:GroupMessage:851926461": [
                    "[Alice/12:00:00]: 这张图 [Image: 当前图片描述]",
                ]
            }
        )

        ok, text = module.PlatformLTMHelper._try_extract_caption(
            ltm,
            "aiocqhttp:GroupMessage:851926461",
            "Alice",
            "这张图 [图片]",
            "12:00:00",
            sender_id="1001",
        )

        self.assertTrue(ok)
        self.assertEqual("这张图 [图片内容: 当前图片描述]", text)


    def test_should_wait_for_platform_ignores_same_nickname_other_sender_id(self):
        module = _load_module("platform_ltm_wait_identity_test", "utils/platform_ltm_helper.py")
        ltm = types.SimpleNamespace(
            session_chats={
                "aiocqhttp:GroupMessage:851926461": [
                    "[Alice(ID:2002)/12:00:00]: [Image]",
                ]
            }
        )

        should_wait = module.PlatformLTMHelper._should_wait_for_platform(
            ltm,
            "aiocqhttp:GroupMessage:851926461",
            "Alice",
            "[\u56fe\u7247]",
            "12:00:00",
            sender_id="1001",
        )

        self.assertTrue(should_wait)

    def test_check_platform_failed_ignores_same_nickname_other_sender_id(self):
        module = _load_module("platform_ltm_failed_identity_test", "utils/platform_ltm_helper.py")
        ltm = types.SimpleNamespace(
            session_chats={
                "aiocqhttp:GroupMessage:851926461": [
                    "[Alice(ID:2002)/12:00:00]: [Image]",
                ]
            }
        )

        failed = module.PlatformLTMHelper._check_platform_failed(
            ltm,
            "aiocqhttp:GroupMessage:851926461",
            "Alice",
            "12:00:00",
            sender_id="1001",
        )

        self.assertFalse(failed)


if __name__ == "__main__":
    unittest.main()
