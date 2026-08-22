import ast
import copy
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
        FINAL_GATE_MAX_HISTORY_LINES = 6
        FINAL_GATE_MAX_CONTEXT_CHARS = 6000

    namespace = {"ReplyHandler": FakeReplyHandler}
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
        self.assertNotIn("历史消息0", prompt)
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


if __name__ == "__main__":
    unittest.main()
