import ast
import asyncio
import copy
import inspect
from pathlib import Path
from types import SimpleNamespace
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]


class RecordingLogger:
    def warning(self, *args, **kwargs):
        pass


class ProviderSelectionCompatibilityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source = (REPO_ROOT / "utils" / "reply_handler.py").read_text(
            encoding="utf-8"
        )
        tree = ast.parse(source)
        reply_handler = next(
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "ReplyHandler"
        )
        cls.method = next(
            node
            for node in reply_handler.body
            if isinstance(node, ast.AsyncFunctionDef)
            and node.name == "_request_with_astrbot_fallback"
        )

    def _build_harness(self, selector):
        method = copy.deepcopy(self.method)
        method.decorator_list = []
        module = ast.Module(
            body=[
                ast.ImportFrom(
                    module="__future__",
                    names=[ast.alias(name="annotations")],
                    level=0,
                ),
                method,
            ],
            type_ignores=[],
        )
        ast.fix_missing_locations(module)

        class FakeReplyHandler:
            @staticmethod
            def _llm_response_has_sendable_content(response):
                return bool(getattr(response, "completion_text", ""))

        namespace = {
            "_select_provider": selector,
            "_get_fallback_chat_providers": lambda provider, context, settings: [],
            "ReplyHandler": FakeReplyHandler,
            "inspect": inspect,
            "logger": RecordingLogger(),
        }
        exec(compile(module, "reply_handler.py", "exec"), namespace)
        return namespace["_request_with_astrbot_fallback"]

    @staticmethod
    def _event_and_request():
        event = SimpleNamespace(
            unified_msg_origin="aiocqhttp:GroupMessage:10001"
        )
        request = SimpleNamespace(
            prompt="reply gate",
            session_id="session",
            image_urls=[],
            contexts=[],
            system_prompt="",
            tool_calls_result=None,
            model=None,
            extra_user_content_parts=None,
        )
        return event, request

    @staticmethod
    def _provider():
        class Provider:
            provider_config = {"id": "provider-a"}

            async def text_chat(self, **kwargs):
                return SimpleNamespace(
                    role="assistant",
                    completion_text="__GCP_REPLY__",
                )

        return Provider()

    def _run_case(self, selector):
        method = self._build_harness(selector)
        event, request = self._event_and_request()
        context = SimpleNamespace(
            get_config=lambda origin: {"provider_settings": {}}
        )
        return asyncio.run(method(event, context, request))

    def test_awaits_astrbot_427_async_provider_selector(self):
        provider = self._provider()

        async def selector(event, context):
            return provider

        response, primary_id, final_id, fallback_count = self._run_case(selector)

        self.assertEqual(response.completion_text, "__GCP_REPLY__")
        self.assertEqual(primary_id, "provider-a")
        self.assertEqual(final_id, "provider-a")
        self.assertEqual(fallback_count, 0)

    def test_keeps_legacy_sync_provider_selector_compatible(self):
        provider = self._provider()

        def selector(event, context):
            return provider

        response, primary_id, final_id, fallback_count = self._run_case(selector)

        self.assertEqual(response.completion_text, "__GCP_REPLY__")
        self.assertEqual(primary_id, "provider-a")
        self.assertEqual(final_id, "provider-a")
        self.assertEqual(fallback_count, 0)


if __name__ == "__main__":
    unittest.main()
