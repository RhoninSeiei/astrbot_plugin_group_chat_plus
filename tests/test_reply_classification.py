import asyncio
import importlib
import logging
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "reply_classification_test_utils"


def load_modules():
    package = ModuleType(PACKAGE)
    package.__path__ = [str(ROOT / "utils")]
    sys.modules[PACKAGE] = package

    astrbot = ModuleType("astrbot")
    api = ModuleType("astrbot.api")
    all_module = ModuleType("astrbot.api.all")
    all_module.logger = logging.getLogger("reply-classification-test")
    all_module.Context = object
    all_module.AstrMessageEvent = object
    injected_modules = {
        "astrbot": astrbot,
        "astrbot.api": api,
        "astrbot.api.all": all_module,
    }
    missing = object()
    previous_modules = {
        name: sys.modules.get(name, missing) for name in injected_modules
    }

    judgment = ModuleType(f"{PACKAGE}.judgment_prompts")
    judgment.build_judgment_system = lambda persona: "judgment:" + persona
    sys.modules[judgment.__name__] = judgment
    formatter = ModuleType(f"{PACKAGE}.ai_error_formatter")
    formatter.format_ai_error = lambda error, *args: "safe"
    sys.modules[formatter.__name__] = formatter
    guard = ModuleType(f"{PACKAGE}._session_guard")
    guard.sample_guard = lambda kind: None
    sys.modules[guard.__name__] = guard
    preferences = ModuleType(f"{PACKAGE}.session_preferences")
    preferences.get_session_provider = lambda context, event=None: context.provider

    async def resolve_persona(context, **kwargs):
        return {"name": "session-persona", "prompt": "persona prompt"}

    preferences.resolve_session_persona = resolve_persona
    sys.modules[preferences.__name__] = preferences
    try:
        sys.modules.update(injected_modules)
        decision = importlib.import_module(f"{PACKAGE}.decision_ai")
        reply = importlib.import_module(f"{PACKAGE}.reply_decision")
        return decision, reply
    finally:
        for name, previous in previous_modules.items():
            if previous is missing:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous


decision_module, reply_module = load_modules()
DecisionAI = decision_module.DecisionAI


class Event:
    def __init__(self, sender="1"):
        self.sender = sender
        self.extras = {}

    def get_sender_id(self):
        return self.sender

    def get_sender_name(self):
        return "tester"

    def get_extra(self, key, default=None):
        return self.extras.get(key, default)

    def set_extra(self, key, value):
        if value is None:
            self.extras.pop(key, None)
        else:
            self.extras[key] = value


class Provider:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.provider_config = {"id": "provider/test"}

    async def text_chat(self, **kwargs):
        self.calls.append(kwargs)
        value = self.responses.pop(0)
        if isinstance(value, BaseException):
            raise value
        if callable(value):
            value = await value()
        return SimpleNamespace(completion_text=value)


class Context:
    def __init__(self, provider):
        self.provider = provider
        self.persona_manager = None

    def get_provider_by_id(self, provider_id):
        return self.provider


