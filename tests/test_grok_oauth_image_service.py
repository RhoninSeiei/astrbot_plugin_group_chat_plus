import asyncio
import importlib.util
from pathlib import Path
from types import ModuleType, SimpleNamespace
import unittest
import uuid
import sys


ROOT = Path(__file__).resolve().parents[1]
package = ModuleType("gcp_grok_adapter_test")
package.__path__ = [str(ROOT / "utils")]
package.__spec__ = importlib.util.spec_from_loader("gcp_grok_adapter_test", loader=None, is_package=True)
sys.modules[package.__name__] = package
spec = importlib.util.spec_from_file_location("gcp_grok_adapter_test.grok_oauth_image_service", ROOT / "utils/grok_oauth_image_service.py")
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
        self.path = ROOT / "tests" / f"_grok_test_{uuid.uuid4().hex}.png"
        self.addCleanup(self.path.unlink, missing_ok=True)
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
            self.assertTrue(caught.exception.__suppress_context__)

    async def test_cancellation_propagates(self):
        async def cancel(**kwargs):
            raise asyncio.CancelledError()
        self.provider.generate_image = cancel
        with self.assertRaises(asyncio.CancelledError):
            await self.service().generate(prompt="cat")

    async def test_provider_owns_deadline_and_outcome_unknown_survives(self):
        async def wait(**kwargs):
            self.provider.calls.append(kwargs)
            await asyncio.sleep(1.05)
            raise type("OutcomeUnknown", (Exception,), {"code": "OutcomeUnknown", "request_id": "safe-123"})("secret")
        self.provider.generate_image = wait
        with self.assertRaises(module.GrokOAuthImageProviderError) as caught:
            await self.service(grok_image_timeout=1).generate(prompt="cat")
        self.assertEqual(caught.exception.reason_code, "outcome_unknown")
        self.assertEqual(caught.exception.request_id, "safe-123")
        self.assertEqual(len(self.provider.calls), 1)
        self.assertEqual(self.provider.calls[0]["timeout"], 1)

    async def test_concurrent_requests_keep_independent_geometry(self):
        config_before = dict(self.config)
        await asyncio.gather(self.service().generate(prompt="landscape", size="16:9"),
                             self.service().generate(prompt="portrait", size="9:16"))
        self.assertEqual({(c["prompt"], c["aspect_ratio"]) for c in self.provider.calls},
                         {("landscape", "16:9"), ("portrait", "9:16")})
        self.assertEqual(self.config, config_before)

    async def test_invalid_prompt_and_timeout_do_not_call_provider(self):
        for prompt, config in (("", {}), ("x" * 9217, {}), ("cat", {"grok_image_timeout": 601}),
                               ("cat", {"grok_image_timeout": float("inf")})):
            with self.subTest(length=len(prompt), config=config):
                with self.assertRaises((module.GrokOAuthImageUserError, module.GrokOAuthImageConfigError)):
                    await self.service(**config).generate(prompt=prompt)
        self.assertEqual(self.provider.calls, [])

    async def test_prompt_budget_default_and_override(self):
        await self.service().generate(prompt="x" * 9216)
        await self.service(grok_image_prompt_max_chars=2048).generate(prompt="x" * 2048)
        with self.assertRaises(module.GrokOAuthImageUserError):
            await self.service(grok_image_prompt_max_chars=2048).generate(prompt="x" * 2049)
        for invalid in (2047, 32001, True, "bad"):
            with self.subTest(invalid=invalid), self.assertRaises(module.GrokOAuthImageConfigError):
                await self.service(grok_image_prompt_max_chars=invalid).generate(prompt="cat")
        self.assertEqual(len(self.provider.calls), 2)

    async def test_native_size_and_compatibility_aliases(self):
        for size, ratio, resolution in (("auto", "auto", "1k"), ("3:2@2k", "3:2", "2k"),
                                        ("1080p", "16:9", "1k"), ("2048x2048", "1:1", "2k")):
            with self.subTest(size=size):
                await self.service().generate(prompt="cat", size=size)
                call = self.provider.calls[-1]
                self.assertEqual((call["aspect_ratio"], call["resolution"]), (ratio, resolution))

    async def test_multiple_references_and_total_limit(self):
        other = self.path.with_suffix(".webp")
        self.addCleanup(other.unlink, missing_ok=True)
        other.write_bytes(b"RIFF\x04\x00\x00\x00WEBP")
        await self.service().edit(prompt="combine", image_paths=[str(self.path), str(other)], size="9:16@2k")
        call = self.provider.calls[-1]
        self.assertEqual(len(call["reference_images"]), 2)
        self.assertTrue(call["reference_images"][1].startswith("data:image/webp;base64,"))
        self.assertEqual((call["aspect_ratio"], call["resolution"]), ("9:16", "2k"))
        with self.assertRaises(module.GrokOAuthImageUserError):
            await self.service().edit(prompt="cat", image_paths=[str(self.path)] * 6)

    async def test_total_reference_bytes_rejected_before_sdk(self):
        service = self.service()
        service._reference = lambda _: ("data:image/png;base64,AA==", 20 * 1024 * 1024)
        with self.assertRaises(module.GrokOAuthImageUserError):
            await service.edit(prompt="cat", image_paths=[str(self.path)] * 5)
        self.assertEqual(self.provider.calls, [])

    async def test_reference_rejects_symlink_gif_and_non_regular(self):
        link = self.path.with_suffix(".link")
        self.addCleanup(link.unlink, missing_ok=True)
        try:
            link.symlink_to(self.path)
        except (OSError, NotImplementedError):
            pass
        else:
            with self.assertRaises(module.GrokOAuthImageUserError):
                await self.service().edit(prompt="cat", image_path=str(link))
        self.path.write_bytes(b"GIF89a_data")
        with self.assertRaises(module.GrokOAuthImageUserError):
            await self.service().edit(prompt="cat", image_path=str(self.path))
        self.assertEqual(self.provider.calls, [])

    async def test_typed_provider_error_retains_safe_assets(self):
        async def fail(**kwargs):
            self.provider.calls.append(kwargs)
            error = type("Busy", (Exception,), {"code": "Busy", "partial": True,
                       "assets": [SimpleNamespace(path=str(self.path), mime_type="image/png")],
                       "request_id": "unsafe/token"})("credential")
            raise error
        self.provider.generate_image = fail
        with self.assertRaises(module.GrokOAuthImageProviderError) as caught:
            await self.service().generate(prompt="cat")
        error = caught.exception
        self.assertEqual(error.reason_code, "busy")
        self.assertTrue(error.partial)
        self.assertEqual(error.assets[0].path, str(self.path))
        self.assertEqual(error.request_id, "")
        self.assertNotIn("credential", str(error))
        self.assertEqual(len(self.provider.calls), 1)

    async def test_multiple_outputs_are_error_with_valid_assets(self):
        async def many(**kwargs):
            return [SimpleNamespace(path=str(self.path), mime_type="image/png"),
                    SimpleNamespace(path="missing", mime_type="image/png")]
        self.provider.generate_image = many
        with self.assertRaises(module.GrokOAuthImageProviderError) as caught:
            await self.service().generate(prompt="cat")
        self.assertEqual(caught.exception.reason_code, "invalid_result")
        self.assertEqual([a.path for a in caught.exception.assets], [str(self.path)])

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
