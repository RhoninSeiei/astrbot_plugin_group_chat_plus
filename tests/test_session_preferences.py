import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_session_preferences():
    module_path = REPO_ROOT / "utils" / "session_preferences.py"
    spec = importlib.util.spec_from_file_location(
        "session_preferences_test_module",
        module_path,
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _PersonaManager:
    def __init__(self):
        self.resolve_calls = []
        self.default_calls = []

    async def resolve_selected_persona(
        self,
        *,
        umo,
        conversation_persona_id,
        platform_name,
        provider_settings,
    ):
        self.resolve_calls.append(
            {
                "umo": umo,
                "conversation_persona_id": conversation_persona_id,
                "platform_name": platform_name,
                "provider_settings": provider_settings,
            }
        )
        return (
            "session-persona",
            {
                "name": "session-persona",
                "prompt": "session persona prompt",
                "_begin_dialogs_processed": [],
            },
            "session-persona",
            False,
        )

    async def get_default_persona_v3(self, umo):
        self.default_calls.append(umo)
        return {
            "name": "config-default",
            "prompt": "config default prompt",
            "_begin_dialogs_processed": [],
        }


class _DefaultOnlyPersonaManager:
    def __init__(self):
        self.default_calls = []

    async def get_default_persona_v3(self, umo):
        self.default_calls.append(umo)
        return {
            "name": "config-default",
            "prompt": "config default prompt",
            "_begin_dialogs_processed": [],
        }


class _Context:
    def __init__(self, persona_manager=None):
        self.persona_manager = persona_manager or _PersonaManager()
        self.config_calls = []
        self.provider_calls = []
        self.provider = object()

    def get_config(self, umo=None):
        self.config_calls.append(umo)
        return {"provider_settings": {"default_personality": "config-default"}}

    def get_using_provider(self, umo=None):
        self.provider_calls.append(umo)
        return self.provider


class SessionPreferencesTest(unittest.TestCase):
    def setUp(self):
        self.module = _load_session_preferences()
        self.event = SimpleNamespace(
            unified_msg_origin="aiocqhttp:GroupMessage:10001",
            get_platform_name=lambda: "aiocqhttp",
        )

    def test_resolve_session_persona_prefers_session_rule_persona(self):
        context = _Context()

        persona = asyncio.run(
            self.module.resolve_session_persona(context, event=self.event)
        )

        self.assertEqual(persona["name"], "session-persona")
        self.assertEqual(
            context.persona_manager.resolve_calls,
            [
                {
                    "umo": "aiocqhttp:GroupMessage:10001",
                    "conversation_persona_id": None,
                    "platform_name": "aiocqhttp",
                    "provider_settings": {"default_personality": "config-default"},
                }
            ],
        )
        self.assertEqual(context.persona_manager.default_calls, [])

    def test_resolve_session_persona_falls_back_to_config_default(self):
        manager = _DefaultOnlyPersonaManager()
        context = _Context(persona_manager=manager)

        persona = asyncio.run(
            self.module.resolve_session_persona(context, event=self.event)
        )

        self.assertEqual(persona["name"], "config-default")
        self.assertEqual(manager.default_calls, ["aiocqhttp:GroupMessage:10001"])

    def test_get_session_provider_uses_current_umo(self):
        context = _Context()

        provider = self.module.get_session_provider(context, event=self.event)

        self.assertIs(provider, context.provider)
        self.assertEqual(context.provider_calls, ["aiocqhttp:GroupMessage:10001"])

    def test_group_chat_call_sites_use_session_helpers(self):
        reply_handler = (REPO_ROOT / "utils" / "reply_handler.py").read_text(
            encoding="utf-8"
        )
        decision_ai = (REPO_ROOT / "utils" / "decision_ai.py").read_text(
            encoding="utf-8"
        )
        proactive = (REPO_ROOT / "utils" / "proactive_chat_manager.py").read_text(
            encoding="utf-8"
        )

        self.assertIn("resolve_session_persona", reply_handler)
        self.assertIn("resolve_session_persona", decision_ai)
        self.assertIn("get_session_provider(context, event=event)", decision_ai)
        self.assertIn("resolve_session_persona", proactive)
        self.assertIn(
            "get_session_provider(context, umo=unified_msg_origin)",
            proactive,
        )


if __name__ == "__main__":
    unittest.main()
