import ast
import copy
from pathlib import Path
from types import SimpleNamespace
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]


class MainReplyDeclineCacheTest(unittest.TestCase):
    def test_pre_decision_state_cleanup_removes_context_and_skip_marker(self):
        source = (REPO_ROOT / "main.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        chat_plus = next(
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "ChatPlus"
        )
        method = next(
            (
                copy.deepcopy(node)
                for node in chat_plus.body
                if isinstance(node, ast.FunctionDef)
                and node.name == "_clear_pre_decision_state"
            ),
            None,
        )
        self.assertIsNotNone(method, "pre_decision cleanup helper is missing")
        method.decorator_list = []
        module = ast.Module(body=[method], type_ignores=[])
        ast.fix_missing_locations(module)
        namespace = {}
        exec(compile(module, "main.py", "exec"), namespace)
        state = SimpleNamespace(
            _pre_decision_context_by_chat={"group-1": "stale memory"},
            _ai_decision_skipped={"group-1"},
        )

        namespace["_clear_pre_decision_state"](state, "group-1")

        self.assertEqual(state._pre_decision_context_by_chat, {})
        self.assertEqual(state._ai_decision_skipped, set())

    def test_main_model_decline_uses_current_message_cache(self):
        source = (REPO_ROOT / "main.py").read_text(encoding="utf-8")
        start = source.index("    async def _handle_main_model_final_decline")
        end = source.index("    async def _generate_and_send_reply")
        function_source = source[start:end]

        self.assertIn("declined_message_cache = current_message_cache", function_source)
        self.assertIn("source=\"主模型最终判断过滤\"", function_source)
        self.assertNotIn("if cached_message_data:", function_source)

    def test_final_gate_runs_before_optional_context_injections(self):
        source = (REPO_ROOT / "main.py").read_text(encoding="utf-8")
        start = source.index("    async def _generate_and_send_reply")
        end = source.index("    async def _do_poke_after_reply")
        function_source = source[start:end]

        gate_pos = function_source.index("ReplyHandler.run_final_decision_gate")
        memory_pos = function_source.index("# 注入记忆")
        tool_pos = function_source.index("# 注入工具信息")
        mood_pos = function_source.index("# 🆕 v1.0.2: 注入情绪状态")

        self.assertLess(gate_pos, memory_pos)
        self.assertLess(gate_pos, tool_pos)
        self.assertLess(gate_pos, mood_pos)
        self.assertIn("formatted_message=formatted_context", function_source)
        self.assertIn("enable_final_decision_gate=False", function_source)
        decline_pos = function_source.index("if not should_generate_reply:")
        cleanup_pos = function_source.index(
            "self._clear_pre_decision_state(ckey)", decline_pos
        )
        decline_handler_pos = function_source.index(
            "await self._handle_main_model_final_decline", decline_pos
        )
        self.assertLess(cleanup_pos, decline_handler_pos)


if __name__ == "__main__":
    unittest.main()
