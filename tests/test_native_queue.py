"""Request-bound native queue refresh, using the real context formatter."""
import ast
import asyncio
import copy
from datetime import datetime
import importlib.util
import logging
from pathlib import Path
import re
import sys
from types import SimpleNamespace
from typing import Any, List
import unittest


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("gcp_native_queue_test", ROOT / "utils/native_queue.py")
queue = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = queue
spec.loader.exec_module(queue)


class Message:
    def __init__(self, message_id, text, sender="user"):
        self.message_id = message_id
        self.message_str = text
        self.sender = SimpleNamespace(nickname=sender, user_id=sender)
        self.timestamp = 100.0


def native_formatter():
    tree = ast.parse((ROOT / "utils/context_manager.py").read_text(encoding="utf-8"))
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "ContextManager")
    methods = {"format_context_for_ai", "normalize_message_content", "strip_tool_call_record_blocks"}
    cls.body = [node for node in cls.body if getattr(node, "name", "") in methods]
    module = ast.Module(body=[cls], type_ignores=[])
    ast.fix_missing_locations(module)
    scope = {"AstrBotMessage": Message, "List": List, "Any": Any, "datetime": datetime,
             "DEBUG_MODE": False, "logger": logging.getLogger(__name__), "re": re}
    exec(compile(module, "context_manager.py", "exec"), scope)
    return scope["ContextManager"].format_context_for_ai


class Event:
    def __init__(self, umo="qq:GroupMessage:g1"):
        self.unified_msg_origin = umo


class NativeStage:
    async def process(self, event):
        async with session_lock_manager.acquire_lock(event.unified_msg_origin):
            result = await build_main_agent(event=event)
            await call_event_hook(event, EventType.OnLLMRequestEvent, result)
            await self._save_to_history(event, result)


class UnlockedStage:
    async def process(self, event):
        async with session_lock_manager.acquire_lock(event.unified_msg_origin):
            result = await build_main_agent(event=event)
        await call_event_hook(event, EventType.OnLLMRequestEvent, result)


session_lock_manager = SimpleNamespace(acquire_lock=lambda session: None)


class NativeCapabilityTests(unittest.TestCase):
    def module(self, stage):
        return SimpleNamespace(InternalAgentSubStage=stage, session_lock_manager=session_lock_manager)

    def test_native_lock_covers_build_hook_and_history_save(self):
        capability = queue.detect_native_queue_capability(self.module(NativeStage))
        self.assertTrue(capability.supported, capability.reason)
        self.assertTrue(queue.can_delegate_native(capability, native_provider_request=True))
        self.assertFalse(queue.can_delegate_native(capability, native_provider_request=True, direct_provider=True))
        self.assertFalse(queue.can_delegate_native(capability, native_provider_request=False))

    def test_old_core_or_hook_outside_lock_falls_back(self):
        for module in (SimpleNamespace(), self.module(UnlockedStage)):
            capability = queue.detect_native_queue_capability(module)
            self.assertFalse(capability.supported)
            self.assertTrue(capability.reason)
            self.assertFalse(queue.can_delegate_native(capability, native_provider_request=True))


class NativeQueueRecipeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.formatter = native_formatter()
        self.event = Event()
        self.req = SimpleNamespace(prompt="retrieval query", system_prompt="persona", contexts=[])
        self.history = [Message("m1", "旧历史")]
        self.window = [{"message_id": "w1", "content": "Smart 追加", "sender_id": "other", "sender_name": "Other"}]
        self.current = "当前消息"
        self.rendered = await self.formatter(self.history, self.current, "bot", include_timestamp=False,
                                             include_sender_info=True, window_buffered_messages=self.window)
        self.payload = "人格和规则\n" + self.rendered + "\nSmart 批次提示\n其他插件补充"
        self.tokens = {self.event.unified_msg_origin: 0}
        self.recipe = queue.RequestHistoryRecipe.capture(
            self.event, history_messages=self.history, current_message=self.current, bot_id="bot",
            window_buffered_messages=self.window, include_timestamp=False, include_sender_info=True,
            rendered_fragment=self.rendered, formatter=self.formatter, reset_token=0)
        self.assertTrue(self.recipe.bind(self.event, self.req))

    async def refresh(self, reader, **kwargs):
        return await self.recipe.refresh(self.event, self.req, self.payload, reader=reader,
                                         get_reset_token=lambda umo: self.tokens[umo], **kwargs)

    async def test_refreshes_only_history_and_preserves_current_window_and_other_prompts(self):
        calls = []
        async def reader(event, umo, original_history):
            calls.append((event, umo, original_history))
            return [*original_history, Message("m2", "最新已发送回复", "bot")]
        result = await self.refresh(reader)
        self.assertEqual(result.status, "refreshed")
        self.assertIn("最新已发送回复", result.payload)
        for preserved in ("当前消息", "Smart 追加", "人格和规则", "Smart 批次提示", "其他插件补充"):
            self.assertEqual(result.payload.count(preserved), self.payload.count(preserved))
        self.assertIs(calls[0][0], self.event)
        self.assertEqual(calls[0][1], self.event.unified_msg_origin)
        self.assertEqual(self.req.prompt, "retrieval query")
        self.assertEqual(self.req.system_prompt, "persona")

    async def test_identical_text_with_distinct_message_ids_is_not_deduplicated(self):
        async def reader(*args):
            return [Message("a", "相同正文"), Message("b", "相同正文")]
        result = await self.refresh(reader)
        self.assertEqual(result.status, "refreshed")
        self.assertEqual(result.payload.count("相同正文"), 2)

    async def test_snapshot_is_deep_copied_and_reader_cannot_change_it(self):
        self.history[0].message_str = "外部篡改"
        self.window[0]["content"] = "外部窗口篡改"
        async def reader(event, umo, original):
            self.assertEqual(original[0].message_str, "旧历史")
            original[0].message_str = "合法刷新"
            return original
        result = await self.refresh(reader)
        self.assertEqual(result.status, "refreshed")
        self.assertIn("合法刷新", result.payload)
        self.assertIn("Smart 追加", result.payload)
        self.assertNotIn("外部", result.payload)

    async def test_other_event_request_or_group_cannot_read_or_refresh(self):
        calls = []
        async def reader(*args):
            calls.append(args)
            return []
        for event, req in ((Event(), self.req), (self.event, SimpleNamespace()), (Event("qq:GroupMessage:g2"), self.req)):
            result = await self.recipe.refresh(event, req, self.payload, reader=reader,
                                               get_reset_token=lambda umo: 0)
            self.assertEqual(result.status, "binding_mismatch")
            self.assertEqual(result.payload, self.payload)
        self.event.unified_msg_origin = "qq:GroupMessage:g2"
        result = await self.recipe.refresh(self.event, self.req, self.payload, reader=reader, get_reset_token=lambda umo: 0)
        self.assertEqual(result.status, "binding_mismatch")
        self.assertEqual(calls, [])

    async def test_binding_cannot_be_reassigned(self):
        self.assertTrue(self.recipe.bind(self.event, self.req))
        self.assertFalse(self.recipe.bind(self.event, SimpleNamespace()))
        self.assertFalse(self.recipe.bind(Event(), self.req))

    async def test_missing_or_ambiguous_known_fragment_keeps_payload(self):
        async def reader(*args):
            self.fail("unsafe replacement must not read history")
        for payload in ("replacement without known fragment", self.payload + self.rendered):
            result = await self.recipe.refresh(self.event, self.req, payload, reader=reader,
                                               get_reset_token=lambda umo: 0)
            self.assertEqual(result.status, "fragment_mismatch")
            self.assertEqual(result.payload, payload)

    async def test_reset_before_read_keeps_payload_without_restoring_old_history(self):
        self.tokens[self.event.unified_msg_origin] = 1
        async def reader(*args):
            self.fail("invalidated request must not read old history")
        result = await self.refresh(reader)
        self.assertEqual(result.status, "reset_changed")
        self.assertEqual(result.payload, self.payload)

    async def test_reset_during_read_keeps_original_payload(self):
        async def reader(*args):
            self.tokens[self.event.unified_msg_origin] = 1
            return [Message("new", "不得恢复")]
        result = await self.refresh(reader)
        self.assertEqual(result.status, "reset_changed")
        self.assertEqual(result.payload, self.payload)

    async def test_reader_failure_keeps_original_payload(self):
        async def failing(*args):
            raise RuntimeError("private history text must not leak")
        result = await self.refresh(failing)
        self.assertEqual(result.status, "read_failed")
        self.assertEqual(result.payload, self.payload)
        self.assertNotIn("private", repr(result))

    async def test_invalid_reader_result_keeps_original_payload(self):
        async def invalid(*args):
            return None
        result = await self.refresh(invalid)
        self.assertEqual(result.status, "invalid_history")
        self.assertEqual(result.payload, self.payload)

    async def test_native_formatter_silent_fallback_cannot_drop_current_section(self):
        async def formatter(**inputs):
            if any(msg.message_id == "new" for msg in inputs["history_messages"]):
                return inputs["current_message"]
            return await self.formatter(**inputs)
        recipe = queue.RequestHistoryRecipe.capture(
            self.event, history_messages=self.history, current_message=self.current, bot_id="bot",
            window_buffered_messages=self.window, include_timestamp=False, include_sender_info=True,
            rendered_fragment=self.rendered, formatter=formatter, reset_token=0)
        recipe.bind(self.event, self.req)
        async def reader(*args):
            return [Message("new", "fresh")]
        result = await recipe.refresh(self.event, self.req, self.payload, reader=reader, get_reset_token=lambda umo: 0)
        self.assertEqual(result.status, "invalid_render")
        self.assertEqual(result.payload, self.payload)

    async def test_reset_during_final_formatter_keeps_original_payload(self):
        async def formatter(**inputs):
            rendered = await self.formatter(**inputs)
            if any(msg.message_id == "new" for msg in inputs["history_messages"]):
                self.tokens[self.event.unified_msg_origin] = 1
            return rendered
        recipe = queue.RequestHistoryRecipe.capture(
            self.event, history_messages=self.history, current_message=self.current, bot_id="bot",
            window_buffered_messages=self.window, include_timestamp=False, include_sender_info=True,
            rendered_fragment=self.rendered, formatter=formatter, reset_token=0)
        recipe.bind(self.event, self.req)
        async def reader(*args):
            return [Message("new", "fresh")]
        result = await recipe.refresh(self.event, self.req, self.payload, reader=reader,
                                      get_reset_token=lambda umo: self.tokens[umo])
        self.assertEqual(result.status, "reset_changed")
        self.assertEqual(result.payload, self.payload)

    async def test_formatter_failure_or_wrong_original_render_keeps_payload(self):
        async def reader(*args):
            return [Message("new", "fresh")]
        for formatter in (self.failing_formatter, self.wrong_formatter):
            recipe = queue.RequestHistoryRecipe.capture(
                self.event, history_messages=self.history, current_message=self.current, bot_id="bot",
                rendered_fragment=self.rendered, formatter=formatter, reset_token=0)
            recipe.bind(self.event, self.req)
            result = await recipe.refresh(self.event, self.req, self.payload, reader=reader, get_reset_token=lambda umo: 0)
            self.assertIn(result.status, {"format_failed", "original_render_mismatch"})
            self.assertEqual(result.payload, self.payload)

    @staticmethod
    async def failing_formatter(*args, **kwargs):
        raise ValueError("bad format")

    @staticmethod
    async def wrong_formatter(*args, **kwargs):
        return "wrong formatter output"

    async def test_cancellation_propagates_and_original_request_remains_unchanged(self):
        async def reader(*args):
            raise asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.refresh(reader)
        self.assertEqual(self.req.prompt, "retrieval query")


if __name__ == "__main__":
    unittest.main()
