import ast
import copy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]


class Event:
    def __init__(self):
        self.extras = {"classification": "old"}
    def get_extra(self, key, default=None): return self.extras.get(key, default)
    def set_extra(self, key, value): self.extras[key] = value
    def get_platform_name(self): return "aiocqhttp"
    def is_private_chat(self): return False
    def get_group_id(self): return "group"
    def get_sender_id(self): return "member"
    def get_sender_name(self): return "member name"


class ReplyDecisionFlowTest(unittest.IsolatedAsyncioTestCase):
    def harness(self, answer=True, error=False):
        tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
        method = copy.deepcopy(next(n for n in ast.walk(tree)
            if isinstance(n, ast.AsyncFunctionDef) and n.name == "_check_ai_decision"))
        calls, records, decays = [], [], []
        async def decide(*args, **kwargs):
            calls.append((args, kwargs))
            args[1]._decision_ai_error = error
            return answer
        async def record(**kwargs): records.append(kwargs)
        async def decay(*args, **kwargs): decays.append((args, kwargs))
        async def skip(**kwargs): return False, ""
        async def interest(*args): return False, ""
        async def history(*args): return ""
        namespace = {
            "time": time,
            "logger": SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None),
            "DecisionAI": SimpleNamespace(should_reply=decide,
                get_reply_classification=lambda event: {},
                clear_reply_classification=lambda event: event.set_extra("classification", None)),
            "ProbabilityManager": SimpleNamespace(get_chat_key=lambda *args: "chat"),
            "HumanizeModeManager": SimpleNamespace(record_decision=record, should_skip_ai_decision=skip,
                check_interest_match=interest, build_decision_history_prompt=history),
            "PLUGIN_PENDING_MAIN_MODEL_DECISION": "pending_success",
            "CooldownManager": SimpleNamespace(mark_pending_decision_result=AsyncMock(return_value="promote"),
                promote_pending_to_active=AsyncMock(), clear_pending_cooldown=AsyncMock()),
        }
        module = ast.fix_missing_locations(ast.Module(body=[ast.parse("from __future__ import annotations").body[0], method], type_ignores=[]))
        exec(compile(module, "main.py", "exec"), namespace)
        plugin = SimpleNamespace(
            keyword_smart_mode=True, proactive_enabled=False, enable_memory_injection=False,
            humanize_mode_enabled=True, humanize_interest_keywords=[], enable_dynamic_reply_probability=False,
            enable_conversation_fatigue=False, enable_attention_mechanism=True, enable_reply_density_limit=False,
            decision_ai_provider_id="configured/decision", decision_ai_extra_prompt="custom", decision_ai_timeout=30,
            decision_ai_prompt_mode="append", context=object(), config={}, include_sender_info=True,
            enable_main_model_final_decision=True, enable_decision_ai_reasoning=False, decision_ai_reasoning_log=False,
            decision_ai_reasoning_log_mode="processed", judgment_reasoning_start_marker="", judgment_reasoning_end_marker="",
            decision_ai_include_persona=True, decision_ai_persona_name="configured persona", debug_mode=False,
            cooldown_enabled=False, _pre_decision_context_by_chat={"chat": "memory"},
            _apply_attention_no_reply_decay=decay, _get_message_id=lambda event: "message",
        )
        return namespace[method.name], plugin, calls, records, decays, namespace["CooldownManager"]

    async def test_classification_once_preserves_context_and_defers_success(self):
        method, plugin, calls, records, decays, _ = self.harness()
        event = Event()
        result = await method(plugin, event, "history CURRENT FOLLOWUP", False, True,
            image_urls=["original.png"], matched_trigger_keyword="机器人", original_message_text="CURRENT")
        self.assertTrue(result)
        self.assertEqual(len(calls), 1)
        args, kwargs = calls[0]
        self.assertEqual(args[2], "history CURRENT FOLLOWUP")
        self.assertEqual(args[3], "configured/decision")
        self.assertTrue(kwargs["select_reply_mode"])
        self.assertFalse(kwargs["is_preliminary_filter"])
        self.assertEqual(kwargs["configured_persona_name"], "configured persona")
        self.assertEqual(kwargs["image_urls"], ["original.png"])
        self.assertEqual(records, [])
        self.assertEqual(decays, [])
        self.assertTrue(event.get_extra("pending_success"))
        self.assertIsNone(event.get_extra("classification"))

    async def test_real_skip_and_provider_failure_have_distinct_effects(self):
        for error in (False, True):
            method, plugin, calls, records, decays, cooldown = self.harness(False, error)
            event = Event()
            self.assertFalse(await method(plugin, event, "CURRENT", False, False,
                pending_cooldown_context={"pending_before": True, "chat_key": "chat"}))
            self.assertEqual(len(calls), 1)
            self.assertEqual(len(records), 0 if error else 1)
            self.assertEqual(len(decays), 0 if error else 1)
            self.assertEqual(cooldown.promote_pending_to_active.await_count, 0 if error else 1)
            self.assertNotIn("chat", plugin._pre_decision_context_by_chat)
            self.assertFalse(event.get_extra("pending_success"))

    async def test_mentions_and_non_smart_keywords_never_add_a_judgment(self):
        for is_at, keyword in ((True, False), (False, True)):
            method, plugin, calls, records, decays, _ = self.harness()
            plugin.keyword_smart_mode = False
            event = Event()
            self.assertTrue(await method(plugin, event, "CURRENT", is_at, keyword))
            self.assertEqual(calls, [])
            self.assertEqual(records, [])
            self.assertTrue(event.get_extra("pending_success"))
            self.assertIsNone(event.get_extra("classification"))
            self.assertEqual(decays, [])

    async def test_disabled_unified_mode_keeps_one_legacy_judgment(self):
        method, plugin, calls, records, _, _ = self.harness()
        plugin.enable_main_model_final_decision = False
        self.assertTrue(await method(plugin, Event(), "CURRENT", False, False))
        self.assertEqual(len(calls), 1)
        self.assertFalse(calls[0][1]["select_reply_mode"])
        self.assertFalse(calls[0][1]["is_preliminary_filter"])
        self.assertEqual(records, [])
