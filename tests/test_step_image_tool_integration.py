import ast
import asyncio
import copy
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Optional
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]


class GroupImageUserError(Exception):
    pass


class GroupImageConfigError(Exception):
    pass


class GroupImageProviderError(Exception):
    pass


class RecordingLogger:
    def __init__(self):
        self.records = []

    def _record(self, level, message, args, kwargs):
        self.records.append((level, str(message), args, kwargs))

    def info(self, message, *args, **kwargs):
        self._record("info", message, args, kwargs)

    def warning(self, message, *args, **kwargs):
        self._record("warning", message, args, kwargs)

    def error(self, message, *args, **kwargs):
        self._record("error", message, args, kwargs)


class FakeMessageChain:
    def __init__(self, chain=None):
        self.chain = list(chain or [])

    def message(self, text):
        self.chain.append(("text", text))
        return self


class FakeMessageEventResult:
    def __init__(self):
        self.chain = []

    def file_image(self, path):
        self.chain = [("image", path)]
        return self


class FakeEvent:
    def __init__(self, order):
        self.extras = {}
        self.order = order
        self.sent = []

    def get_extra(self, key, default=None):
        return self.extras.get(key, default)

    def set_extra(self, key, value):
        if value is None:
            self.extras.pop(key, None)
        else:
            self.extras[key] = value

    async def send(self, chain):
        payload = list(chain.chain)
        self.sent.append(payload)
        self.order.append(payload[0][0])


