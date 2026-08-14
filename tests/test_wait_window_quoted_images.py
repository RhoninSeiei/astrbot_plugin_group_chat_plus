import ast
import asyncio
import copy
from pathlib import Path
from types import SimpleNamespace
from typing import Optional
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_REFERENCE_IMAGE_URLS = "_group_chat_plus_reference_image_urls"


class AsyncLock:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class RecordingCacheManager:
    def __init__(self):
        self.messages = []

    def add_to_cache(self, chat_id, message, source):
        self.messages.append((chat_id, copy.deepcopy(message), source))


class FakeProvider:
    def __init__(self):
        self.calls = []

    async def text_chat(self, **kwargs):
        self.calls.append(kwargs)
        image = kwargs["image_urls"][0]
        return SimpleNamespace(completion_text=f"desc:{image}")


class FakeContext:
    def __init__(self, provider):
        self.provider = provider

    def get_provider_by_id(self, provider_id):
        return self.provider if provider_id == "vision" else None


class FakeEvent:
    def __init__(self, raw_text="引用探针"):
        self.raw_text = raw_text
        self.extras = {}
        self.message_str = raw_text
        self.message_obj = SimpleNamespace(message=[], timestamp=123)

    def get_sender_id(self):
        return "sender-1"

    def get_sender_name(self):
        return "sender"

    def get_messages(self):
        return self.message_obj.message

    def get_extra(self, key, default=None):
        return self.extras.get(key, default)

    def set_extra(self, key, value):
        self.extras[key] = value


class FakeImageHandler:
    @staticmethod
    async def collect_message_images(event, message_chain, max_images):
        return [SimpleNamespace(url="quoted-a", source="quoted_embedded", component_index=0)]

    @staticmethod
    async def _convert_images_to_text(
        message_chain,
        context,
        provider_id,
        prompt,
        resolved_images,
        timeout,
        image_description_cache,
    ):
        provider = context.get_provider_by_id(provider_id)
        response = await provider.text_chat(
            prompt=prompt,
            contexts=[],
            image_urls=[resolved_images[0].url],
            func_tool=None,
            system_prompt="",
        )
        return (
            "[引用消息]"
            f"[引用图片内容: {response.completion_text}]"
            "问题正文"
        )


class FakePlatformLTMHelper:
    @staticmethod
    def has_image_in_message(_event):
        return False

    @staticmethod
    def is_pure_image_message(_event):
        return False

    @staticmethod
    async def extract_image_caption_from_platform(*_args, **_kwargs):
        return False, None


class FakeMessageCleaner:
    @staticmethod
    def extract_raw_message_from_event(event):
        return event.raw_text

    @staticmethod
    def process_cached_message_images(text):
        return True, text


class FakeLogger:
    def info(self, *_args, **_kwargs):
        pass

    def warning(self, *_args, **_kwargs):
        pass


class WaitWindowQuotedImagesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source = (REPO_ROOT / "main.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        chat_plus = next(
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "ChatPlus"
        )
        method = next(
            copy.deepcopy(node)
            for node in chat_plus.body
            if isinstance(node, ast.AsyncFunctionDef)
            and node.name == "_maybe_intercept_for_wait_window"
        )
        module = ast.Module(body=[method], type_ignores=[])
        ast.fix_missing_locations(module)
        namespace = {
            "AstrMessageEvent": object,
            "Optional": Optional,
            "At": type("At", (), {}),
            "Plain": type("Plain", (), {}),
            "asyncio": asyncio,
            "time": SimpleNamespace(time=lambda: 1000.0),
            "MessageCleaner": FakeMessageCleaner,
            "PlatformLTMHelper": FakePlatformLTMHelper,
            "ImageHandler": FakeImageHandler,
            "EmojiDetector": SimpleNamespace(),
            "PLUGIN_REFERENCE_IMAGE_URLS": PLUGIN_REFERENCE_IMAGE_URLS,
            "logger": FakeLogger(),
        }
        exec(compile(module, "main.py", "exec"), namespace)
        cls.intercept = namespace["_maybe_intercept_for_wait_window"]

    def _make_harness(self, provider_id):
        cache_manager = RecordingCacheManager()
        harness = SimpleNamespace(
            _group_wait_window_lock=AsyncLock(),
            _group_wait_windows={
                ("chat-1", "sender-1"): {
                    "token": "window-token",
                    "extra_count": 0,
                    "deadline": 0,
                }
            },
            group_wait_window_at_mode="merge",
            group_wait_window_poke_mode="merge",
            group_wait_window_keyword_mode="merge",
            probability_filter_cache_delay=0,
            platform_image_caption_max_wait=0,
            platform_image_caption_retry_interval=0,
            platform_image_caption_fast_check_count=0,
            image_to_text_provider_id=provider_id,
            image_to_text_prompt="describe",
            image_to_text_timeout=60,
            image_description_cache=None,
            max_images_per_message=10,
            context=FakeContext(FakeProvider()),
            enable_emoji_filter=False,
            debug_mode=False,
            cache_manager=cache_manager,
            group_wait_window_timeout_ms=1000,
            _group_wait_window_max_extra=3,
            _get_message_id=lambda event: "message-1",
            _save_platform_descriptions_to_cache=lambda *args: None,
            _try_cache_fallback_for_images=lambda *args: None,
        )

        async def no_cache_fallback(*_args, **_kwargs):
            return None

        async def no_save(*_args, **_kwargs):
            return None

        harness._try_cache_fallback_for_images = no_cache_fallback
        harness._save_platform_descriptions_to_cache = no_save
        return harness, cache_manager

    def _run(self, provider_id):
        harness, cache_manager = self._make_harness(provider_id)
        event = FakeEvent()
        intercepted = asyncio.run(
            type(self).intercept(
                harness,
                event,
                "chat-1",
                False,
                False,
                None,
                "aiocqhttp",
            )
        )
        self.assertTrue(intercepted)
        self.assertEqual(len(cache_manager.messages), 1)
        return cache_manager.messages[0][1], event, harness

    def test_multimodal_wait_window_preserves_quoted_image(self):
        cached_message, event, _harness = self._run("")

        self.assertEqual(cached_message["image_urls"], ["quoted-a"])
        self.assertIn("引用探针", cached_message["content"])
        self.assertEqual(
            cached_message["reference_image_urls"],
            ["quoted-a"],
        )
        self.assertEqual(
            event.get_extra(PLUGIN_REFERENCE_IMAGE_URLS),
            ["quoted-a"],
        )

    def test_image_to_text_wait_window_keeps_description_and_original_reference(self):
        cached_message, _event, harness = self._run("vision")

        self.assertEqual(cached_message["image_urls"], [])
        self.assertIn(
            "[引用图片内容: desc:quoted-a]",
            cached_message["content"],
        )
        self.assertEqual(
            cached_message["reference_image_urls"],
            ["quoted-a"],
        )
        self.assertEqual(
            harness.context.provider.calls[0]["image_urls"],
            ["quoted-a"],
        )

    def test_reference_merge_preserves_current_wait_and_smart_first_occurrence(self):
        source = (REPO_ROOT / "main.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        chat_plus = next(
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "ChatPlus"
        )
        matches = [
            copy.deepcopy(node)
            for node in chat_plus.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "_merge_reference_image_urls"
        ]
        self.assertEqual(len(matches), 1)
        matches[0].decorator_list = []
        module = ast.Module(body=matches, type_ignores=[])
        ast.fix_missing_locations(module)
        namespace = {}
        exec(compile(module, "main.py", "exec"), namespace)

        merged = namespace["_merge_reference_image_urls"](
            ["current-a", "shared"],
            ["wait-a", "shared", "current-a"],
            ["smart-a", "wait-a", "smart-b"],
        )

        self.assertEqual(
            merged,
            ["current-a", "shared", "wait-a", "smart-a", "smart-b"],
        )


if __name__ == "__main__":
    unittest.main()
