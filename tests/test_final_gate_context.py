import ast
import copy
import re
from pathlib import Path
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_final_gate_helpers():
    tree = ast.parse(
        (REPO_ROOT / "utils" / "reply_handler.py").read_text(encoding="utf-8")
    )
    reply_handler = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "ReplyHandler"
    )
    method_names = {
        "_compact_final_decision_context",
        "_build_final_decision_gate_prompt",
    }
    methods = [
        copy.deepcopy(node)
        for node in reply_handler.body
        if isinstance(node, ast.FunctionDef) and node.name in method_names
    ]
    found_names = {node.name for node in methods}
    missing = method_names - found_names
    if missing:
        raise AssertionError(f"missing final gate helpers: {sorted(missing)}")

    for method in methods:
        method.decorator_list = []
    module = ast.Module(body=methods, type_ignores=[])
    ast.fix_missing_locations(module)

    class FakeReplyHandler:
        MAIN_MODEL_FINAL_GATE_PROMPT = "FINAL GATE"
        FINAL_GATE_MAX_CONTEXT_CHARS = 6000

    namespace = {"ReplyHandler": FakeReplyHandler, "re": re}
    exec(compile(module, "reply_handler.py", "exec"), namespace)
    FakeReplyHandler._compact_final_decision_context = staticmethod(
        namespace["_compact_final_decision_context"]
    )
    FakeReplyHandler._build_final_decision_gate_prompt = staticmethod(
        namespace["_build_final_decision_gate_prompt"]
    )
    return FakeReplyHandler