class RecordingFacade:
    def __init__(self, order, error=None):
        self.order = order
        self.error = error
        self.calls = []

    async def generate(self, **kwargs):
        self.calls.append(("generate", kwargs))
        self.order.append("facade_generate")
        if self.error is not None:
            raise self.error
        return SimpleNamespace(path="generated.png")

    async def edit(self, **kwargs):
        self.calls.append(("edit", kwargs))
        self.order.append("facade_edit")
        if self.error is not None:
            raise self.error
        return SimpleNamespace(path="edited.png")


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

    def _module_function_node(self, name):
        matches = [
            node
            for node in self.main_tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == name
        ]
        self.assertEqual(len(matches), 1, f"main.{name} 应当唯一")
        return matches[0]

    def _compile_module_function(self, name, namespace=None):
        function_node = self._module_function_node(name)
        module = ast.Module(body=[function_node], type_ignores=[])
        ast.fix_missing_locations(module)
        namespace = dict(namespace or {})
        exec(compile(module, "main.py", "exec"), namespace)
        return namespace[name]

    def _compile_unbound_method(self, name):
        method_node = self._method_node(name)
        module = ast.Module(body=[method_node], type_ignores=[])
        ast.fix_missing_locations(module)
        namespace = {"Optional": Optional}
        exec(compile(module, "main.py", "exec"), namespace)
        return namespace[name]

    def _compile_tool_methods(self):
        method_names = (
            "_mark_step_image_progress_sent",
            "_mark_step_image_tool_result",
            "_build_step_image_tool_result_text",
            "_send_step_image_progress",
            "_send_step_image_image_result",
            "gcp_step_image_generate",
            "gcp_step_image_edit",
        )
        nodes = []
        for name in method_names:
            node = copy.deepcopy(self._method_node(name))
            node.decorator_list = []
            nodes.append(node)
        module = ast.Module(
            body=[
                ast.ImportFrom(
                    module="__future__",
                    names=[ast.alias(name="annotations")],
                    level=0,
                ),
                *nodes,
            ],
            type_ignores=[],
        )
        ast.fix_missing_locations(module)
        logger = RecordingLogger()
        namespace = {
            "GroupImageUserError": GroupImageUserError,
            "GroupImageConfigError": GroupImageConfigError,
            "GroupImageProviderError": GroupImageProviderError,
            "MessageChain": FakeMessageChain,
            "MessageEventResult": FakeMessageEventResult,
            "Path": Path,
            "logger": logger,
            "sanitize_tool_call_markup": lambda value: SimpleNamespace(
                sanitized_text=str(value or "")
            ),
            "PLUGIN_STEP_IMAGE_PROGRESS_SENT": "progress_sent",
            "PLUGIN_STEP_IMAGE_IMAGE_SENT": "image_sent",
            "PLUGIN_STEP_IMAGE_ACTION": "action",
            "PLUGIN_STEP_IMAGE_TOOL_HIT": "tool_hit",
            "PLUGIN_STEP_IMAGE_TOOL_STATUS": "tool_status",
            "PLUGIN_STEP_IMAGE_TOOL_MESSAGE": "tool_message",
        }
        exec(compile(module, "main.py", "exec"), namespace)
        return namespace, logger

    def _make_tool_harness(self, facade, *, current_image="current.png"):
        namespace, logger = self._compile_tool_methods()

        class ToolHarness:
            pass

        for name, value in namespace.items():
            if name.startswith("_") or name.startswith("gcp_step_image_"):
                if callable(value):
                    setattr(ToolHarness, name, value)

        harness = ToolHarness()
        harness._step_image_guard = lambda event: None
        harness._build_step_image_progress_text = (
            lambda action: f"progress:{action}"
        )
        harness._append_step_image_pending_progress = lambda event, text: None
        harness._cleanup_step_image_outputs = lambda: facade.order.append("cleanup")
        harness._get_step_image_service = lambda: facade
        harness._get_step_image_group_id = lambda event: "group-1"

        async def extract_current_image(event):
            return current_image

        harness._extract_first_current_image_path = extract_current_image
        return harness, logger

    @staticmethod
    def _collect_tool_results(generator):
        async def collect():
            return [item async for item in generator]

        return asyncio.run(collect())

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
        self.assertEqual(schema["image_tool_backend_config_version"]["type"], "int")
        self.assertEqual(schema["image_tool_backend_config_version"]["default"], 0)

    def test_backend_config_migration_distinguishes_new_and_existing_configs(self):
        class RecordingLogger:
            def __init__(self):
                self.records = []

            def info(self, message, *args):
                self.records.append(("info", message, args))

            def warning(self, message, *args):
                self.records.append(("warning", message, args))

        class FakeConfig(dict):
            def __init__(self, values, *, first_deploy_marker=None, fail_save=False):
                super().__init__(values)
                if first_deploy_marker is not None:
                    self.first_deploy = first_deploy_marker
                self.fail_save = fail_save
                self.save_count = 0

            def save_config(self):
                self.save_count += 1
                if self.fail_save:
                    raise RuntimeError(
                        "save exposed sensitive-token-value at C:\\private\\config.json"
                    )

        logger = RecordingLogger()
        migrate = self._compile_module_function(
            "_migrate_image_tool_backend_config",
            {
                "AstrBotConfig": FakeConfig,
                "logger": logger,
            },
        )

        new_config = FakeConfig(
            {
                "image_tool_backend": "codex_oauth",
                "image_tool_backend_config_version": 0,
            },
            first_deploy_marker=True,
        )
        old_config = FakeConfig(
            {
                "image_tool_backend": "codex_oauth",
                "image_tool_backend_config_version": 0,
            }
        )
        migrated_config = FakeConfig(
            {
                "image_tool_backend": "codex_oauth",
                "image_tool_backend_config_version": 1,
            }
        )

        self.assertEqual(migrate(new_config), "codex_oauth")
        self.assertEqual(new_config["image_tool_backend_config_version"], 1)
        self.assertEqual(new_config.save_count, 1)

        self.assertEqual(migrate(old_config), "stepfun")
        self.assertEqual(old_config["image_tool_backend"], "stepfun")
        self.assertEqual(old_config["image_tool_backend_config_version"], 1)
        self.assertEqual(old_config.save_count, 1)

        self.assertEqual(migrate(migrated_config), "codex_oauth")
        self.assertEqual(migrated_config.save_count, 0)

    def test_backend_config_migration_uses_runtime_choice_when_save_fails(self):
        class RecordingLogger:
            def __init__(self):
                self.records = []

            def warning(self, message, *args):
                self.records.append((message, args))

            def info(self, message, *args):
                self.records.append((message, args))

        class FailingConfig(dict):
            def save_config(self):
                raise RuntimeError(
                    "save exposed sensitive-token-value at C:\\private\\config.json"
                )

        logger = RecordingLogger()
        migrate = self._compile_module_function(
            "_migrate_image_tool_backend_config",
            {
                "AstrBotConfig": FailingConfig,
                "logger": logger,
            },
        )
        config = FailingConfig(
            {
                "image_tool_backend": "codex_oauth",
                "image_tool_backend_config_version": 0,
            }
        )

        self.assertEqual(migrate(config), "stepfun")
        rendered_logs = repr(logger.records)
        self.assertIn("GROUP_IMAGE_BACKEND_MIGRATION_SAVE_FAILED", rendered_logs)
        self.assertNotIn("sensitive-token-value", rendered_logs)
        self.assertNotIn("private", rendered_logs.lower())

    def test_main_routes_existing_tools_through_group_image_service(self):
        service_factory_source = self._method_source("_get_step_image_service")
        self.assertIn(
            "GroupImageService.is_enabled(self.step_image_config)", self.main_source
        )
        self.assertIn("return GroupImageService(", service_factory_source)
        self.assertIn(
            "runtime_image_tool_backend = _migrate_image_tool_backend_config(config)",
            self.main_source,
        )
        self.assertIn(
            '"image_tool_backend": runtime_image_tool_backend,',
            self.main_source,
        )

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

    def test_generate_tool_executes_progress_facade_image_and_summary(self):
        order = []
        facade = RecordingFacade(order)
        harness, _logger = self._make_tool_harness(facade)
        event = FakeEvent(order)

        results = self._collect_tool_results(
            harness.gcp_step_image_generate(event, prompt="orange cat", size="")
        )

        self.assertEqual(
            order,
            ["text", "cleanup", "facade_generate", "image"],
        )
        self.assertEqual(
            facade.calls,
            [("generate", {"prompt": "orange cat", "size": ""})],
        )
        self.assertEqual(len(event.sent), 2)
        self.assertEqual(event.sent[0], [("text", "progress:generate")])
        self.assertEqual(event.sent[1], [("image", "generated.png")])
        self.assertTrue(event.extras["progress_sent"])
        self.assertTrue(event.extras["image_sent"])
        self.assertTrue(event.extras["tool_hit"])
        self.assertEqual(event.extras["action"], "generate")
        self.assertEqual(event.extras["tool_status"], "success")
        self.assertEqual(event.extras["tool_message"], "图片已经发送到群聊。")
        self.assertEqual(
            results,
            ["群聊图片工具 图片生成成功：图片已经发送到群聊。"],
        )

    def test_edit_tool_executes_with_current_image_and_safe_summary(self):
        order = []
        facade = RecordingFacade(order)
        harness, _logger = self._make_tool_harness(
            facade, current_image="current-source.png"
        )
        event = FakeEvent(order)

        results = self._collect_tool_results(
            harness.gcp_step_image_edit(event, prompt="change the sky")
        )

        self.assertEqual(order, ["text", "cleanup", "facade_edit", "image"])
        self.assertEqual(
            facade.calls,
            [
                (
                    "edit",
                    {
                        "prompt": "change the sky",
                        "image_path": "current-source.png",
                    },
                )
            ],
        )
        self.assertEqual(len(event.sent), 2)
        self.assertEqual(event.extras["action"], "edit")
        self.assertEqual(event.extras["tool_status"], "success")
        self.assertEqual(
            results,
            ["群聊图片工具 图片编辑成功：图片已经发送到群聊。"],
        )

    def test_tool_failure_branches_send_no_image_and_return_safe_summaries(self):
        cases = (
            (GroupImageUserError("图片尺寸无效。"), "图片尺寸无效。"),
            (
                GroupImageConfigError(
                    "config exposed sensitive-token-value at C:\\private\\config.json"
                ),
                "图片生成工具配置未就绪。",
            ),
            (
                GroupImageProviderError(
                    "provider exposed sensitive-token-value at /private/provider.json"
                ),
                "图片生成失败，稍后再试。",
            ),
            (
                RuntimeError(
                    "unexpected exposed sensitive-token-value at /private/error.json"
                ),
                "图片生成失败，稍后再试。",
            ),
        )
        for error, expected_message in cases:
            with self.subTest(error=error.__class__.__name__):
                order = []
                facade = RecordingFacade(order, error=error)
                harness, logger = self._make_tool_harness(facade)
                event = FakeEvent(order)

                results = self._collect_tool_results(
                    harness.gcp_step_image_generate(event, prompt="cat", size="")
                )

                self.assertEqual(len(event.sent), 1)
                self.assertEqual(event.sent[0][0][0], "text")
                self.assertFalse(any(item[0][0] == "image" for item in event.sent))
                self.assertEqual(event.extras["tool_status"], "failed")
                self.assertEqual(event.extras["tool_message"], expected_message)
                self.assertEqual(
                    results,
                    [f"群聊图片工具 图片生成失败：{expected_message}"],
                )
                rendered = repr((logger.records, results, event.extras))
                self.assertNotIn("sensitive-token-value", rendered)
                self.assertNotIn("private", rendered.lower())

    def test_edit_failure_branches_send_no_image_and_return_safe_summaries(self):
        cases = (
            (GroupImageUserError("图片提示词不能为空。"), "图片提示词不能为空。"),
            (
                GroupImageConfigError("sensitive-token-value"),
                "图片编辑工具配置未就绪。",
            ),
            (
                GroupImageProviderError("sensitive-token-value"),
                "图片编辑失败，稍后再试。",
            ),
            (RuntimeError("sensitive-token-value"), "图片编辑失败，稍后再试。"),
        )
        for error, expected_message in cases:
            with self.subTest(error=error.__class__.__name__):
                order = []
                facade = RecordingFacade(order, error=error)
                harness, logger = self._make_tool_harness(facade)
                event = FakeEvent(order)

                results = self._collect_tool_results(
                    harness.gcp_step_image_edit(event, prompt="change")
                )

                self.assertEqual(len(event.sent), 1)
                self.assertFalse(any(item[0][0] == "image" for item in event.sent))
                self.assertEqual(event.extras["tool_status"], "failed")
                self.assertEqual(event.extras["tool_message"], expected_message)
                self.assertEqual(
                    results,
                    [f"群聊图片工具 图片编辑失败：{expected_message}"],
                )
                self.assertNotIn("sensitive-token-value", repr(logger.records))

    def test_guard_and_missing_edit_image_fail_before_any_send(self):
        order = []
        facade = RecordingFacade(order)
        harness, _logger = self._make_tool_harness(facade)
        event = FakeEvent(order)
        harness._step_image_guard = lambda current_event: "图片生成工具未启用。"

        generate_results = self._collect_tool_results(
            harness.gcp_step_image_generate(event, prompt="cat", size="")
        )

        self.assertEqual(event.sent, [])
        self.assertEqual(facade.calls, [])
        self.assertEqual(event.extras["tool_status"], "failed")
        self.assertEqual(
            generate_results,
            ["群聊图片工具 图片生成失败：图片生成工具未启用。"],
        )

        order = []
        facade = RecordingFacade(order)
        harness, _logger = self._make_tool_harness(facade, current_image="")
        event = FakeEvent(order)
        edit_results = self._collect_tool_results(
            harness.gcp_step_image_edit(event, prompt="change")
        )

        self.assertEqual(event.sent, [])
        self.assertEqual(facade.calls, [])
        self.assertEqual(event.extras["tool_status"], "failed")
        self.assertEqual(
            edit_results,
            [
                "群聊图片工具 图片编辑失败："
                "未检测到可编辑图片，请把图片和编辑要求放在同一条消息里。"
            ],
        )

    def test_progress_and_image_markers_prevent_duplicate_sends(self):
        order = []
        facade = RecordingFacade(order)
        harness, _logger = self._make_tool_harness(facade)
        event = FakeEvent(order)

        self._collect_tool_results(
            harness.gcp_step_image_generate(event, prompt="first", size="")
        )
        self._collect_tool_results(
            harness.gcp_step_image_generate(event, prompt="second", size="")
        )

        sent_kinds = [payload[0][0] for payload in event.sent]
        self.assertEqual(sent_kinds.count("text"), 1)
        self.assertEqual(sent_kinds.count("image"), 1)
        self.assertEqual(len(facade.calls), 2)

    def test_image_tool_exception_logs_use_fixed_operation_codes(self):
        method_names = (
            "_cleanup_step_image_outputs",
            "_send_step_image_progress",
            "_send_step_image_image_result",
            "_extract_first_current_image_path",
            "gcp_step_image_generate",
            "gcp_step_image_edit",
        )
        source = "\n".join(self._method_source(name) for name in method_names)
        self.assertNotIn("exc_info=True", source)
        self.assertNotIn("{exc}", source)
        for operation_code in (
            "STEP_IMAGE_CLEANUP_FAILED",
            "STEP_IMAGE_PROGRESS_SEND_FAILED",
            "STEP_IMAGE_IMAGE_SEND_FAILED",
            "STEP_IMAGE_EXTRACT_FAILED",
            "STEP_IMAGE_GENERATE_CONFIG_FAILED",
            "STEP_IMAGE_GENERATE_PROVIDER_FAILED",
            "STEP_IMAGE_GENERATE_UNEXPECTED_FAILED",
            "STEP_IMAGE_EDIT_CONFIG_FAILED",
            "STEP_IMAGE_EDIT_PROVIDER_FAILED",
            "STEP_IMAGE_EDIT_UNEXPECTED_FAILED",
        ):
            self.assertIn(operation_code, source)

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
