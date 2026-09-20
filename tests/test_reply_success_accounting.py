import ast
import asyncio
import copy
from pathlib import Path
from types import SimpleNamespace
import unittest

from tests.test_current_user_save_runtime import (
    AsyncLock,
    FakeEvent as SaveEvent,
    RecordingCacheManager,
    RecordingContextManager,
    RecordingLogger,
    _load_after_message_sent,
)


ROOT = Path(__file__).resolve().parents[1]
MAIN_SOURCE = (ROOT / "main.py").read_text(encoding="utf-8")
MAIN_TREE = ast.parse(MAIN_SOURCE)
CHAT_PLUS = next(
    node
    for node in MAIN_TREE.body
    if isinstance(node, ast.ClassDef) and node.name == "ChatPlus"
)


def method_node(name):
    return next(
        (
            copy.deepcopy(node)
            for node in CHAT_PLUS.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == name
        ),
        None,
    )


def compile_method(name, namespace):
    node = method_node(name)
    if node is None:
        return None
    node.decorator_list = []
    module = ast.Module(body=[node], type_ignores=[])
    ast.fix_missing_locations(module)
    target = dict(namespace)
    exec(compile(module, "main.py", "exec"), target)
    return target[name]


class Result:
    def __init__(self, text="", *, llm=True):
        self.chain = [SimpleNamespace(text=text)] if text is not None else []
        self._llm = llm
        self.content_type = None

    def is_llm_result(self):
        return self._llm

    def set_result_content_type(self, value):
        self.content_type = value
        self._llm = value == "llm"


class Event:
    def __init__(self, result=None, *, at=False):
        self.extras = {}
        self.result = result
        self.session_id = "session"
        self.is_at_or_wake_command = at

    def get_platform_name(self):
        return "aiocqhttp"

    def is_private_chat(self):
        return False

    def get_group_id(self):
        return "group"

    def get_sender_id(self):
        return "sender"

    def get_sender_name(self):
        return "sender-name"

    def get_message_str(self):
        return "hello"

    def get_extra(self, key, default=None):
        return self.extras.get(key, default)

    def set_extra(self, key, value):
        if value is None:
            self.extras.pop(key, None)
        else:
            self.extras[key] = value

    def get_result(self):
        return self.result

    def set_result(self, value):
        self.result = value

    def plain_result(self, text):
        return Result(text, llm=False)


