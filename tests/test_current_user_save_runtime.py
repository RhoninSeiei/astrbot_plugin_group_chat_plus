import ast
import asyncio
import copy
from pathlib import Path
from types import SimpleNamespace
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]


class AsyncLock:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class RecordingLogger:
    def __init__(self):
        self.records = []

    def info(self, message, *args, **kwargs):
        self.records.append(("info", message, args, kwargs))

    def warning(self, message, *args, **kwargs):
        self.records.append(("warning", message, args, kwargs))

    def error(self, message, *args, **kwargs):
        self.records.append(("error", message, args, kwargs))


class RecordingContextManager:
    bot_messages = []
    official_saves = []

    @classmethod
    async def save_bot_message(cls, event, message, context):
        cls.bot_messages.append(message)

    @classmethod
    async def save_to_official_conversation_with_cache(
        cls,
        event,
        cached_messages,
        user_message,
        bot_message,
        context,
        user_image_urls=None,
    ):
        cls.official_saves.append(
            {
                "cached_messages": copy.deepcopy(cached_messages),
                "user_message": user_message,
                "bot_message": bot_message,
                "user_image_urls": list(user_image_urls or []),
            }
        )
        return True


class RecordingCacheManager:
    def prepare_cache_for_save(self, **_kwargs):
        return []

    def clear_saved_cache(self, **_kwargs):
        return None

    def prepare_window_buffered_for_save(self, **_kwargs):
        return []

    def get_window_buffered_messages(self, _chat_id):
        return []

    def clear_window_buffered_cache(self, *_args, **_kwargs):
        return None


class FakeEvent:
    def __init__(self):
        self.extras = {}
        self._result = SimpleNamespace(
            chain=[SimpleNamespace(text="机器人回复")],
            is_llm_result=lambda: True,
        )

    def get_platform_name(self):
        return "aiocqhttp"

    def is_private_chat(self):
        return False

    def get_group_id(self):
        return "group-1"

    def get_sender_id(self):
        return "sender-1"

    def get_sender_name(self):
        return "sender"

    def get_extra(self, key, default=None):
        return self.extras.get(key, default)

    def set_extra(self, key, value):
        self.extras[key] = value


def _load_after_message_sent(logger):
    tree = ast.parse((REPO_ROOT / "main.py").read_text(encoding="utf-8"))
    chat_plus = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "ChatPlus"
    )
    method = next(
        copy.deepcopy(node)
        for node in chat_plus.body
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "after_message_sent"
    )
    method.decorator_list = []
    module = ast.Module(body=[method], type_ignores=[])
    ast.fix_missing_locations(module)
    namespace = {
        "AstrMessageEvent": object,
        "ContextManager": RecordingContextManager,
        "MessageProcessor": SimpleNamespace(
            add_metadata_from_cache=lambda raw, *_args, **_kwargs: raw,
            add_metadata_to_message=lambda _event, raw, *_args, **_kwargs: raw,
        ),
        "MessageCleaner": SimpleNamespace(
            clean_message=lambda value: value,
            extract_raw_message_from_event=lambda _event: "event fallback",
        ),
        "ProbabilityManager": SimpleNamespace(get_chat_key=lambda *_args: "chat"),
        "HumanizeModeManager": SimpleNamespace(),
        "PLUGIN_DIRECT_REPLY_MODE": "direct_reply",
        "PLUGIN_FALLBACK_PAYLOAD": "fallback_payload",
        "PLUGIN_REPLY_EFFECT_CONTEXT": "reply_effect_context",
        "PLUGIN_PENDING_MAIN_MODEL_DECISION": "pending_decision",
        "time": SimpleNamespace(time=lambda: 1000.0),
        "logger": logger,
    }
    exec(compile(module, "main.py", "exec"), namespace)
    return namespace["after_message_sent"]


class CurrentUserSaveRuntimeTest(unittest.TestCase):
    def setUp(self):
        RecordingContextManager.bot_messages.clear()
        RecordingContextManager.official_saves.clear()

    def test_ordinary_send_saves_snapshot_images_and_hides_sensitive_content(self):
        logger = RecordingLogger()
        after_message_sent = _load_after_message_sent(logger)
        secret_url = "https://private.example/image.png?token=SECRET-TOKEN"
        secret_base64 = "data:image/png;base64,SECRETBASE64"
        secret_text = f"引用正文 {secret_url} {secret_base64}"
        event = FakeEvent()

        async def no_success_effects(*_args, **_kwargs):
            return None

        harness = SimpleNamespace(
            _compute_session_integrity=lambda _chat_id: None,
            _get_message_id=lambda _event: "message-1",
            concurrent_lock=AsyncLock(),
            processing_sessions={"message-1": True},
            _saved_messages={},
            _agent_done_flags={"message-1"},
            _pending_bot_replies={"message-1": ["机器人回复"]},
            _duplicate_blocked_messages={},
            _ai_error_message_ids=set(),
            raw_reply_cache={},
            debug_mode=True,
            content_filter=SimpleNamespace(process_for_save=lambda value: value),
            _build_interleaved_tool_reply=lambda *_args: "",
            context=SimpleNamespace(),
            recent_replies_cache={},
            duplicate_filter_check_count=3,
            _DUPLICATE_CACHE_SIZE_LIMIT=100,
            _message_cache_snapshots={
                "message-1": {
                    "content": secret_text,
                    "reference_image_urls": [secret_url],
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
            _apply_successful_reply_effects=no_success_effects,
        )

        asyncio.run(after_message_sent(harness, event))

        self.assertEqual(len(RecordingContextManager.official_saves), 1)
        saved = RecordingContextManager.official_saves[0]
        self.assertEqual(saved["user_message"], secret_text)
        self.assertEqual(saved["user_image_urls"], [secret_url])
        rendered_logs = repr(logger.records)
        self.assertNotIn("private.example", rendered_logs)
        self.assertNotIn("SECRET-TOKEN", rendered_logs)
        self.assertNotIn("SECRETBASE64", rendered_logs)
        self.assertNotIn("引用正文", rendered_logs)


if __name__ == "__main__":
    unittest.main()
