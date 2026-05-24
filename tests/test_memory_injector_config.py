import importlib.util
import pathlib
import sys
import types
import unittest


REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _load_memory_injector():
    astrbot_module = types.ModuleType("astrbot")
    astrbot_api_module = types.ModuleType("astrbot.api")
    astrbot_api_all_module = types.ModuleType("astrbot.api.all")
    logger = types.SimpleNamespace(
        info=lambda *_args, **_kwargs: None,
        warning=lambda *_args, **_kwargs: None,
        error=lambda *_args, **_kwargs: None,
        debug=lambda *_args, **_kwargs: None,
    )
    astrbot_api_all_module.logger = logger
    astrbot_api_all_module.Context = object
    astrbot_api_all_module.AstrMessageEvent = object
    sys.modules.setdefault("astrbot", astrbot_module)
    sys.modules.setdefault("astrbot.api", astrbot_api_module)
    sys.modules.setdefault("astrbot.api.all", astrbot_api_all_module)

    module_path = REPO_ROOT / "utils" / "memory_injector.py"
    spec = importlib.util.spec_from_file_location("memory_injector_test", module_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _Context:
    def __init__(self, star=None, tool_manager=None, persona_manager=None):
        self._star = star
        self._tool_manager = tool_manager
        self.persona_manager = persona_manager

    def get_registered_star(self, _name):
        return self._star

    def get_llm_tool_manager(self):
        return self._tool_manager


class MemoryInjectorConfigTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.schema = (REPO_ROOT / "_conf_schema.json").read_text(encoding="utf-8")
        self.main_source = (REPO_ROOT / "main.py").read_text(encoding="utf-8")
        self.proactive_source = (
            REPO_ROOT / "utils" / "proactive_chat_manager.py"
        ).read_text(encoding="utf-8")

    def test_schema_and_call_sites_expose_auto_and_persona_compat(self):
        for key in (
            "memory_plugin_mode",
            "livingmemory_version",
            "livingmemory_persona_compat_mode",
        ):
            self.assertIn(f'"{key}"', self.schema)
        for fragment in (
            '"auto"',
            "MemoryInjector.resolve_mode",
            "persona_compat_mode=livingmemory_persona_compat_mode",
            "self.livingmemory_persona_compat_mode",
        ):
            self.assertIn(fragment, self.main_source)
        self.assertIn("_livingmemory_persona_compat_mode", self.proactive_source)
        self.assertIn("MemoryInjector.resolve_mode", self.proactive_source)

    async def test_auto_mode_prefers_livingmemory_v2(self):
        module = _load_memory_injector()
        memory_engine = object()
        initializer = types.SimpleNamespace(
            is_initialized=True,
            memory_engine=memory_engine,
        )
        plugin = types.SimpleNamespace(initializer=initializer)
        star = types.SimpleNamespace(activated=True, star_cls=plugin)
        mode, version = module.MemoryInjector.resolve_mode(
            _Context(star=star),
            "auto",
            "auto",
        )

        self.assertEqual(mode, "livingmemory")
        self.assertEqual(version, "v2")

    async def test_persona_resolver_uses_current_conversation_persona(self):
        module = _load_memory_injector()

        class PersonaManager:
            async def resolve_selected_persona(self, **kwargs):
                self.kwargs = kwargs
                return "persona-current", {"name": "角色"}, None, False

        class ConversationManager:
            async def get_curr_conversation_id(self, _session_id):
                return "conv-1"

            async def get_conversation(self, _session_id, _conversation_id):
                return types.SimpleNamespace(persona_id="persona-from-conv")

        persona_mgr = PersonaManager()
        context = _Context(persona_manager=persona_mgr)
        context.conversation_manager = ConversationManager()
        event = types.SimpleNamespace(get_platform_name=lambda: "aiocqhttp")

        persona_id = await module.MemoryInjector._resolve_livingmemory_persona_id(
            context,
            "aiocqhttp:GroupMessage:123",
            event=event,
            compat_mode="auto",
        )

        self.assertEqual(persona_id, "persona-current")
        self.assertEqual(
            persona_mgr.kwargs["conversation_persona_id"],
            "persona-from-conv",
        )


if __name__ == "__main__":
    unittest.main()
