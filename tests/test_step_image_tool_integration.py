from pathlib import Path
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]


class StepImageToolIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.main_source = (REPO_ROOT / "main.py").read_text(encoding="utf-8")
        self.schema_source = (REPO_ROOT / "_conf_schema.json").read_text(
            encoding="utf-8"
        )

    def test_main_registers_guarded_step_image_tools(self):
        self.assertIn(
            '@filter.llm_tool(name="gcp_step_image_generate")', self.main_source
        )
        self.assertIn('@filter.llm_tool(name="gcp_step_image_edit")', self.main_source)
        self.assertIn(
            "StepImageService.is_enabled(self.step_image_config)", self.main_source
        )
        self.assertIn("self._is_step_image_enabled_for_event(event)", self.main_source)
        self.assertIn("await self._send_step_image_progress", self.main_source)
        self.assertIn("await self._send_step_image_image_result", self.main_source)
        self.assertIn("yield None", self.main_source)

    def test_tool_sends_image_directly_without_response_stage_image_result(self):
        self.assertIn("PLUGIN_STEP_IMAGE_IMAGE_SENT", self.main_source)
        self.assertIn(
            "MessageEventResult().file_image(str(image_path))",
            self.main_source,
        )
        self.assertIn("await event.send(MessageChain(image_result.chain))", self.main_source)
        self.assertNotIn("yield self._build_step_image_direct_result", self.main_source)

    def test_step_image_guard_uses_group_id_fallbacks(self):
        self.assertIn("def _get_step_image_group_id", self.main_source)
        self.assertIn("unified_msg_origin", self.main_source)
        self.assertIn("GroupMessage", self.main_source)
        self.assertIn("if is_private and not has_group_origin:", self.main_source)
        self.assertIn("str(group_id) in enabled_groups", self.main_source)

    def test_step_image_context_removes_stale_capability_refusals(self):
        self.assertIn("def _sanitize_step_image_stale_text", self.main_source)
        self.assertIn("视觉塔", self.main_source)
        self.assertIn("画不了", self.main_source)
        self.assertIn("过期图片能力记录已省略", self.main_source)
        self.assertIn("历史中的图片能力拒绝说法属于过期记录", self.main_source)

    def test_intermediate_step_image_text_becomes_progress_message(self):
        self.assertIn("_maybe_replace_step_image_intermediate_text", self.main_source)
        self.assertIn("self._infer_step_image_action(event)", self.main_source)
        self.assertIn("阶跃星辰 Step Image Edit 2", self.main_source)
        self.assertIn("PLUGIN_STEP_IMAGE_PROGRESS_SENT", self.main_source)

    def test_tool_uses_current_message_image_for_editing(self):
        self.assertIn("async def _extract_first_current_image_path", self.main_source)
        self.assertIn("if isinstance(component, Image):", self.main_source)
        self.assertIn("await component.convert_to_file_path()", self.main_source)
        self.assertIn("请把图片和编辑要求放在同一条消息里", self.main_source)

    def test_schema_exposes_safe_step_image_settings(self):
        for key in (
            '"enable_step_image_tools"',
            '"step_image_provider_id"',
            '"step_image_model"',
            '"step_image_default_size"',
            '"step_image_timeout"',
            '"step_image_output_retention_minutes"',
        ):
            self.assertIn(key, self.schema_source)
        self.assertIn('"_special": "select_provider"', self.schema_source)
        self.assertIn('"default": "768x1360"', self.schema_source)

    def test_tool_description_requires_model_refined_prompt(self):
        self.assertIn("正式回复模型整理后的图像提示词", self.main_source)
        self.assertIn("1080p", self.main_source)
        self.assertIn("16:9", self.main_source)
        self.assertIn("工具会发送进度提示和图片结果", self.main_source)


if __name__ == "__main__":
    unittest.main()
