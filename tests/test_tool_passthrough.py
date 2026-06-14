from pathlib import Path
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]


class ToolPassthroughIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.main_source = (REPO_ROOT / "main.py").read_text(encoding="utf-8")
        self.reply_source = (REPO_ROOT / "utils" / "reply_handler.py").read_text(
            encoding="utf-8"
        )
        self.decision_source = (REPO_ROOT / "utils" / "decision_ai.py").read_text(
            encoding="utf-8"
        )

    def test_formal_reply_uses_astrbot_request_llm_for_tool_loop(self):
        self.assertIn("return event.request_llm(", self.reply_source)
        self.assertIn("func_tool_manager=func_tools_mgr", self.reply_source)
        self.assertIn("tool_set=plugin_tool_set", self.reply_source)
        self.assertNotIn(
            "await ReplyHandler._request_with_astrbot_fallback(\n"
            "                    event,\n"
            "                    context,\n"
            "                    req,\n"
            "                )",
            self.reply_source,
        )

    def test_on_llm_request_merges_platform_and_plugin_tools(self):
        self.assertIn("plugin_tools = _get_compatible_tools(plugin_tool_set)", self.main_source)
        self.assertIn("req.func_tool.merge(plugin_tool_set)", self.main_source)
        self.assertIn("req.func_tool.add_tool(tool)", self.main_source)
        self.assertIn("req.func_tool.func_list.append(tool)", self.main_source)
        self.assertNotIn(
            "req.func_tool = plugin_tool_set  # 可能是 ToolSet 或 None",
            self.main_source,
        )
        self.assertIn("TOOL_CALL_PROMPT", self.main_source)

    def test_formal_reply_expands_plugin_scope_for_tool_owner_filter(self):
        self.assertIn("PLUGIN_ORIGINAL_PLUGINS_NAME", self.reply_source)
        self.assertIn(
            "_expand_event_plugins_name_for_tool_access(event, plugin_tool_set)",
            self.reply_source,
        )
        self.assertIn("handler_module_path", self.reply_source)
        self.assertIn("star_map", self.reply_source)

    def test_on_llm_request_restores_original_plugin_scope(self):
        self.assertIn("PLUGIN_ORIGINAL_PLUGINS_NAME", self.main_source)
        self.assertIn("event.plugins_name = original_plugins_name", self.main_source)

    def test_judgment_ai_keeps_tools_disabled(self):
        self.assertIn("func_tool=None", self.decision_source)
        self.assertIn("gate_req = ProviderRequest(", self.reply_source)
        gate_block = self.reply_source.split("gate_req = ProviderRequest(", 1)[1].split(
            "try:", 1
        )[0]
        self.assertNotIn("func_tool=", gate_block)


if __name__ == "__main__":
    unittest.main()
