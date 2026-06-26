import importlib.util
from pathlib import Path
import sys
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_tool_policy():
    module_path = REPO_ROOT / "utils" / "tool_policy.py"
    spec = importlib.util.spec_from_file_location("tool_policy_test", module_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.ToolPolicy


class ToolPolicyTest(unittest.TestCase):
    def test_filters_by_allowed_names_denied_names_plugins_and_step_image_flag(self):
        tool_policy = _load_tool_policy()
        policy = tool_policy(
            allowed_tool_names=frozenset({"search_tool", "gcp_step_image_generate"}),
            denied_tool_names=frozenset({"blocked_tool"}),
            allowed_plugin_names=frozenset({"search_plugin", "astrbot_plugin_group_chat_plus"}),
            allow_step_image=False,
        )
        tools = [
            {"name": "search_tool", "plugin": "search_plugin"},
            {"name": "blocked_tool", "plugin": "search_plugin"},
            {"name": "other_tool", "plugin": "search_plugin"},
            {"name": "search_tool", "plugin": "other_plugin"},
            {
                "name": "gcp_step_image_generate",
                "plugin": "astrbot_plugin_group_chat_plus",
            },
        ]

        self.assertEqual(policy.filter_tools(tools), [tools[0]])

    def test_prompt_allowed_names_preserve_unrestricted_mode(self):
        tool_policy = _load_tool_policy()

        unrestricted = tool_policy()
        restricted = tool_policy.from_allowed_tool_names(["b", "a"])

        self.assertIsNone(unrestricted.allowed_names_for_prompt())
        self.assertEqual(restricted.allowed_names_for_prompt(), ["a", "b"])

    def test_prompt_allowed_names_can_use_filtered_visible_tools(self):
        tool_policy = _load_tool_policy()
        policy = tool_policy.from_allowed_tool_names(
            None,
            denied_tool_names=["blocked_tool"],
            allow_step_image=False,
        )
        tools = [
            {"name": "search_tool"},
            {"name": "blocked_tool"},
            {"name": "gcp_step_image_generate"},
        ]
        visible_tools = policy.filter_tools(tools)

        self.assertEqual(
            policy.allowed_names_for_prompt(visible_tools),
            ["search_tool"],
        )

    def test_main_uses_tool_policy_for_visible_tool_filtering(self):
        main_source = (REPO_ROOT / "main.py").read_text(encoding="utf-8")

        self.assertIn("ToolPolicy", main_source)
        self.assertIn("tool_policy = ToolPolicy", main_source)
        self.assertIn("visible_tools = tool_policy.filter_tools", main_source)
        self.assertIn(
            "tool_policy.allowed_names_for_prompt(visible_tools)",
            main_source,
        )
        self.assertIn("def _filter_tool_container_for_visible_names", main_source)
        self.assertIn(
            "_filter_tool_container_for_visible_names(plugin_tool_set, visible_tool_names)",
            main_source,
        )


if __name__ == "__main__":
    unittest.main()
