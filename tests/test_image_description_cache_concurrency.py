import asyncio
import importlib.util
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock

from tests.test_image_handler_quoted_images import (
    FakeContext,
    FakePlain,
    FakeProvider,
    FakeReply,
    ImageHandler,
    ResolvedMessageImage,
)


REPO_ROOT = Path(__file__).resolve().parents[1]


class RecordingLogger:
    def __init__(self):
        self.records = []

    def info(self, *args, **kwargs):
        self.records.append(("info", args, kwargs))

    def warning(self, *args, **kwargs):
        self.records.append(("warning", args, kwargs))

    def error(self, *args, **kwargs):
        self.records.append(("error", args, kwargs))


def _load_cache_module():
    logger = RecordingLogger()
    astrbot_api = sys.modules.setdefault("astrbot.api", types.ModuleType("astrbot.api"))
    astrbot_api.logger = logger
    spec = importlib.util.spec_from_file_location(
        "quoted_image_cache_concurrency_test",
        REPO_ROOT / "utils" / "image_description_cache.py",
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    module.logger = logger
    return module, logger


cache_module, cache_logger = _load_cache_module()
ImageDescriptionCache = cache_module.ImageDescriptionCache


class ImageDescriptionCacheConcurrencyTest(unittest.TestCase):
    def setUp(self):
        cache_logger.records.clear()
        cache_module.DEBUG_MODE = False

    def test_concurrent_image_conversion_uses_one_provider_call(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as temp_dir:
                cache = ImageDescriptionCache(temp_dir, enabled=True)

                class ConcurrentProvider(FakeProvider):
                    async def text_chat(self, **kwargs):
                        self.calls.append(kwargs)
                        await asyncio.sleep(0.01)
                        image = kwargs["image_urls"][0]
                        return types.SimpleNamespace(
                            completion_text=f"desc:{image}"
                        )

                provider = ConcurrentProvider()
                records = [
                    ResolvedMessageImage(" quoted-a ", "quoted_embedded", 0)
                ]
                chain = [FakeReply("r1", message_str=""), FakePlain("问题正文")]

                first, second = await asyncio.gather(
                    ImageHandler._convert_images_to_text(
                        chain, FakeContext(provider), "vision", "describe", records, 60, cache
                    ),
                    ImageHandler._convert_images_to_text(
                        chain, FakeContext(provider), "vision", "describe", records, 60, cache
                    ),
                )

                return provider.calls, first, second, cache.entry_count

        calls, first, second, entry_count = asyncio.run(scenario())

        self.assertEqual(len(calls), 1)
        self.assertIn("desc: quoted-a ", first)
        self.assertEqual(second, first)
        self.assertEqual(entry_count, 1)

    def test_failed_single_flight_allows_waiter_retry(self):
        self.assertTrue(
            hasattr(ImageDescriptionCache, "get_or_create"),
            "ImageDescriptionCache requires async single-flight",
        )

        async def scenario():
            with tempfile.TemporaryDirectory() as temp_dir:
                cache = ImageDescriptionCache(temp_dir, enabled=True)
                calls = 0

                async def create_description():
                    nonlocal calls
                    calls += 1
                    await asyncio.sleep(0)
                    if calls == 1:
                        raise RuntimeError("provider failed")
                    return "retry-description"

                results = await asyncio.gather(
                    cache.get_or_create("quoted-a", create_description),
                    cache.get_or_create("quoted-a", create_description),
                    return_exceptions=True,
                )
                return calls, results, cache.lookup("quoted-a")

        calls, results, cached = asyncio.run(scenario())

        self.assertEqual(calls, 2)
        self.assertEqual(sum(isinstance(item, RuntimeError) for item in results), 1)
        self.assertIn("retry-description", results)
        self.assertEqual(cached, "retry-description")

    def test_cache_debug_and_exception_logs_hide_payloads(self):
        secret_url = "https://private.example/image.png?token=SECRET-TOKEN"
        secret_description = "description data:image/png;base64,SECRETBASE64"
        secret_error = "sensitive-open-error"

        with tempfile.TemporaryDirectory() as temp_dir:
            cache = ImageDescriptionCache(temp_dir, enabled=True)
            cache_module.DEBUG_MODE = True
            cache.save(secret_url, secret_description)
            self.assertEqual(cache.lookup(secret_url), secret_description)
            with mock.patch("builtins.open", side_effect=RuntimeError(secret_error)):
                self.assertIsNone(cache.lookup(secret_url))

        rendered = repr(cache_logger.records)
        self.assertNotIn("private.example", rendered)
        self.assertNotIn("SECRET-TOKEN", rendered)
        self.assertNotIn("SECRETBASE64", rendered)
        self.assertNotIn(secret_error, rendered)


if __name__ == "__main__":
    unittest.main()