class FinalGateContextTest(unittest.TestCase):
    def test_gate_keeps_recent_history_and_current_message_only(self):
        reply_handler = _load_final_gate_helpers()
        history = "\n".join(f"成员(ID:{index}): 历史消息{index}" for index in range(12))
        formatted = (
            "=== 历史消息上下文 ===\n"
            "身份规则\n"
            f"{history}\n\n"
            "==================================================\n"
            "=== 以上全部是历史消息，你已经处理过了，不要重复回答 ===\n"
            "=== 【重要】以下是当前新消息（请优先关注这条消息的核心内容）===\n"
            "==================================================\n"
            "当前成员(ID:999): 请处理这条当前消息\n"
            "==================================================\n"
            "--- 以下是你收到这条消息后，同一用户或其他用户紧接着又发的消息 ---\n"
            "追加成员(ID:998): WAIT WINDOW FOLLOWUP\n"
            "--- 以上为紧接着的追加消息 ---\n"
            "=== 相关记忆 ===\nSECRET MEMORY\n"
            "=== 可用工具列表 ===\nSECRET TOOL SCHEMA\n"
            "=== 当前情绪 ===\nSECRET MOOD"
        )

        prompt = reply_handler._build_final_decision_gate_prompt(
            formatted_message=formatted,
            sender_emphasis="CURRENT SENDER",
        )

        self.assertIn("历史消息6", prompt)
        self.assertIn("历史消息11", prompt)
        self.assertIn("历史消息0", prompt)
        self.assertIn("请处理这条当前消息", prompt)
        self.assertIn("WAIT WINDOW FOLLOWUP", prompt)
        self.assertIn("CURRENT SENDER", prompt)
        self.assertNotIn("SECRET MEMORY", prompt)
        self.assertNotIn("SECRET TOOL SCHEMA", prompt)
        self.assertNotIn("SECRET MOOD", prompt)
        self.assertLessEqual(
            len(reply_handler._compact_final_decision_context(formatted)),
            reply_handler.FINAL_GATE_MAX_CONTEXT_CHARS,
        )

    def test_gate_caps_unstructured_context_from_the_tail(self):
        reply_handler = _load_final_gate_helpers()
        formatted = "old-prefix-" + ("x" * 7000) + "-current-tail"

        compact = reply_handler._compact_final_decision_context(formatted)

        self.assertLessEqual(len(compact), reply_handler.FINAL_GATE_MAX_CONTEXT_CHARS)
        self.assertNotIn("old-prefix", compact)
        self.assertTrue(compact.endswith("-current-tail"))

    def test_budget_keeps_whole_recent_messages_in_order(self):
        handler = _load_final_gate_helpers()
        current = (
            "=== 以上全部是历史消息，你已经处理过了，不要重复回答 ===\n"
            + "=" * 50 + "\nCURRENT\n" + "=" * 50 + "\n"
            "--- 以下是你收到这条消息后，同一用户或其他用户紧接着又发的消息 ---\n"
            "FOLLOWUP\n--- 以上为紧接着的追加消息 ---"
        )
        newest = "成员(ID:3): newest\nsecond line\n\nlast line"
        middle = "成员(ID:2): middle"
        prefix = "=== 最近群聊上下文 ===\n"
        expected = prefix + middle + "\n" + newest + "\n\n" + current
        handler.FINAL_GATE_MAX_CONTEXT_CHARS = len(expected)
        formatted = "成员(ID:1): " + "old" * 2000 + "\n" + middle + "\n" + newest + "\n" + current
        self.assertEqual(handler._compact_final_decision_context(formatted), expected)
        handler.FINAL_GATE_MAX_CONTEXT_CHARS -= 1
        compact = handler._compact_final_decision_context(formatted)
        self.assertNotIn(middle, compact)
        self.assertIn(newest, compact)
        self.assertTrue(compact.endswith(current))
        self.assertLessEqual(len(compact), handler.FINAL_GATE_MAX_CONTEXT_CHARS)

    def test_oversized_current_section_remains_bounded(self):
        handler = _load_final_gate_helpers()
        current = "=== 以上全部是历史消息，你已经处理过了，不要重复回答 ===\n" + "x" * 7000 + "TAIL"
        compact = handler._compact_final_decision_context("成员(ID:1): OLD\n" + current)
        self.assertEqual(len(compact), 6000)
        self.assertTrue(compact.endswith("TAIL"))
        self.assertNotIn("OLD", compact)

    def test_unidentified_multiline_history_is_never_partially_kept(self):
        handler = _load_final_gate_helpers()
        current = "=== 以上全部是历史消息，你已经处理过了，不要重复回答 ===\nCURRENT"
        history = "anonymous first line\nsecond line\nlast line"
        formatted = history + "\n" + current
        self.assertIn(history, handler._compact_final_decision_context(formatted))
        handler.FINAL_GATE_MAX_CONTEXT_CHARS = len(current) + 35
        compact = handler._compact_final_decision_context(formatted)
        self.assertNotIn("last line", compact)
        self.assertTrue(compact.endswith(current))

    def test_timestamp_only_and_cached_bot_messages_preserve_boundaries(self):
        handler = _load_final_gate_helpers()
        current = "=== 以上全部是历史消息，你已经处理过了，不要重复回答 ===\nCURRENT"
        recent = "【📦近期未回复】 [2026-09-05 周六 12:00:00] anonymous\ncontinuation"
        bot = "【禁止重复-你的历史回复】: prior reply\ncontinuation"
        expected = "=== 最近群聊上下文 ===\n" + bot + "\n" + recent + "\n\n" + current
        handler.FINAL_GATE_MAX_CONTEXT_CHARS = len(expected)
        formatted = "[2026-09-05 周六 11:00:00] " + "x" * 6000 + "\n" + bot + "\n" + recent + "\n" + current
        self.assertEqual(handler._compact_final_decision_context(formatted), expected)

    def test_does_not_skip_oversized_newest_message_to_include_older_one(self):
        handler = _load_final_gate_helpers()
        current = "=== 以上全部是历史消息，你已经处理过了，不要重复回答 ===\nCURRENT"
        formatted = "成员(ID:1): OLD\n成员(ID:2): " + "x" * 6000 + "\n" + current
        compact = handler._compact_final_decision_context(formatted)
        self.assertEqual(compact, "=== 最近群聊上下文 ===\n" + current)


if __name__ == "__main__":
    unittest.main()
