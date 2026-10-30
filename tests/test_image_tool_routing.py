import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest


path = Path(__file__).resolve().parents[1] / "utils/image_tool_routing.py"
spec = importlib.util.spec_from_file_location("gcp_image_tool_routing_test", path)
routing = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = routing
spec.loader.exec_module(routing)


def provider(backend):
    kind = {"grok_oauth": "grok_oauth_chat_completion", "codex_oauth": "openai_oauth_chat_completion"}.get(backend, "other")
    return SimpleNamespace(meta=lambda: SimpleNamespace(type=kind))


class ImageToolRoutingTest(unittest.TestCase):
    def test_normal_members_only_see_their_actual_planner_tool(self):
        event = SimpleNamespace(is_admin=lambda: False)
        for backend, tool in routing.IMAGE_TOOLS.items():
            self.assertEqual(routing.visible_image_tools(event, provider(backend)), {tool})
        self.assertEqual(routing.visible_image_tools(event, provider("other")), set())

    def test_default_and_same_backend_pin_actual_instance_without_lookup(self):
        event = SimpleNamespace(is_admin=lambda: False)
        context = SimpleNamespace(get_provider_by_id=lambda _: self.fail("unexpected lookup"))
        for backend in routing.IMAGE_TOOLS:
            caller = provider(backend)
            for requested in (None, backend):
                route = routing.resolve_image_tool_route(event, caller, requested, context, {})
                self.assertIs(route.provider, caller)
                self.assertEqual(route.backend, backend)

    def test_non_admin_cannot_invoke_hidden_opposite_tool(self):
        for admin_value in (False, None, 1, "true"):
            event = SimpleNamespace(is_admin=lambda: admin_value)
            context = SimpleNamespace(get_provider_by_id=lambda _: self.fail("unauthorized lookup"))
            with self.subTest(admin=admin_value), self.assertRaises(routing.ImageToolRoutingError):
                routing.resolve_image_tool_route(event, provider("grok_oauth"), "codex_oauth", context, {})

    def test_admin_can_select_either_configured_backend(self):
        event = SimpleNamespace(is_admin=lambda: True)
        for actual, target, key in (("grok_oauth", "codex_oauth", "codex_oauth_image_provider_id"),
                                    ("codex_oauth", "grok_oauth", "grok_image_provider_id")):
            chosen = provider(target)
            looked_up = []
            context = SimpleNamespace(get_provider_by_id=lambda name: (looked_up.append(name), chosen)[1])
            route = routing.resolve_image_tool_route(event, provider(actual), target, context, {key: "chosen/provider"})
            self.assertIs(route.provider, chosen)
            self.assertEqual(looked_up, ["chosen/provider"])
            self.assertEqual(routing.visible_image_tools(event, provider(actual)), routing.PUBLIC_IMAGE_TOOLS)

    def test_group_default_overrides_planner_for_members_and_survives_reload(self):
        event = SimpleNamespace(is_admin=lambda: False, unified_msg_origin="bot:GroupMessage:42")
        chosen = provider("codex_oauth")
        context = SimpleNamespace(get_provider_by_id=lambda _: chosen)
        config = {"image_group_backends": '{"bot:GroupMessage:42":"codex_oauth"}'}
        self.assertEqual(routing.visible_image_tools(event, provider("grok_oauth"), config), {"gcp_gpt_image"})
        route = routing.resolve_image_tool_route(event, provider("grok_oauth"), None, context, config)
        self.assertIs(route.provider, chosen)
        with self.assertRaises(routing.ImageToolRoutingError):
            routing.resolve_image_tool_route(event, provider("grok_oauth"), "grok_oauth", context, config)
        event.is_admin = lambda: True
        caller = provider("grok_oauth")
        self.assertIs(routing.resolve_image_tool_route(event, caller, "grok_oauth", context, config).provider, caller)
        event.is_admin = lambda: False
        event.unified_msg_origin = "other:GroupMessage:42"
        self.assertEqual(routing.visible_image_tools(event, caller, config), {"gcp_grok_image"})

    def test_scoped_admin_explicit_backend_filters_and_blocks_wrong_tool(self):
        import json
        event = SimpleNamespace(is_admin=lambda: True, unified_msg_origin="default:GroupMessage:905956314",
                                get_message_str=lambda: "机器人用gpt画一张死库水tn")
        config = {"image_subject_contexts": json.dumps({event.unified_msg_origin: {"tn": "角色定义"}})}
        caller = provider("grok_oauth")
        self.assertEqual(routing.visible_image_tools(event, caller, config), {"gcp_gpt_image"})
        ctx = SimpleNamespace(get_provider_by_id=lambda _: self.fail("wrong backend must not reach provider"))
        with self.assertRaises(routing.ImageToolRoutingError):
            routing.resolve_image_tool_route(event, caller, "grok_oauth", ctx, config)
        chosen = provider("codex_oauth")
        ctx.get_provider_by_id = lambda _: chosen
        self.assertIs(routing.resolve_image_tool_route(event, caller, "codex_oauth", ctx, config).provider, chosen)
        self.assertIs(routing.resolve_image_tool_route(event, caller, None, ctx, config).provider, chosen)
        event.is_admin = lambda: False
        self.assertEqual(routing.visible_image_tools(event, caller, config), {"gcp_grok_image"})
        event.is_admin = lambda: True
        event.unified_msg_origin = "default:GroupMessage:764968756"
        self.assertEqual(routing.visible_image_tools(event, caller, config), routing.PUBLIC_IMAGE_TOOLS)

    def test_explicit_backend_scope_and_unambiguous_wording(self):
        import json
        event = SimpleNamespace(is_admin=lambda: True, unified_msg_origin="default:GroupMessage:905956314")
        config = {"image_subject_contexts": json.dumps({event.unified_msg_origin: {"tn": "角色定义"}})}
        for text, expected in [("机器人用gpt画一张tn", "codex_oauth"), ("请用 GPT 将角色换成 TN", "codex_oauth"),
                               ("[引用消息][At:123|你]用Codex改图为tn", "codex_oauth"),
                               ("机器人帮我用grok画tn", "grok_oauth"),
                               ("别用gpt画tn", None), ("他说用gpt画tn", None),
                               ("用gpt或grok画tn", None),
                               ("用gpt画tn，不，改用grok画", None),
                               ("用gpt不要画tn，用grok画tn", None),
                               ("用gpt画tn或者用grok画tn", None),
                               ("用GPT画tn，禁止使用Grok", "codex_oauth"), ("用gpt画猫", None),
                               ("用gpt画tnabc", None), ("tn用的gpt是什么", None)]:
            with self.subTest(text=text):
                event.get_message_str = lambda: text
                self.assertEqual(routing.explicit_image_backend(event, config), expected)
        event.get_message_str = lambda: "用gpt画tn"
        event.unified_msg_origin = "qq2:GroupMessage:905956314"
        self.assertIsNone(routing.explicit_image_backend(event, config))
        event.unified_msg_origin = "default:GroupMessage:905956314"
        event.is_admin = lambda: False
        self.assertIsNone(routing.explicit_image_backend(event, config))
        with self.assertRaises(routing.ImageToolRoutingError):
            routing.resolve_image_tool_route(event, provider("grok_oauth"), "codex_oauth",
                SimpleNamespace(get_provider_by_id=lambda _: self.fail("unauthorized lookup")), config)

    def test_permission_revocation_and_wrong_provider_fail_closed(self):
        event = SimpleNamespace(is_admin=lambda: True)
        context = SimpleNamespace(get_provider_by_id=lambda _: provider("other"))
        with self.assertRaises(routing.ImageToolRoutingError):
            routing.resolve_image_tool_route(event, provider("grok_oauth"), "codex_oauth", context, {})
        event.is_admin = lambda: False
        context.get_provider_by_id = lambda _: self.fail("revoked permission reached lookup")
        with self.assertRaises(routing.ImageToolRoutingError):
            routing.resolve_image_tool_route(event, provider("grok_oauth"), "codex_oauth", context, {})


if __name__ == "__main__":
    unittest.main()