class ReplySuccessAccountingTest(unittest.TestCase):
    def test_raw_llm_failure_clears_pending_state_and_marks_managed_message(self):
        pending_key = "pending_decision"
        effect_key = "reply_effect_context"
        event = Event(Result("RAW_PROVIDER_FAILURE", llm=True))
        event.extras.update({pending_key: {"decision": True}, effect_key: {"message": "x"}})

        async def persona_failure(*_args):
            return "符合人格的错误说明"

        def mark_failed(current_event, message_id):
            current_event.set_extra(pending_key, None)
            current_event.set_extra(effect_key, None)
            if message_id in harness.processing_sessions:
                harness._ai_error_message_ids.add(message_id)

        def replace_text(result, text):
            result.chain = [SimpleNamespace(text=text)]

        method = compile_method(
            "on_decorating_result",
            {
                "AstrMessageEvent": object,
                "classify_raw_llm_failure": lambda text: "provider_failed" if text == "RAW_PROVIDER_FAILURE" else None,
                "logger": RecordingLogger(),
                "PLUGIN_MAIN_MODEL_FINAL_GATE_DECLINED": "declined",
            },
        )
        self.assertIsNotNone(method)
        harness = SimpleNamespace(
            _get_message_id=lambda _event: "managed-message",
            _clear_sent_step_image_direct_result=lambda *_args: False,
            _group_llm_runtime_guard_enabled=lambda _event: True,
            _build_persona_llm_failure_reply=persona_failure,
            _replace_llm_result_text=replace_text,
            _mark_reply_generation_failed=mark_failed,
            processing_sessions={"managed-message": True},
            _ai_error_message_ids=set(),
        )

        asyncio.run(method(harness, event))

        self.assertEqual(event.result.chain[0].text, "符合人格的错误说明")
        self.assertNotIn(pending_key, event.extras)
        self.assertNotIn(effect_key, event.extras)
        self.assertEqual(harness._ai_error_message_ids, {"managed-message"})

    def test_typed_llm_error_is_saved_without_bot_or_success_effects(self):
        self._check_unsuccessful_send(generation_error=True, delivered=True)

    def test_no_successful_platform_send_does_not_commit_reply_effects(self):
        self._check_unsuccessful_send(generation_error=False, delivered=False)

    def _check_unsuccessful_send(self, *, generation_error, delivered):
        RecordingContextManager.bot_messages.clear()
        RecordingContextManager.official_saves.clear()
        logger = RecordingLogger()
        method = _load_after_message_sent(logger)
        event = SaveEvent()
        event._has_send_oper = delivered
        event._result.chain = [SimpleNamespace(text="符合人格的错误说明")]
        success_effects = []

        async def record_success(*args):
            success_effects.append(args)

        harness = SimpleNamespace(
            _compute_session_integrity=lambda _chat_id: None,
            _get_message_id=lambda _event: "message-1",
            concurrent_lock=AsyncLock(),
            processing_sessions={"message-1": True},
            _saved_messages={},
            _agent_done_flags={"message-1"},
            _pending_bot_replies={"message-1": ["符合人格的错误说明"]},
            _duplicate_blocked_messages={},
            _ai_error_message_ids={"message-1"} if generation_error else set(),
            raw_reply_cache={},
            debug_mode=False,
            content_filter=SimpleNamespace(process_for_save=lambda value: value),
            _build_interleaved_tool_reply=lambda *_args: "",
            context=SimpleNamespace(),
            recent_replies_cache={},
            duplicate_filter_check_count=3,
            _DUPLICATE_CACHE_SIZE_LIMIT=100,
            _message_cache_snapshots={
                "message-1": {
                    "content": "用户消息",
                    "reference_image_urls": [],
                    "sender_id": "sender-1",
                    "sender_name": "sender",
                    "timestamp": 900.0,
                    "message_id": "message-1",
                }
            },
            pending_messages_cache={},
            include_timestamp=False,
            include_sender_info=False,
            _append_persistent_event_text=lambda value, _extra: value,
            proactive_processing_sessions=set(),
            cache_manager=RecordingCacheManager(),
            _smart_batch_snapshots={},
            humanize_mode_enabled=False,
            _apply_successful_reply_effects=record_success,
        )

        asyncio.run(method(harness, event))

        self.assertEqual(len(RecordingContextManager.official_saves), 1)
        self.assertIsNone(RecordingContextManager.official_saves[0]["bot_message"])
        self.assertEqual(RecordingContextManager.bot_messages, [])
        self.assertEqual(success_effects, [])

    def test_empty_reply_recovery_rejects_error_shapes_and_accepts_normal_text(self):
        responses = (
            (SimpleNamespace(role="err", status_code=None, completion_text="角色错误", result_chain=None), False),
            (SimpleNamespace(role="assistant", status_code=429, completion_text="请求太多", result_chain=None), False),
            (SimpleNamespace(role="assistant", status_code=None, completion_text="", result_chain=None), False),
            (SimpleNamespace(role="assistant", status_code=None, completion_text="正常回复", result_chain=None), True),
        )
        for response, expected in responses:
            with self.subTest(role=response.role, status=response.status_code, text=response.completion_text):
                async def request(*_args, **_kwargs):
                    return response, "primary", "actual", 0

                method = compile_method(
                    "_try_recover_empty_llm_reply",
                    {
                        "AstrMessageEvent": object,
                        "ProviderRequest": lambda **kwargs: SimpleNamespace(**kwargs),
                        "ReplyHandler": SimpleNamespace(_request_with_astrbot_fallback=request),
                        "ResultContentType": SimpleNamespace(LLM_RESULT="llm"),
                        "PLUGIN_FALLBACK_PAYLOAD": "fallback_payload",
                        "classify_raw_llm_failure": lambda text: "provider_failed" if text == "RAW_FAILURE" else None,
                        "logger": RecordingLogger(),
                    },
                )
                event = Event(at=False)
                event.extras["fallback_payload"] = {"prompt": "p", "system_prompt": "s"}
                failures = []
                harness = SimpleNamespace(
                    context=object(),
                    debug_mode=False,
                    _mark_reply_generation_failed=lambda current_event, message_id: failures.append(message_id),
                )

                recovered = asyncio.run(method(harness, event, "message-1"))

                self.assertIs(recovered, expected)
                if expected:
                    self.assertTrue(event.result.is_llm_result())
                    self.assertEqual(event.result.chain[0].text, "正常回复")
                else:
                    self.assertIsNone(event.result)

    def test_failure_helper_does_not_create_global_set_for_unmanaged_event(self):
        method = compile_method(
            "_mark_reply_generation_failed",
            {
                "AstrMessageEvent": object,
                "PLUGIN_PENDING_MAIN_MODEL_DECISION": "pending_decision",
                "PLUGIN_REPLY_EFFECT_CONTEXT": "reply_effect_context",
            },
        )
        self.assertIsNotNone(method, "ChatPlus._mark_reply_generation_failed must exist")
        event = Event()
        event.extras.update({"pending_decision": {"x": 1}, "reply_effect_context": {"x": 2}})
        harness = SimpleNamespace(processing_sessions={})

        method(harness, event, "external-message")

        self.assertNotIn("pending_decision", event.extras)
        self.assertNotIn("reply_effect_context", event.extras)
        self.assertFalse(hasattr(harness, "_ai_error_message_ids"))


if __name__ == "__main__":
    unittest.main()
