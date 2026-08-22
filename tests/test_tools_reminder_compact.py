import ast
import copy
from pathlib import Path
from typing import List, Optional
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_inject_tools_to_message():
    tree = ast.parse(
        (REPO_ROOT / "utils" / "tools_reminder.py").read_text(encoding="utf-8")
    )
    tools_reminder = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "ToolsReminder"
    )
    method_names = {
        "inject_structured_tool_usage_hint",
        "inject_tools_to_message",
    }
    methods = [
        copy.deepcopy(node)
        for node in tools_reminder.body
        if isinstance(node, ast.FunctionDef) and node.name in method_names
    ]
    for method in methods:
        method.decorator_list = []
    module = ast.Module(body=methods, type_ignores=[])
    ast.fix_missing_locations(module)

    class Logger:
        def error(self, *_args, **_kwargs):
            pass

    class FakeToolsReminder:
        @staticmethod
        def get_available_tools(_context):
            return [
                {
                    "name": "search_tool",
                    "description": "search description",
                    "parameters": [],
                }
            ]

        @staticmethod
        def format_tools_info(_tools):
            return "FULL TOOL DESCRIPTION"

    namespace = {
        "Context": object,
        "List": List,
        "Optional": Optional,
        "ToolsReminder": FakeToolsReminder,
        "DEBUG_MODE": False,
        "logger": Logger(),
    }
    exec(compile(module, "tools_reminder.py", "exec"), namespace)
    FakeToolsReminder.inject_structured_tool_usage_hint = staticmethod(
        namespace["inject_structured_tool_usage_hint"]
    )
    FakeToolsReminder.inject_tools_to_message = staticmethod(
        namespace["inject_tools_to_message"]
    )
    return FakeToolsReminder


class ToolsReminderCompactTest(unittest.TestCase):
    def test_structured_tools_use_short_rule_without_full_catalog(self):
        tools_reminder = _load_inject_tools_to_message()

        try:
            result = tools_reminder.inject_tools_to_message(
                "BASE MESSAGE",
                object(),
                structured_tools_available=True,
            )
        except TypeError as exc:
            self.fail(f"structured tool mode is missing: {exc}")

        self.assertIn("BASE MESSAGE", result)
        self.assertIn("=== 工具使用规则 ===", result)
        self.assertNotIn("=== 可用工具列表 ===", result)
        self.assertNotIn("FULL TOOL DESCRIPTION", result)

    def test_text_only_mode_keeps_full_catalog(self):
        tools_reminder = _load_inject_tools_to_message()

        result = tools_reminder.inject_tools_to_message(
            "BASE MESSAGE",
            object(),
            structured_tools_available=False,
        )

        self.assertIn("=== 可用工具列表 ===", result)
        self.assertIn("FULL TOOL DESCRIPTION", result)


if __name__ == "__main__":
    unittest.main()
