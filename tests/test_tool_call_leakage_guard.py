from pathlib import Path
import importlib.util
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]
TOOL_GUARD_PATH = REPO_ROOT / "utils" / "tool_call_leakage_guard.py"

_spec = importlib.util.spec_from_file_location(
    "tool_call_leakage_guard", TOOL_GUARD_PATH
)
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)
sanitize_tool_call_markup = _module.sanitize_tool_call_markup


class ToolCallLeakageGuardTest(unittest.TestCase):
    def test_blocks_pure_tool_call_markup(self):
        result = sanitize_tool_call_markup(
            "<tool_call>\n<function=pixiv_search_illust>\n"
        )

        self.assertTrue(result.had_markup)
        self.assertTrue(result.should_block)
        self.assertEqual("", result.sanitized_text)

    def test_strips_tool_markup_and_keeps_user_facing_text(self):
        result = sanitize_tool_call_markup(
            "<tool_call> <function\npixiv_search_illust>\n搜不了，自个翻Pixiv去。"
        )

        self.assertTrue(result.had_markup)
        self.assertFalse(result.should_block)
        self.assertEqual("搜不了，自个翻Pixiv去。", result.sanitized_text)

    def test_blocks_tool_json_payload(self):
        result = sanitize_tool_call_markup(
            '<tool_call>\n<function=pixiv_search_illust>\n{"query":"Atlanta"}'
        )

        self.assertTrue(result.had_markup)
        self.assertTrue(result.should_block)
        self.assertEqual("", result.sanitized_text)

    def test_plain_text_is_unchanged(self):
        result = sanitize_tool_call_markup("搜不了，自个翻Pixiv去。")

        self.assertFalse(result.had_markup)
        self.assertFalse(result.should_block)
        self.assertEqual("搜不了，自个翻Pixiv去。", result.sanitized_text)

class ToolCallLeakageIntegrationSourceTest(unittest.TestCase):
    def setUp(self):
        self.main_source = (REPO_ROOT / "main.py").read_text(encoding="utf-8")
        self.context_source = (REPO_ROOT / "utils" / "context_manager.py").read_text(
            encoding="utf-8"
        )

    def test_decorating_stage_filters_tool_markup_before_saving(self):
        guard_pos = self.main_source.index("sanitize_tool_call_markup(reply_text)")
        brevity_pos = self.main_source.index(
            "brief_text = ReplyHandler._apply_group_chat_brevity_limit"
        )
        cache_pos = self.main_source.index("self.raw_reply_cache[message_id] = reply_text")

        self.assertLess(guard_pos, brevity_pos)
        self.assertLess(guard_pos, cache_pos)
        self.assertIn("[工具调用外显防护]", self.main_source)

    def test_llm_request_logs_visible_and_executable_tool_delta(self):
        self.assertIn("_get_tool_name_set", self.main_source)
        self.assertIn("_log_tool_visibility_delta", self.main_source)
        self.assertIn("提示工具与执行工具不一致", self.main_source)

    def test_tool_call_records_are_sanitized_before_history_exposure(self):
        self.assertIn("def strip_tool_call_record_blocks", self.context_source)
        self.assertIn(
            "message_content = ContextManager.strip_tool_call_record_blocks(",
            self.context_source,
        )
        self.assertIn(
            "bot_message = ContextManager.strip_tool_call_record_blocks(",
            self.context_source,
        )
        self.assertIn(
            "cached_content = cached_msg[\"content\"]",
            self.context_source,
        )
        self.assertIn("strip_tool_call_record_blocks", self.context_source)

    def test_group_flow_does_not_wrap_event_send_for_tool_status(self):
        self.assertNotIn("def _install_tool_status_send_filter", self.main_source)
        self.assertNotIn("_restore_tool_status_send_filter", self.main_source)
        self.assertNotIn("is_tool_status_payload(", self.main_source)
        self.assertNotIn("setattr(event, \"send\"", self.main_source)


if __name__ == "__main__":
    unittest.main()
