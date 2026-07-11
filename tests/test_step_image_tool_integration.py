import ast
import json
from pathlib import Path
from typing import Optional
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]


class StepImageToolIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.main_source = (REPO_ROOT / "main.py").read_text(encoding="utf-8")
        self.main_tree = ast.parse(self.main_source)
        self.chat_plus_node = next(
            node
            for node in self.main_tree.body
            if isinstance(node, ast.ClassDef) and node.name == "ChatPlus"
        )
        self.schema_source = (REPO_ROOT / "_conf_schema.json").read_text(
            encoding="utf-8"
        )

    def _method_node(self, name):
        matches = [
            node
            for node in self.chat_plus_node.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == name
        ]
        self.assertEqual(len(matches), 1, f"ChatPlus.{name} 应当唯一")
        return matches[0]

    def _method_source(self, name):
        return ast.get_source_segment(self.main_source, self._method_node(name))

    def _compile_unbound_method(self, name):
        method_node = self._method_node(name)
        module = ast.Module(body=[method_node], type_ignores=[])
        ast.fix_missing_locations(module)
        namespace = {"Optional": Optional}
        exec(compile(module, "main.py", "exec"), namespace)
        return namespace[name]

    def _assert_group_image_error_branches(self, method_name):
        handler_names = {
            handler.type.id
            for handler in ast.walk(self._method_node(method_name))
            if isinstance(handler, ast.ExceptHandler)
            and isinstance(handler.type, ast.Name)
        }
        for error_name in (
            "GroupImageUserError",
            "GroupImageConfigError",
            "GroupImageProviderError",
        ):
            self.assertIn(error_name, handler_names)

    def test_main_registers_guarded_step_image_tools(self):
        self.assertIn(
            '@filter.llm_tool(name="gcp_step_image_generate")', self.main_source
        )
        self.assertIn('@filter.llm_tool(name="gcp_step_image_edit")', self.main_source)
        self.assertIn(
            "GroupImageService.is_enabled(self.step_image_config)", self.main_source
        )
        self.assertIn("self._is_step_image_enabled_for_event(event)", self.main_source)
        self.assertIn("await self._send_step_image_progress", self.main_source)
        self.assertIn("await self._send_step_image_image_result", self.main_source)

    def test_successful_step_image_tools_return_model_facing_result(self):
        for method_name in ("gcp_step_image_generate", "gcp_step_image_edit"):
            method_source = self._method_source(method_name)
            self.assertIn(
                "yield self._build_step_image_tool_result_text", method_source
            )
        self.assertNotIn(
            "The tool has no return value, or has sent the result directly to the user.",
            self.main_source,
        )

    def test_step_image_tool_records_hit_status_and_model_facing_result(self):
        for marker in (
            "PLUGIN_STEP_IMAGE_TOOL_HIT",
            "PLUGIN_STEP_IMAGE_TOOL_STATUS",
            "PLUGIN_STEP_IMAGE_TOOL_MESSAGE",
            "def _mark_step_image_tool_result",
            "def _build_step_image_tool_result_text",
        ):
            self.assertIn(marker, self.main_source)

        for status in ('status="success"', 'status="failed"'):
            self.assertIn(status, self.main_source)

        self.assertIn("群聊图片工具", self.main_source)
        self.assertIn("自然语言", self.main_source)
        self.assertIn("先提交工具参数并等待工具结果", self.main_source)
        self.assertIn("成功时图片由工具发送一次", self.main_source)
        self.assertIn("禁止输出工具协议、参数、Provider ID", self.main_source)
        self.assertIn(
            'return f"群聊图片工具 {action_label}{status_label}：{result_message}"',
            self.main_source,
        )
        self.assertIn(
            'return f"群聊图片工具 {action}{status_label}: {safe_message}"',
            self.main_source,
        )

    def test_step_image_tool_history_uses_safe_status_summary(self):
        self.assertIn("def _build_step_image_history_summary", self.main_source)
        self.assertIn("func_name in STEP_IMAGE_TOOL_NAMES", self.main_source)
        self.assertIn('func_args = "{...}"', self.main_source)
        self.assertIn("event.get_extra(PLUGIN_STEP_IMAGE_TOOL_STATUS", self.main_source)
        self.assertIn("event.get_extra(PLUGIN_STEP_IMAGE_TOOL_MESSAGE", self.main_source)

    def test_tool_sends_image_directly_without_response_stage_image_result(self):
        method_source = self._method_source("_send_step_image_image_result")
        self.assertEqual(
            method_source.count("MessageEventResult().file_image(str(image_path))"),
            1,
        )
        self.assertEqual(
            method_source.count("await event.send(MessageChain(image_result.chain))"),
            1,
        )
        self.assertEqual(
            method_source.count(
                "event.set_extra(PLUGIN_STEP_IMAGE_IMAGE_SENT, True)"
            ),
            1,
        )
        self.assertIn("图片结果已通过工具发送", method_source)
        self.assertNotIn("yield self._build_step_image_direct_result", method_source)

    def test_step_image_guard_uses_group_id_fallbacks(self):
        self.assertIn("def _get_step_image_group_id", self.main_source)
        self.assertIn("unified_msg_origin", self.main_source)
        self.assertIn("GroupMessage", self.main_source)
        self.assertIn("if is_private and not has_group_origin:", self.main_source)
        self.assertIn("str(group_id) in enabled_groups", self.main_source)

    def test_step_image_context_removes_stale_capability_refusals(self):
        self.assertIn("STEP_IMAGE_STALE_CAPABILITY_PLACEHOLDER", self.main_source)
        self.assertIn("STEP_IMAGE_STALE_CAPABILITY_TERMS", self.main_source)
        self.assertIn("def _sanitize_step_image_stale_text", self.main_source)
        self.assertIn("历史中的图片能力拒绝说法属于过期记录", self.main_source)

    def test_intermediate_step_image_text_becomes_progress_message(self):
        replace_source = self._method_source(
            "_maybe_replace_step_image_intermediate_text"
        )
        append_source = self._method_source("_append_step_image_pending_progress")

        self.assertIn("self._infer_step_image_action(event)", replace_source)
        self.assertIn("self._build_step_image_progress_text(action)", replace_source)
        self.assertIn("PLUGIN_STEP_IMAGE_PROGRESS_SENT", replace_source)
        self.assertIn(
            "pending_replies[-1] != progress_text",
            append_source,
        )
        self.assertNotIn("reply_text", append_source)

    def test_progress_text_uses_dynamic_backend_display_name(self):
        class FakeService:
            @staticmethod
            def display_name():
                return "OpenAI Codex 图像生成服务"

        class MinimalChatPlus:
            @staticmethod
            def _get_step_image_service():
                return FakeService()

        build_progress_text = self._compile_unbound_method(
            "_build_step_image_progress_text"
        )
        plugin = MinimalChatPlus()

        self.assertEqual(
            build_progress_text(plugin),
            "正在用 OpenAI Codex 图像生成服务生成图片，稍等一下。",
        )
        self.assertEqual(
            build_progress_text(plugin, "edit"),
            "正在用 OpenAI Codex 图像生成服务编辑这张图，稍等一下。",
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

    def test_schema_exposes_configurable_image_backends(self):
        schema = json.loads(self.schema_source)
        self.assertEqual(schema["image_tool_backend"]["default"], "codex_oauth")
        self.assertEqual(
            schema["image_tool_backend"]["options"], ["codex_oauth", "stepfun"]
        )
        self.assertEqual(
            schema["codex_oauth_image_provider_id"]["default"],
            "openai_oauth/gpt-5.6-sol",
        )
        self.assertEqual(
            schema["codex_oauth_image_provider_id"]["_special"], "select_provider"
        )
        self.assertEqual(
            schema["codex_oauth_image_model"]["default"], "gpt-5.6-sol"
        )
        self.assertEqual(
            schema["codex_oauth_image_default_size"]["options"],
            ["1024x1024", "1536x1024", "1024x1536"],
        )
        self.assertEqual(schema["codex_oauth_image_timeout"]["default"], 300)

    def test_main_routes_existing_tools_through_group_image_service(self):
        service_factory_source = self._method_source("_get_step_image_service")
        self.assertIn(
            "GroupImageService.is_enabled(self.step_image_config)", self.main_source
        )
        self.assertIn("return GroupImageService(", service_factory_source)
        self.assertIn(
            '"image_tool_backend": config.get("image_tool_backend"),',
            self.main_source,
        )
        self.assertNotIn('config.get("image_tool_backend",', self.main_source)

    def test_generate_routes_errors_size_and_single_image_send(self):
        method_source = self._method_source("gcp_step_image_generate")
        self._assert_group_image_error_branches("gcp_step_image_generate")
        self.assertEqual(
            method_source.count('size=str(size or "").strip(),'),
            1,
        )
        self.assertEqual(
            method_source.count(
                "await self._send_step_image_image_result(event, result.path)"
            ),
            1,
        )

    def test_edit_routes_errors_and_single_image_send(self):
        method_source = self._method_source("gcp_step_image_edit")
        self._assert_group_image_error_branches("gcp_step_image_edit")
        self.assertEqual(
            method_source.count(
                "await self._send_step_image_image_result(event, result.path)"
            ),
            1,
        )

    def test_tool_description_requires_model_refined_prompt(self):
        self.assertIn("正式回复模型整理后的图像提示词", self.main_source)
        self.assertIn("1080p", self.main_source)
        self.assertIn("16:9", self.main_source)
        self.assertIn("工具返回结果后", self.main_source)
        self.assertIn("根据工具结果", self.main_source)
        self.assertIn("自然语言", self.main_source)
        self.assertNotIn("工具会发送进度提示和图片结果。", self.main_source)


if __name__ == "__main__":
    unittest.main()
