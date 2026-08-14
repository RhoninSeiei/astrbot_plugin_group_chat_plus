import ast
import asyncio
import copy
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]
TOOL_POLICY_SPEC = importlib.util.spec_from_file_location(
    "task_tool_policy_for_passthrough",
    REPO_ROOT / "utils" / "tool_policy.py",
)
assert TOOL_POLICY_SPEC is not None
assert TOOL_POLICY_SPEC.loader is not None
TOOL_POLICY_MODULE = importlib.util.module_from_spec(TOOL_POLICY_SPEC)
sys.modules[TOOL_POLICY_SPEC.name] = TOOL_POLICY_MODULE
TOOL_POLICY_SPEC.loader.exec_module(TOOL_POLICY_MODULE)
ToolPolicy = TOOL_POLICY_MODULE.ToolPolicy


class RecordingLogger:
    def __init__(self):
        self.records = []

    def info(self, message, *args, **kwargs):
        self.records.append(("info", str(message), args, kwargs))

    def warning(self, message, *args, **kwargs):
        self.records.append(("warning", str(message), args, kwargs))


class FakeToolContainer:
    def __init__(self, names):
        self.tools = [SimpleNamespace(name=name) for name in names]

    def merge(self, other):
        self.tools.extend(other.tools)

    def remove_tool(self, name):
        self.tools = [tool for tool in self.tools if tool.name != name]


class FakeRequestEvent:
    def __init__(self, extras):
        self.extras = dict(extras)
        self.unified_msg_origin = "aiocqhttp:GroupMessage:20002"
        self.session_id = self.unified_msg_origin
        self.message_obj = SimpleNamespace(
            unified_msg_origin=self.unified_msg_origin,
            session_id=self.session_id,
        )

    def get_extra(self, key, default=None):
        return self.extras.get(key, default)

    def set_extra(self, key, value):
        if value is None:
            self.extras.pop(key, None)
        else:
            self.extras[key] = value

    def get_group_id(self):
        return "20002"

    def get_platform_name(self):
        return "aiocqhttp"

    def is_private_chat(self):
        return False


class VirtualRequestEvent:
    def __init__(self, extras):
        self.extras = dict(extras)
        self.unified_msg_origin = "aiocqhttp:GroupMessage:20002"
        self.session_id = self.unified_msg_origin
        self.message_obj = SimpleNamespace(
            unified_msg_origin=self.unified_msg_origin,
            session_id=self.session_id,
        )

    def get_extra(self, key, default=None):
        return self.extras.get(key, default)

    def set_extra(self, key, value):
        if value is None:
            self.extras.pop(key, None)
        else:
            self.extras[key] = value

    def get_group_id(self):
        return "20002"


class RaisingRequestEvent(FakeRequestEvent):
    def get_platform_name(self):
        raise RuntimeError("sensitive-platform-value")

    def is_private_chat(self):
        raise RuntimeError("sensitive-private-value")


class ToolPassthroughIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.main_source = (REPO_ROOT / "main.py").read_text(encoding="utf-8")
        self.reply_source = (REPO_ROOT / "utils" / "reply_handler.py").read_text(
            encoding="utf-8"
        )
        self.decision_source = (REPO_ROOT / "utils" / "decision_ai.py").read_text(
            encoding="utf-8"
        )
        self.main_tree = ast.parse(self.main_source)
        self.chat_plus_node = next(
            node
            for node in self.main_tree.body
            if isinstance(node, ast.ClassDef) and node.name == "ChatPlus"
        )

    def _method_node(self, name):
        matches = [
            node
            for node in self.chat_plus_node.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == name
        ]
        self.assertEqual(len(matches), 1, f"ChatPlus.{name} should be unique")
        return matches[0]

    def _make_on_llm_request_harness(self):
        method_names = [
            "_normalize_step_image_group_id",
            "_extract_step_image_group_id_from_origin",
            "_get_step_image_group_id",
            "_is_step_image_enabled_for_event",
            "_can_expose_step_image_tools",
            "_filter_step_image_tools_for_request",
            "on_llm_request",
        ]
        available_method_names = {
            node.name
            for node in self.chat_plus_node.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        for optional_name in (
            "_safe_step_image_log_context",
            "_log_step_image_tools_filtered",
        ):
            if optional_name in available_method_names:
                method_names.insert(-1, optional_name)

        class ImportStripper(ast.NodeTransformer):
            def visit_Import(self, node):
                return None

            def visit_ImportFrom(self, node):
                return None

        nodes = []
        for name in method_names:
            node = copy.deepcopy(self._method_node(name))
            node.decorator_list = []
            nodes.append(ImportStripper().visit(node))

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

        class FakeGroupImageService:
            @staticmethod
            def is_enabled(config):
                return True

        namespace = {
            "GroupImageService": FakeGroupImageService,
            "STEP_IMAGE_TOOL_NAMES": {
                "gcp_step_image_generate",
                "gcp_step_image_edit",
            },
            "ToolPolicy": ToolPolicy,
            "SystemPromptRewriter": SimpleNamespace(
                rewrite_preserving_plugin_base=lambda current, base: SimpleNamespace(
                    merged_system_prompt=current,
                    strategy="test",
                    confidence=1.0,
                    warnings=[],
                )
            ),
            "PLUGIN_REQUEST_MARKER": "plugin_request_marker",
            "PLUGIN_CUSTOM_CONTEXTS": "plugin_contexts",
            "PLUGIN_CUSTOM_SYSTEM_PROMPT": "plugin_system_prompt",
            "PLUGIN_CUSTOM_PROMPT": "plugin_prompt",
            "PLUGIN_IMAGE_URLS": "plugin_image_urls",
            "PLUGIN_FUNC_TOOL": "plugin_func_tool",
            "PLUGIN_CURRENT_MESSAGE": "plugin_current_message",
            "PLUGIN_VISIBLE_TOOL_NAMES": "plugin_visible_tool_names",
            "PLUGIN_ORIGINAL_PLUGINS_NAME": "plugin_original_plugins_name",
            "PLUGIN_STEP_IMAGE_ACTION": "step_image_action",
            "TOOL_CALL_PROMPT": "tool call prompt",
            "logger": logger,
        }
        exec(compile(module, "main.py", "exec"), namespace)

        class RequestHarness:
            pass

        for name in method_names:
            setattr(RequestHarness, name, namespace[name])

        harness = RequestHarness()
        harness.enable_group_chat = True
        harness.step_image_config = {"image_tool_backend": "codex_oauth"}
        harness.enabled_groups = ["10001"]
        harness.debug_mode = False
        harness._sanitize_llm_request_images = lambda event, req, stage: None
        harness._infer_step_image_action = lambda event: None
        harness._check_compliance_status = lambda: None
        harness._build_step_image_tool_directive = lambda tool_names: ""
        return harness, logger

    @staticmethod
    def _visibility_log_records(logger):
        return [
            record
            for record in logger.records
            if record[1].startswith("GCP_TOOL_VISIBILITY_FILTERED")
        ]

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

    def test_formal_reply_clones_legacy_tool_manager_before_filtering(self):
        self.assertIn("from .tool_policy import ToolPolicy", self.reply_source)
        self.assertIn(
            "plugin_tool_set = ToolPolicy.clone_tool_container(func_tools_mgr)",
            self.reply_source,
        )
        self.assertNotIn(
            "\n                    plugin_tool_set = func_tools_mgr\n",
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

    def test_on_llm_request_filters_step_image_tools_before_marker_return_and_after_merge(self):
        filter_call = "self._filter_step_image_tools_for_request"
        first_filter = self.main_source.index(filter_call)
        second_filter = self.main_source.index(filter_call, first_filter + 1)
        marker_read = self.main_source.index(
            "is_plugin_request = event.get_extra(PLUGIN_REQUEST_MARKER, False)"
        )
        merge_complete = self.main_source.index(
            'req.system_prompt = merged_system_prompt or ""'
        )
        current_tools_read = self.main_source.index(
            "current_tools = _get_compatible_tools(req.func_tool)"
        )

        self.assertLess(first_filter, marker_read)
        self.assertLess(merge_complete, second_filter)
        self.assertLess(second_filter, current_tools_read)

    def test_non_plugin_request_filters_unauthorized_step_image_tools_before_return(self):
        harness, logger = self._make_on_llm_request_harness()
        original = FakeToolContainer(
            [
                "normal_search",
                "gcp_step_image_generate",
                "astrbot_plugin_imgflow_generate_image",
                "gcp_step_image_edit",
            ]
        )
        request = SimpleNamespace(
            func_tool=original,
            system_prompt="platform prompt",
            contexts=[],
            prompt="message",
            image_urls=[],
        )
        event = FakeRequestEvent({"plugin_request_marker": False})

        asyncio.run(harness.on_llm_request(event, request))

        self.assertEqual(
            [tool.name for tool in request.func_tool.tools],
            ["normal_search", "astrbot_plugin_imgflow_generate_image"],
        )
        self.assertEqual(
            [tool.name for tool in original.tools],
            [
                "normal_search",
                "gcp_step_image_generate",
                "astrbot_plugin_imgflow_generate_image",
                "gcp_step_image_edit",
            ],
        )
        self.assertEqual(
            self._visibility_log_records(logger),
            [
                (
                    "info",
                    "GCP_TOOL_VISIBILITY_FILTERED "
                    "stage=%s platform=%s private=%s removed=%s",
                    (
                        "incoming",
                        "aiocqhttp",
                        False,
                        ["gcp_step_image_generate", "gcp_step_image_edit"],
                    ),
                    {},
                )
            ],
        )

    def test_plugin_request_refilters_step_image_tools_after_plugin_merge(self):
        harness, logger = self._make_on_llm_request_harness()
        request = SimpleNamespace(
            func_tool=FakeToolContainer(
                ["normal_search", "gcp_step_image_generate"]
            ),
            system_prompt="platform prompt",
            contexts=[],
            prompt="message",
            image_urls=[],
        )
        plugin_tools = FakeToolContainer(
            ["astrbot_plugin_imgflow_generate_image", "gcp_step_image_edit"]
        )
        event = FakeRequestEvent(
            {
                "plugin_request_marker": True,
                "plugin_contexts": [],
                "plugin_system_prompt": "plugin prompt",
                "plugin_prompt": "message",
                "plugin_image_urls": [],
                "plugin_func_tool": plugin_tools,
                "plugin_visible_tool_names": None,
            }
        )

        asyncio.run(harness.on_llm_request(event, request))

        self.assertEqual(
            [tool.name for tool in request.func_tool.tools],
            ["normal_search", "astrbot_plugin_imgflow_generate_image"],
        )
        self.assertEqual(
            self._visibility_log_records(logger),
            [
                (
                    "info",
                    "GCP_TOOL_VISIBILITY_FILTERED "
                    "stage=%s platform=%s private=%s removed=%s",
                    (
                        "incoming",
                        "aiocqhttp",
                        False,
                        ["gcp_step_image_generate"],
                    ),
                    {},
                ),
                (
                    "info",
                    "GCP_TOOL_VISIBILITY_FILTERED "
                    "stage=%s platform=%s private=%s removed=%s",
                    (
                        "post_merge",
                        "aiocqhttp",
                        False,
                        ["gcp_step_image_edit"],
                    ),
                    {},
                ),
            ],
        )

    def test_plugin_request_skips_post_merge_log_when_no_tool_is_removed(self):
        harness, logger = self._make_on_llm_request_harness()
        request = SimpleNamespace(
            func_tool=FakeToolContainer(
                [
                    "normal_search",
                    "gcp_step_image_generate",
                    "gcp_step_image_edit",
                ]
            ),
            system_prompt="platform prompt",
            contexts=[],
            prompt="message",
            image_urls=[],
        )
        event = FakeRequestEvent(
            {
                "plugin_request_marker": True,
                "plugin_contexts": [],
                "plugin_system_prompt": "plugin prompt",
                "plugin_prompt": "message",
                "plugin_image_urls": [],
                "plugin_func_tool": FakeToolContainer(
                    ["astrbot_plugin_imgflow_generate_image"]
                ),
                "plugin_visible_tool_names": None,
            }
        )

        asyncio.run(harness.on_llm_request(event, request))

        self.assertEqual(
            [tool.name for tool in request.func_tool.tools],
            ["normal_search", "astrbot_plugin_imgflow_generate_image"],
        )
        self.assertEqual(
            self._visibility_log_records(logger),
            [
                (
                    "info",
                    "GCP_TOOL_VISIBILITY_FILTERED "
                    "stage=%s platform=%s private=%s removed=%s",
                    (
                        "incoming",
                        "aiocqhttp",
                        False,
                        ["gcp_step_image_generate", "gcp_step_image_edit"],
                    ),
                    {},
                )
            ],
        )

    def test_virtual_plugin_request_uses_safe_log_values_and_restores_prompt(self):
        harness, logger = self._make_on_llm_request_harness()
        request = SimpleNamespace(
            func_tool=FakeToolContainer(
                ["normal_search", "gcp_step_image_generate"]
            ),
            system_prompt="platform prompt",
            contexts=[],
            prompt="short message",
            image_urls=[],
        )
        event = VirtualRequestEvent(
            {
                "plugin_request_marker": True,
                "plugin_contexts": [],
                "plugin_system_prompt": "plugin system prompt",
                "plugin_prompt": "restored full prompt",
                "plugin_image_urls": [],
                "plugin_func_tool": FakeToolContainer(
                    ["astrbot_plugin_imgflow_generate_image", "gcp_step_image_edit"]
                ),
                "plugin_visible_tool_names": None,
            }
        )

        asyncio.run(harness.on_llm_request(event, request))

        self.assertEqual(request.prompt, "restored full prompt")
        self.assertEqual(
            [tool.name for tool in request.func_tool.tools],
            ["normal_search", "astrbot_plugin_imgflow_generate_image"],
        )
        self.assertEqual(
            self._visibility_log_records(logger),
            [
                (
                    "info",
                    "GCP_TOOL_VISIBILITY_FILTERED "
                    "stage=%s platform=%s private=%s removed=%s",
                    (
                        "incoming",
                        "unknown",
                        "unknown",
                        ["gcp_step_image_generate"],
                    ),
                    {},
                ),
                (
                    "info",
                    "GCP_TOOL_VISIBILITY_FILTERED "
                    "stage=%s platform=%s private=%s removed=%s",
                    (
                        "post_merge",
                        "unknown",
                        "unknown",
                        ["gcp_step_image_edit"],
                    ),
                    {},
                ),
            ],
        )

    def test_visibility_log_uses_safe_values_when_event_methods_raise(self):
        harness, logger = self._make_on_llm_request_harness()
        request = SimpleNamespace(
            func_tool=FakeToolContainer(
                ["normal_search", "gcp_step_image_generate"]
            ),
            system_prompt="platform prompt",
            contexts=[],
            prompt="message",
            image_urls=[],
        )
        event = RaisingRequestEvent({"plugin_request_marker": False})

        asyncio.run(harness.on_llm_request(event, request))

        self.assertEqual(
            self._visibility_log_records(logger),
            [
                (
                    "info",
                    "GCP_TOOL_VISIBILITY_FILTERED "
                    "stage=%s platform=%s private=%s removed=%s",
                    (
                        "incoming",
                        "unknown",
                        "unknown",
                        ["gcp_step_image_generate"],
                    ),
                    {},
                )
            ],
        )

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
