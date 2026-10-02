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
