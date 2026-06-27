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


class FakeTool:
    def __init__(self, name):
        self.name = name


class RemoveToolContainer:
    def __init__(self, names):
        self.tools = [FakeTool(name) for name in names]

    def remove_tool(self, name):
        self.tools = [tool for tool in self.tools if tool.name != name]


class FuncListContainer:
    def __init__(self, names):
        self.func_list = [FakeTool(name) for name in names]

    def remove_func(self, name):
        self.func_list = [tool for tool in self.func_list if tool.name != name]


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

    def test_filters_toolset_like_container_by_visible_names(self):
        tool_policy = _load_tool_policy()
        container = RemoveToolContainer(["a", "b", "c"])

        removed = tool_policy.filter_tool_container_for_visible_names(
            container,
            {"a", "c"},
        )

        self.assertEqual([tool.name for tool in container.tools], ["a", "c"])
        self.assertEqual(removed, ["b"])

    def test_filters_func_list_container_by_visible_names(self):
        tool_policy = _load_tool_policy()
        container = FuncListContainer(["a", "b", "c"])

        removed = tool_policy.filter_tool_container_for_visible_names(
            container,
            {"b"},
        )

        self.assertEqual([tool.name for tool in container.func_list], ["b"])
        self.assertEqual(removed, ["a", "c"])

    def test_filter_container_skips_unrestricted_visible_names(self):
        tool_policy = _load_tool_policy()
        container = RemoveToolContainer(["a", "b"])

        removed = tool_policy.filter_tool_container_for_visible_names(container, None)

        self.assertEqual([tool.name for tool in container.tools], ["a", "b"])
        self.assertEqual(removed, [])

    def test_main_uses_tool_policy_for_visible_tool_filtering(self):
        main_source = (REPO_ROOT / "main.py").read_text(encoding="utf-8")

        self.assertIn("ToolPolicy", main_source)
        self.assertIn("tool_policy = ToolPolicy", main_source)
        self.assertIn("policy_visible_tools = tool_policy.filter_tools", main_source)
        self.assertIn(
            "tool_policy.allowed_names_for_prompt(policy_visible_tools)",
            main_source,
        )
        self.assertNotIn("def _filter_tool_container_for_visible_names", main_source)
        self.assertIn(
            "ToolPolicy.filter_tool_container_for_visible_names(plugin_tool_set, visible_tool_names)",
            main_source,
        )
        plugin_filter_pos = main_source.index(
            "ToolPolicy.filter_tool_container_for_visible_names(plugin_tool_set, visible_tool_names)"
        )
        req_filter_pos = main_source.index(
            "ToolPolicy.filter_tool_container_for_visible_names(req.func_tool, visible_tool_names)"
        )
        current_tools_pos = main_source.index(
            "current_tools = _get_compatible_tools(req.func_tool)"
        )
        self.assertLess(plugin_filter_pos, req_filter_pos)
        self.assertLess(req_filter_pos, current_tools_pos)
        self.assertIn("if tool_policy.is_unrestricted():", main_source)

    def test_main_decouples_tool_policy_from_tool_reminder_prompt(self):
        main_source = (REPO_ROOT / "main.py").read_text(encoding="utf-8")
        section_start = main_source.index("# 注入工具信息")
        section_end = main_source.index("# 🆕 v1.0.2: 注入情绪状态", section_start)
        tool_section = main_source[section_start:section_end]

        policy_pos = tool_section.index("tool_policy = ToolPolicy.from_allowed_tool_names")
        set_visible_pos = tool_section.index(
            "event.set_extra(PLUGIN_VISIBLE_TOOL_NAMES, visible_tool_names)"
        )
        reminder_if_pos = tool_section.index("if self.enable_tools_reminder:")
        inject_pos = tool_section.index("ToolsReminder.inject_tools_to_message")

        self.assertLess(policy_pos, reminder_if_pos)
        self.assertLess(set_visible_pos, reminder_if_pos)
        self.assertGreater(inject_pos, reminder_if_pos)
        self.assertIn("policy_visible_tools = tool_policy.filter_tools", tool_section)

    def test_main_uses_step_image_config_for_tool_policy_flag(self):
        main_source = (REPO_ROOT / "main.py").read_text(encoding="utf-8")
        policy_start = main_source.index("tool_policy = ToolPolicy.from_allowed_tool_names")
        policy_end = main_source.index("policy_visible_tools =", policy_start)
        policy_block = main_source[policy_start:policy_end]

        self.assertIn(
            "allow_step_image=StepImageService.is_enabled(self.step_image_config)",
            policy_block,
        )
        self.assertNotIn("allow_step_image=self.enable_step_image_tools", policy_block)

    def test_allowed_plugin_names_documents_executable_filter_scope(self):
        policy_source = (REPO_ROOT / "utils" / "tool_policy.py").read_text(
            encoding="utf-8"
        )

        self.assertIn("allowed_plugin_names: frozenset[str] = frozenset()", policy_source)
        self.assertIn("executable ToolSet filtering", policy_source)


if __name__ == "__main__":
    unittest.main()
