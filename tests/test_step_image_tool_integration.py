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
        self.assertIn("if not self._is_enabled(event):", self.main_source)
        self.assertIn(
            "MessageEventResult().file_image(result.path).stop_event()",
            self.main_source,
        )

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


if __name__ == "__main__":
    unittest.main()
