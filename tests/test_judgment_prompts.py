import asyncio
import ast
import json
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("judgment_prompts", ROOT / "utils/judgment_prompts.py")
prompts = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prompts)


class JudgmentPromptTest(unittest.IsolatedAsyncioTestCase):
    async def test_judgment_retains_history_but_not_generation_instructions(self):
        async def formatter(history, current, bot_id, **options):
            return f"{bot_id}: " + "\n".join(history) + "\n" + current
        generation, judgment = await prompts.format_proactive_contexts(
            formatter, ["用户：最近在玩什么游戏？", "机器人：之前谈过天气。"],
            "GENERATION_ONLY：直接生成群聊回复并调用工具", "bot", need_judgment=True,
        )
        final, system = prompts.build_proactive_judgment_messages(judgment, "喜欢游戏", prompts.PROACTIVE_JUDGMENT_PROMPT)
        self.assertIn("GENERATION_ONLY", generation)
        self.assertNotIn("GENERATION_ONLY", final)
        self.assertIn("最近在玩什么游戏", final)
        self.assertIn("之前谈过天气", final)
        self.assertIn("喜欢游戏", system)
        self.assertIn("不是角色对话或回复生成", system)

    async def test_disabled_judgment_does_not_reformat_generation(self):
        calls = []
        async def formatter(history, current, bot_id, **options):
            calls.append(current)
            return current
        result = await prompts.format_proactive_contexts(formatter, [], "原生成请求", "bot", need_judgment=False)
        self.assertEqual(result, ("原生成请求", ""))
        self.assertEqual(calls, ["原生成请求"])

    async def test_custom_judgment_instruction_is_preserved(self):
        prompt, system = prompts.build_proactive_judgment_messages("历史数据", "", "仅考虑游戏话题，回答 yes/no")
        self.assertIn("仅考虑游戏话题", prompt)
        self.assertIn("历史数据", prompt)
        self.assertIn("时段、频率、沉默间隔和冷却由程序处理", system)

    async def test_real_message_shape_preserves_unanswered_history_identity_and_additions(self):
        messages = [
            SimpleNamespace(message_str="游戏更新了", message_id="cached_123", sender=SimpleNamespace(user_id="42", nickname="成员"), timestamp=100),
            SimpleNamespace(message_str="此前说过的观点", sender=SimpleNamespace(user_id="99", nickname="机器人"), timestamp=101),
        ]
        async def reply_formatter(*args, **kwargs):
            return "以上全部是历史消息，你已经处理过了。GENERATION_ONLY"
        generation, judgment = await prompts.format_proactive_contexts(
            reply_formatter, messages, "生成一句回复", "99", need_judgment=True,
            window_buffered_messages=[{"sender_id": "43", "sender_name": "另一成员", "content": "新补充", "timestamp": 102}],
        )
        data = json.loads(judgment)
        self.assertTrue(data["recent_history"][0]["is_unanswered"])
        self.assertEqual(data["recent_history"][0]["sender_id"], "42")
        self.assertTrue(data["recent_history"][1]["is_bot"])
        self.assertEqual(data["recent_additions"][0]["content"], "新补充")
        judgment = prompts.add_judgment_memories(judgment, "成员42喜欢游戏")
        final, _ = prompts.build_proactive_judgment_messages(judgment, "", prompts.PROACTIVE_JUDGMENT_PROMPT)
        for forbidden in ("已经处理过了", "优先关注当前新消息", "GENERATION_ONLY", "自然地融入到你的回答"):
            self.assertNotIn(forbidden, final)
        self.assertIn("成员42喜欢游戏", final)
        self.assertIn("GENERATION_ONLY", generation)

    async def test_judgment_history_respects_display_options_and_strips_tool_records(self):
        data = json.loads(prompts.format_judgment_history(
            [{"content": "正文<tool>协议</tool>", "sender_id": "7", "timestamp": 100}], "7",
            include_sender_info=False, include_timestamp=False,
            stripper=lambda text: text.split("<tool>")[0],
        ))["recent_history"][0]
        self.assertEqual(data, {"content": "正文", "is_bot": True})

    async def test_fatigue_hints_only_check_content_and_closure(self):
        tree = ast.parse((ROOT / "utils/decision_ai.py").read_text(encoding="utf-8"))
        branch = next(n for n in ast.walk(tree) if isinstance(n, ast.If)
                      and isinstance(n.test, ast.BoolOp)
                      and isinstance(n.test.values[0], ast.Name)
                      and n.test.values[0].id == "conversation_fatigue_info")
        function = ast.parse('def render(conversation_fatigue_info):\n    enhanced_context = ""\n    return enhanced_context').body[0]
        function.body.insert(1, branch)
        namespace = {}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[])), "fatigue-test", "exec"), namespace)
        for level in ("light", "medium", "heavy"):
            text = namespace["render"]({"enabled": True, "consecutive_replies": 9, "fatigue_level": level})
            self.assertIn("9", text)
            self.assertNotIn("只对重要", text)
            self.assertNotIn("减少回复频率", text)
            self.assertNotIn("除非消息非常重要", text)

    async def test_active_path_uses_separate_judgment_context(self):
        source = (ROOT / "utils/proactive_chat_manager.py").read_text(encoding="utf-8")
        self.assertTrue("format_proactive_contexts(" in source)
        self.assertTrue("build_proactive_judgment_messages(" in source)
        self.assertTrue("judgment_context, judge_persona_prompt, judge_prompt_text" in source)
        self.assertFalse('+ saved_final_message' in source)

    async def test_schema_and_runtime_default_agree(self):
        schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
        self.assertEqual(schema["proactive_ai_judge_prompt"]["default"], prompts.PROACTIVE_JUDGMENT_PROMPT)

    async def test_contract_allows_small_talk_but_keeps_concrete_vetoes(self):
        prompt = prompts.PROACTIVE_JUDGMENT_PROMPT
        for rule in ("无需等待别人明确提问", "简短轻松互动", "重复刚说过的内容", "明确要求停止", "私人交流边界", "不能单独作为拒绝理由"):
            self.assertIn(rule, prompt)
        self.assertIn("不要再次以时间早晚", prompt)

    async def test_both_reply_judges_use_judgment_system(self):
        for filename, target in (("decision_ai.py", "call_decision_ai"), ("reply_handler.py", "_run_final_decision_gate")):
            tree = ast.parse((ROOT / "utils" / filename).read_text(encoding="utf-8"))
            method = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)
                          and n.name == target and (target != "call_decision_ai" or not n.args.args))
            system_args = [kw.value for n in ast.walk(method) if isinstance(n, ast.Call)
                           for kw in n.keywords if kw.arg == "system_prompt"]
            self.assertTrue(any(isinstance(value, ast.Call) and isinstance(value.func, ast.Name)
                                and value.func.id == "build_judgment_system" for value in system_args))


if __name__ == "__main__":
    unittest.main()