class ReplyClassificationTest(unittest.IsolatedAsyncioTestCase):
    async def classify(self, response, event=None, **kwargs):
        event = event or Event()
        provider = Provider([response])
        extra_prompt = kwargs.pop("extra_prompt", "")
        result = await DecisionAI.should_reply(
            Context(provider), event, "CURRENT MESSAGE AND APPENDED MESSAGE",
            "", extra_prompt, select_reply_mode=True, **kwargs
        )
        return result, event, provider

    async def test_three_modes_use_one_provider_call(self):
        for mode, expected in (("skip", False), ("brief", True), ("full", True)):
            with self.subTest(mode=mode):
                result, event, provider = await self.classify(mode)
                self.assertIs(result, expected)
                self.assertEqual(len(provider.calls), 1)
                self.assertEqual(DecisionAI.get_reply_mode(event), mode)
                metadata = DecisionAI.get_reply_classification(event)
                self.assertEqual(set(metadata), {"reply_mode", "status", "provider_id", "elapsed_ms"})
                self.assertEqual(metadata["status"], "parsed")
                self.assertEqual(getattr(event, "_decision_ai_error", False), False)

    async def test_normal_skip_replaces_stale_classification_and_clears_error(self):
        event = Event()
        event.extras[DecisionAI.DECISION_REPLY_CLASSIFICATION_KEY] = {
            "reply_mode": "full", "status": "stale"
        }
        event._decision_ai_error = True
        result, event, provider = await self.classify("skip", event=event)
        self.assertFalse(result)
        self.assertEqual(len(provider.calls), 1)
        self.assertFalse(event._decision_ai_error)
        self.assertEqual(DecisionAI.get_reply_mode(event), "skip")
        self.assertEqual(DecisionAI.get_reply_classification(event)["status"], "parsed")

    async def test_protocol_failure_and_empty_response_are_errors(self):
        for response in ("", "I would choose brief because it is friendly", "yes"):
            with self.subTest(response=response):
                result, event, provider = await self.classify(response)
                self.assertFalse(result)
                self.assertEqual(len(provider.calls), 1)
                self.assertTrue(event._decision_ai_error)
                self.assertIsNone(DecisionAI.get_reply_mode(event))
                self.assertEqual(DecisionAI.get_reply_classification(event)["status"], "protocol_failed")

    async def test_timeout_marks_error_without_retry(self):
        async def delayed():
            await asyncio.sleep(1)
            return "brief"

        result, event, provider = await self.classify(delayed, timeout=0.01)
        self.assertFalse(result)
        self.assertTrue(event._decision_ai_error)
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(DecisionAI.get_reply_classification(event)["status"], "timeout")

    async def test_concurrent_events_keep_classifications_isolated(self):
        first, second = Event("first"), Event("second")
        outcomes = await asyncio.gather(
            self.classify("brief", event=first), self.classify("full", event=second)
        )
        self.assertEqual([item[0] for item in outcomes], [True, True])
        self.assertEqual(DecisionAI.get_reply_mode(first), "brief")
        self.assertEqual(DecisionAI.get_reply_mode(second), "full")
        DecisionAI.clear_reply_classification(first)
        self.assertIsNone(DecisionAI.get_reply_mode(first))
        self.assertEqual(DecisionAI.get_reply_mode(second), "full")

    async def test_override_preserves_dynamic_messages_and_mode_protocol(self):
        result, event, provider = await self.classify(
            "brief", prompt_mode="override", extra_prompt="CUSTOM OVERRIDE",
            is_preliminary_filter=True,
        )
        self.assertTrue(result)
        prompt = provider.calls[0]["prompt"]
        self.assertIn("CUSTOM OVERRIDE", prompt)
        self.assertIn("CURRENT MESSAGE AND APPENDED MESSAGE", prompt)
        self.assertIn("skip / brief / full", prompt)
        self.assertNotIn("第一道宽松筛选", prompt)

    async def test_append_old_yes_no_protocol_is_superseded_at_prompt_end(self):
        result, event, provider = await self.classify(
            "brief",
            extra_prompt="只有消息涉及摄影才参与。只输出yes或no。",
        )
        self.assertTrue(result)
        prompt = provider.calls[0]["prompt"]
        self.assertIn("只有消息涉及摄影才参与", prompt)
        authority = prompt.rfind("【本轮三态分类权威输出协议】")
        self.assertGreater(authority, prompt.find("CURRENT MESSAGE AND APPENDED MESSAGE"))
        self.assertGreater(authority, prompt.find("只输出yes或no"))
        self.assertIn("不得输出 yes、no", prompt[authority:])
        self.assertTrue(prompt.rstrip().endswith("禁止附加解释、标点、引号或代码块。"))

    async def test_override_old_reasoning_protocol_gets_authoritative_mode_tail(self):
        old_override = (
            "只有新信息才参与。\n"
            "【额外推理协议】先在 <why> 和 </why> 之间思考，最后只输出 yes / no。"
        )
        result, event, provider = await self.classify(
            "<why>有新信息</why>\nfull",
            prompt_mode="override",
            extra_prompt=old_override,
            enable_reasoning=True,
            reasoning_start_marker="<why>",
            reasoning_end_marker="</why>",
        )
        self.assertTrue(result)
        prompt = provider.calls[0]["prompt"]
        self.assertIn("只有新信息才参与", prompt)
        authority = prompt.rfind("【本轮三态分类权威输出协议】")
        self.assertGreater(authority, prompt.find("CURRENT MESSAGE AND APPENDED MESSAGE"))
        authoritative_tail = prompt[authority:]
        self.assertIn("<why>", authoritative_tail)
        self.assertIn("</why>", authoritative_tail)
        self.assertIn("skip / brief / full", authoritative_tail)
        self.assertIn("不得输出 yes、no", authoritative_tail)

    async def test_reasoning_protocol_accepts_final_mode_and_updates_choices(self):
        result, event, provider = await self.classify(
            "<why>有自然接话点</why>\nbrief", enable_reasoning=True,
            reasoning_start_marker="<why>", reasoning_end_marker="</why>",
        )
        self.assertTrue(result)
        prompt = provider.calls[0]["prompt"]
        self.assertIn("skip / brief / full", prompt)
        self.assertNotIn("yes / no", prompt)

    async def test_append_mode_keeps_persona_and_disables_tools_search_and_rate_retry(self):
        result, event, provider = await self.classify(
            "full", extra_prompt="CUSTOM APPEND"
        )
        self.assertTrue(result)
        call = provider.calls[0]
        self.assertIn("CUSTOM APPEND", call["prompt"])
        self.assertIn("CURRENT MESSAGE AND APPENDED MESSAGE", call["prompt"])
        self.assertEqual(call["system_prompt"], "judgment:persona prompt")
        self.assertIsNone(call["func_tool"])
        self.assertEqual(call["tool_choice"], "none")
        self.assertEqual(call["oauth_web_search"], "disabled")
        self.assertIs(call["retry_rate_limits"], False)

    async def test_provider_error_is_not_retried_or_treated_as_real_skip(self):
        event = Event()
        provider = Provider([RuntimeError("rate limited")])
        result = await DecisionAI.should_reply(
            Context(provider), event, "message", "", "", select_reply_mode=True
        )
        self.assertFalse(result)
        self.assertEqual(len(provider.calls), 1)
        self.assertTrue(event._decision_ai_error)
        classification = DecisionAI.get_reply_classification(event)
        self.assertEqual(classification["status"], "failed")
        self.assertIsNone(classification["reply_mode"])
        self.assertEqual(classification["provider_id"], "provider/test")

    async def test_legacy_yes_no_path_stays_compatible(self):
        for answer, expected in (("yes", True), ("no", False)):
            event = Event()
            provider = Provider([answer])
            result = await DecisionAI.should_reply(
                Context(provider), event, "message", "", "",
                select_reply_mode=False,
            )
            self.assertIs(result, expected)
            self.assertEqual(DecisionAI.get_reply_classification(event), {})
            self.assertEqual(len(provider.calls), 1)

    def test_reply_mode_instruction_is_natural_and_keeps_tools(self):
        brief = reply_module.build_reply_mode_instruction("brief")
        full = reply_module.build_reply_mode_instruction("full")
        self.assertIn("一两句", brief)
        self.assertIn("人格", brief)
        self.assertNotIn("截断", brief)
        self.assertIn("完整", full)
        self.assertIn("无需刻意长篇", full)
        for instruction in (brief, full):
            self.assertIn("工具", instruction)
            self.assertIn("结果", instruction)
        self.assertEqual(reply_module.build_reply_mode_instruction("skip"), "")

    def test_mode_prompt_assigns_queries_and_tools_to_full(self):
        prompt = reply_module.REPLY_MODE_DECISION_PROMPT
        self.assertIn("查询", prompt)
        self.assertIn("工具", prompt)
        self.assertIn("full", prompt)
        self.assertIn("正式回复模型", prompt)
        self.assertIn("单纯 emoji 不强制跳过", prompt)
        self.assertIn("有自然回应点时可选择 brief", prompt)


if __name__ == "__main__":
    unittest.main()
