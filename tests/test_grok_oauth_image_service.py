import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "gcp_grok_image_service_test", ROOT / "utils/grok_oauth_image_service.py"
)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


class Provider:
    def __init__(self, path):
        self.path = path
        self.calls = []
        self.capabilities = {
            name: {"implementation": True, "enabled": True, "observed": "unknown"}
            for name in ("image_generation", "image_editing")
        }

    def meta(self):
        return SimpleNamespace(type="grok_oauth_chat_completion", id="grok_oauth/grok-4.6")

    async def generate_image(self, **kwargs):
        self.calls.append(kwargs)
        return [SimpleNamespace(path=self.path, mime_type="image/png", revised_prompt="")]


class GrokImageServiceTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "image.png"
        self.path.write_bytes(b"\x89PNG\r\n\x1a\nimage")
        self.provider = Provider(str(self.path))
        self.config = {"image_planner_provider_id": "grok_oauth/grok-4.6"}
        self.context = SimpleNamespace(get_provider_by_id=lambda _: self.provider)

    def service(self, **extra):
        return module.GrokOAuthImageService(context=self.context, config={**self.config, **extra})

    async def test_generation_uses_imagine_model_and_exact_geometry(self):
        result = await self.service().generate(prompt="保留自然语言描述", size="16:9")
        self.assertEqual(result.path, str(self.path))
        self.assertEqual(result.backend, "grok_oauth")
        call = self.provider.calls[0]
        self.assertEqual(call["model"], "grok-imagine-image-2.0")
        self.assertEqual(call["aspect_ratio"], "16:9")
        self.assertEqual(call["resolution"], "1k")
        self.assertNotIn("size", call)
        self.assertEqual(call["n"], 1)
        self.assertEqual(call["prompt"], "保留自然语言描述")

    async def test_edit_sends_original_image_as_data_uri(self):
        await self.service().edit(prompt="替换人物，保留背景", image_path=str(self.path))
        call = self.provider.calls[0]
        self.assertEqual(call["action"], "edit")
        import base64
        uri = call["reference_images"][0]
        self.assertTrue(uri.startswith("data:image/png;base64,"))
        self.assertEqual(base64.b64decode(uri.split(",", 1)[1]), self.path.read_bytes())

    async def test_jpeg_reference_bytes_are_preserved(self):
        content = b"\xff\xd8\xff" + b"jpeg reference bytes"
        self.path.write_bytes(content)
        await self.service().edit(prompt="replace subject", image_path=str(self.path))
        uri = self.provider.calls[0]["reference_images"][0]
        self.assertTrue(uri.startswith("data:image/jpeg;base64,"))
        import base64
        self.assertEqual(base64.b64decode(uri.split(",", 1)[1]), content)

    async def test_oversized_reference_is_rejected_before_sdk(self):
        limit = module.GrokOAuthImageService.MAX_REFERENCE_BYTES
        self.assertEqual(limit, 20 * 1024 * 1024)
        with self.path.open("wb") as stream:
            stream.write(b"\x89PNG\r\n\x1a\n")
            stream.truncate(limit + 1)
        with self.assertRaises(module.GrokOAuthImageUserError):
            await self.service().edit(prompt="replace subject", image_path=str(self.path))
        self.assertEqual(self.provider.calls, [])

    async def test_disabled_capability_stops_before_request(self):
        self.provider.capabilities["image_generation"]["enabled"] = False
        with self.assertRaises(module.GrokOAuthImageConfigError):
            await self.service().generate(prompt="cat")
        self.assertEqual(self.provider.calls, [])

    async def test_wrong_provider_type_is_rejected(self):
        self.provider.meta = lambda: SimpleNamespace(type="openai_oauth_chat_completion")
        with self.assertRaises(module.GrokOAuthImageConfigError):
            await self.service().generate(prompt="cat")
        self.assertEqual(self.provider.calls, [])

    async def test_invalid_geometry_or_text_model_never_calls_provider(self):
        for config, size in (({}, "13:7"), ({"grok_image_model": "grok-4.6"}, "1:1")):
            with self.subTest(config=config, size=size):
                with self.assertRaises((module.GrokOAuthImageUserError, module.GrokOAuthImageConfigError)):
                    await self.service(**config).generate(prompt="cat", size=size)
        self.assertEqual(self.provider.calls, [])

    async def test_rate_limit_and_unknown_outcome_are_not_retried(self):
        for name in ("RateLimited", "OutcomeUnknown"):
            self.provider.calls.clear()
            async def fail(**kwargs):
                self.provider.calls.append(kwargs)
                raise type(name, (Exception,), {})("token=SECRET /private/path")
            self.provider.generate_image = fail
            with self.assertRaises(module.GrokOAuthImageProviderError) as caught:
                await self.service().generate(prompt="cat")
            self.assertEqual(len(self.provider.calls), 1)
            self.assertNotIn("SECRET", str(caught.exception))
            self.assertIsNone(caught.exception.__context__)

    async def test_cancellation_propagates(self):
        async def cancel(**kwargs):
            raise asyncio.CancelledError()
        self.provider.generate_image = cancel
        with self.assertRaises(asyncio.CancelledError):
            await self.service().generate(prompt="cat")

    async def test_timeout_cancels_single_request(self):
        cancelled = []
        async def wait(**kwargs):
            self.provider.calls.append(kwargs)
            try:
                await asyncio.sleep(10)
            finally:
                cancelled.append(True)
        self.provider.generate_image = wait
        with self.assertRaises(module.GrokOAuthImageProviderError) as caught:
            await self.service(grok_image_timeout=1).generate(prompt="cat")
        self.assertEqual(caught.exception.reason_code, "provider_timeout")
        self.assertEqual(len(self.provider.calls), 1)
        self.assertEqual(cancelled, [True])

    async def test_concurrent_requests_keep_independent_geometry(self):
        config_before = dict(self.config)
        await asyncio.gather(self.service().generate(prompt="landscape", size="16:9"),
                             self.service().generate(prompt="portrait", size="9:16"))
        self.assertEqual({(c["prompt"], c["aspect_ratio"]) for c in self.provider.calls},
                         {("landscape", "16:9"), ("portrait", "9:16")})
        self.assertEqual(self.config, config_before)

    async def test_invalid_prompt_and_timeout_do_not_call_provider(self):
        for prompt, config in (("", {}), ("x" * 2049, {}), ("cat", {"grok_image_timeout": 601}),
                               ("cat", {"grok_image_timeout": float("inf")})):
            with self.subTest(length=len(prompt), config=config):
                with self.assertRaises((module.GrokOAuthImageUserError, module.GrokOAuthImageConfigError)):
                    await self.service(**config).generate(prompt=prompt)
        self.assertEqual(self.provider.calls, [])

    async def test_missing_output_and_source_fail_without_exposing_paths(self):
        self.provider.path = str(self.path.parent / "missing.png")
        with self.assertRaises(module.GrokOAuthImageProviderError) as caught:
            await self.service().generate(prompt="cat")
        self.assertNotIn(str(self.path.parent), str(caught.exception))
        self.provider.calls.clear()
        with self.assertRaises(module.GrokOAuthImageUserError):
            await self.service().edit(prompt="cat", image_path=self.provider.path)
        self.assertEqual(self.provider.calls, [])


if __name__ == "__main__":
    unittest.main()
