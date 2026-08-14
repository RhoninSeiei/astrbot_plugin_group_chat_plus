import ast
import asyncio
import copy
from pathlib import Path
import sys
from types import SimpleNamespace
import types
from typing import Optional
import unittest

from tests.test_image_handler_quoted_images import (
    FakeImage,
    FakePlain,
    FakeReply,
    ImageHandler as ProductionImageHandler,
)


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


class FakeAt:
    def __init__(self, qq):
        self.qq = qq


class FakeEvent:
    def __init__(self, raw_text="引用探针", chain=None):
        self.raw_text = raw_text
        self.extras = {}
        self.message_str = raw_text
        self.message_obj = SimpleNamespace(
            message=list(
                chain
                if chain is not None
                else [
                    FakeReply("r1", [FakeImage("quoted-a")], message_str=""),
                    FakePlain(raw_text),
                ]
            ),
            timestamp=123,
        )

    def get_sender_id(self):
        return "sender-1"

    def get_sender_name(self):
        return "sender"

    def get_self_id(self):
        return "bot-1"

    def get_messages(self):
        return self.message_obj.message

    def get_extra(self, key, default=None):
        return self.extras.get(key, default)

    def set_extra(self, key, value):
        self.extras[key] = value


class FakeImageHandler:
    collect_error = None

    @staticmethod
    async def collect_message_images(event, message_chain, max_images):
        if FakeImageHandler.collect_error is not None:
            raise FakeImageHandler.collect_error
        return await ProductionImageHandler.collect_message_images(
            event,
            message_chain,
            max_images,
        )

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
        return await ProductionImageHandler._convert_images_to_text(
            message_chain,
            context,
            provider_id,
            prompt,
            resolved_images,
            timeout,
            image_description_cache,
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
    def __init__(self):
        self.records = []

    def info(self, *_args, **_kwargs):
        self.records.append(("info", _args, _kwargs))

    def warning(self, *_args, **_kwargs):
        self.records.append(("warning", _args, _kwargs))


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
        cls.logger = FakeLogger()
        namespace = {
            "AstrMessageEvent": object,
            "Optional": Optional,
            "At": FakeAt,
            "Plain": FakePlain,
            "asyncio": asyncio,
            "time": SimpleNamespace(time=lambda: 1000.0),
            "MessageCleaner": FakeMessageCleaner,
            "PlatformLTMHelper": FakePlatformLTMHelper,
            "ImageHandler": FakeImageHandler,
            "EmojiDetector": SimpleNamespace(),
            "PLUGIN_REFERENCE_IMAGE_URLS": PLUGIN_REFERENCE_IMAGE_URLS,
            "logger": cls.logger,
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
            _should_merge_at_for_user=lambda _sender_id: True,
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

    def _run(
        self,
        provider_id,
        raw_text="引用探针",
        *,
        chain=None,
        is_at_message=False,
    ):
        harness, cache_manager = self._make_harness(provider_id)
        event = FakeEvent(raw_text, chain=chain)
        intercepted = asyncio.run(
            type(self).intercept(
                harness,
                event,
                "chat-1",
                is_at_message,
                False,
                None,
                "aiocqhttp",
            )
        )
        self.assertTrue(intercepted)
        self.assertEqual(len(cache_manager.messages), 1)
        return cache_manager.messages[0][1], event, harness

    def test_merged_at_with_only_embedded_reply_image_uses_real_collector(self):
        for provider_id in ("", "vision"):
            with self.subTest(provider_id=provider_id or "multimodal"):
                cached_message, event, harness = self._run(
                    provider_id,
                    raw_text="",
                    chain=[
                        FakeAt("bot-1"),
                        FakeReply(
                            "r1",
                            [FakeImage("quoted-a")],
                            message_str="",
                        ),
                        FakePlain(""),
                    ],
                    is_at_message=True,
                )

                self.assertEqual(
                    cached_message["reference_image_urls"],
                    ["quoted-a"],
                )
                self.assertEqual(event.get_messages()[0].id, "r1")
                if provider_id:
                    self.assertEqual(cached_message["image_urls"], [])
                    self.assertIn("[引用图片内容: desc:quoted-a]", cached_message["content"])
                    self.assertEqual(len(harness.context.provider.calls), 1)
                else:
                    self.assertEqual(cached_message["image_urls"], ["quoted-a"])

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

    def test_multimodal_wait_window_caches_reference_without_text(self):
        cached_message, event, _harness = self._run("", raw_text="")

        self.assertEqual(cached_message["content"], "")
        self.assertEqual(cached_message["image_urls"], ["quoted-a"])
        self.assertEqual(cached_message["reference_image_urls"], ["quoted-a"])
        self.assertEqual(
            event.get_extra(PLUGIN_REFERENCE_IMAGE_URLS),
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

    def test_regular_follower_cache_reference_reaches_smart_merge_result(self):
        source = (REPO_ROOT / "main.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        chat_plus = next(
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "ChatPlus"
        )
        process_method = next(
            node
            for node in chat_plus.body
            if isinstance(node, ast.AsyncFunctionDef)
            and node.name == "_process_message_content"
        )
        cached_assignment = next(
            copy.deepcopy(node)
            for node in ast.walk(process_method)
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "cached_message"
                for target in node.targets
            )
        )
        builder = ast.FunctionDef(
            name="_build_regular_cache",
            args=ast.arguments(
                posonlyargs=[],
                args=[],
                vararg=None,
                kwonlyargs=[],
                kw_defaults=[],
                kwarg=None,
                defaults=[],
            ),
            body=[
                cached_assignment,
                ast.Return(value=ast.Name(id="cached_message", ctx=ast.Load())),
            ],
            decorator_list=[],
        )
        merge_method = next(
            copy.deepcopy(node)
            for node in chat_plus.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "_merge_reference_image_urls"
        )
        merge_method.decorator_list = []
        module = ast.Module(body=[builder, merge_method], type_ignores=[])
        ast.fix_missing_locations(module)

        event = FakeEvent("follower")
        event.set_extra(PLUGIN_REFERENCE_IMAGE_URLS, ["follower-a", "shared"])
        namespace = {
            "time": SimpleNamespace(time=lambda: 1000.0),
            "event": event,
            "processed_message": "follower",
            "current_message_id": "follower-id",
            "mention_info": None,
            "is_at_message": False,
            "has_trigger_keyword": False,
            "poke_info": None,
            "image_urls": [],
            "is_empty_at": False,
            "is_at_all_message": False,
            "persistent_poke_event_text": "",
            "PLUGIN_REFERENCE_IMAGE_URLS": PLUGIN_REFERENCE_IMAGE_URLS,
        }
        exec(compile(module, "main.py", "exec"), namespace)

        follower_cache = namespace["_build_regular_cache"]()
        self.assertIn("reference_image_urls", follower_cache)
        merged = namespace["_merge_reference_image_urls"](
            ["current-a", "shared"],
            follower_cache["reference_image_urls"],
        )

        self.assertEqual(
            follower_cache["reference_image_urls"],
            ["follower-a", "shared"],
        )
        self.assertEqual(
            merged,
            ["current-a", "shared", "follower-a"],
        )

    def test_wait_window_and_cache_helper_logs_hide_image_secrets(self):
        secret = (
            "exception-body https://private.example/image.png "
            "data:image/png;base64,SECRETPAYLOAD"
        )
        self.logger.records.clear()
        harness, _cache_manager = self._make_harness("")
        event = FakeEvent("message")
        FakeImageHandler.collect_error = RuntimeError(secret)
        try:
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
        finally:
            FakeImageHandler.collect_error = None

        self.assertFalse(intercepted)
        rendered = repr(self.logger.records)
        self.assertNotIn("private.example", rendered)
        self.assertNotIn("SECRETPAYLOAD", rendered)
        self.assertNotIn("exception-body", rendered)
        self.assertNotIn("exc_info", rendered)

    def test_cache_helper_logs_hide_image_secrets(self):
        secret = (
            "exception-body https://private.example/image.png "
            "data:image/png;base64,SECRETPAYLOAD"
        )
        source = (REPO_ROOT / "main.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        chat_plus = next(
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "ChatPlus"
        )
        methods = [
            copy.deepcopy(node)
            for node in chat_plus.body
            if isinstance(node, ast.AsyncFunctionDef)
            and node.name
            in {
                "_save_platform_descriptions_to_cache",
                "_try_cache_fallback_for_images",
            }
        ]
        module = ast.Module(body=methods, type_ignores=[])
        ast.fix_missing_locations(module)
        helper_logger = FakeLogger()
        namespace = {
            "Optional": Optional,
            "logger": helper_logger,
        }
        exec(compile(module, "main.py", "exec"), namespace)

        class CacheImage:
            async def convert_to_file_path(self):
                raise RuntimeError(secret)

        components_module = types.ModuleType("astrbot.api.message_components")
        components_module.Image = CacheImage
        components_module.Plain = type("Plain", (), {})
        module_names = (
            "astrbot",
            "astrbot.api",
            "astrbot.api.message_components",
        )
        previous_modules = {name: sys.modules.get(name) for name in module_names}
        sys.modules["astrbot"] = types.ModuleType("astrbot")
        sys.modules["astrbot.api"] = types.ModuleType("astrbot.api")
        sys.modules["astrbot.api.message_components"] = components_module

        cache = SimpleNamespace(
            enabled=True,
            lookup=lambda _path: None,
            save=lambda _path, _description: None,
        )
        plugin = SimpleNamespace(image_description_cache=cache)
        save_event = SimpleNamespace(
            message_obj=SimpleNamespace(message=[CacheImage()])
        )

        class RaisingEvent:
            @property
            def message_obj(self):
                raise RuntimeError(secret)

        try:
            asyncio.run(
                namespace["_save_platform_descriptions_to_cache"](
                    plugin,
                    save_event,
                    "[图片内容: harmless]",
                )
            )
            asyncio.run(
                namespace["_try_cache_fallback_for_images"](
                    plugin,
                    RaisingEvent(),
                )
            )
        finally:
            for name, previous in previous_modules.items():
                if previous is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = previous

        rendered = repr(helper_logger.records)
        self.assertNotIn("private.example", rendered)
        self.assertNotIn("SECRETPAYLOAD", rendered)
        self.assertNotIn("exception-body", rendered)
        self.assertNotIn("exc_info", rendered)


if __name__ == "__main__":
    unittest.main()
