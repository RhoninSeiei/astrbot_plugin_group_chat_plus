import asyncio
import importlib.util
import sys
import tempfile
import traceback
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_service_module():
    module_path = REPO_ROOT / "utils" / "codex_oauth_image_service.py"
    spec = importlib.util.spec_from_file_location(
        "codex_oauth_image_service_test", module_path
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


service_module = _load_service_module()
CodexOAuthImageConfigError = service_module.CodexOAuthImageConfigError
CodexOAuthImageProviderError = service_module.CodexOAuthImageProviderError
CodexOAuthImageService = service_module.CodexOAuthImageService
CodexOAuthImageUserError = service_module.CodexOAuthImageUserError


class FakeProvider:
    capabilities = {"image_generate": True, "image_edit": True}

    def __init__(self, result_path: Path, provider_id="openai_oauth/gpt-5.6-sol"):
        self.result_path = result_path
        self.provider_id = provider_id
        self.calls = []
        self.observed_timeouts = []
        self._timeout = 120
        self.timeout_write_count = 0

    @property
    def timeout(self):
        return self._timeout

    @timeout.setter
    def timeout(self, value):
        self.timeout_write_count += 1
        self._timeout = value

    def meta(self):
        return SimpleNamespace(id=self.provider_id)

    async def generate_image(self, **kwargs):
        self.calls.append(kwargs)
        self.observed_timeouts.append(self.timeout)
        return [
            SimpleNamespace(
                path=str(self.result_path),
                mime_type="image/png",
                revised_prompt="revised",
            )
        ]


class FailingProvider(FakeProvider):
    async def generate_image(self, **kwargs):
        self.calls.append(kwargs)
        raise RuntimeError(
            "provider failed with sensitive-token-value at "
            "/private/codex/oauth-token.json"
        )


class ConcurrentProvider(FakeProvider):
    def __init__(self, result_path: Path):
        super().__init__(result_path)
        self.active_calls = 0
        self.max_active_calls = 0

    async def generate_image(self, **kwargs):
        self.calls.append(kwargs)
        self.active_calls += 1
        self.max_active_calls = max(self.max_active_calls, self.active_calls)
        self.observed_timeouts.append(self.timeout)
        try:
            await asyncio.sleep(0.02)
            return [SimpleNamespace(path=str(self.result_path))]
        finally:
            self.active_calls -= 1


class SlowProvider(FakeProvider):
    def __init__(self, result_path: Path):
        super().__init__(result_path)
        self.cancelled = False

    async def generate_image(self, **kwargs):
        self.calls.append(kwargs)
        try:
            await asyncio.sleep(60)
        finally:
            self.cancelled = True


class ExplodingIterable:
    def __iter__(self):
        raise RuntimeError(
            "iteration exposed sensitive-token-value at /private/codex/results.json"
        )


class ExplodingResult:
    @property
    def path(self):
        raise RuntimeError(
            "result exposed sensitive-token-value at /private/codex/result.png"
        )


class ExplodingFloat:
    def __float__(self):
        raise RuntimeError(
            "timeout exposed sensitive-token-value at /private/codex/config.json"
        )


class FakeContext:
    def __init__(self, *providers):
        self.providers = list(providers)
        self.get_all_calls = 0

    def get_all_providers(self):
        self.get_all_calls += 1
        return list(self.providers)

    def get_provider_by_id(self, provider_id):
        raise AssertionError("图片适配器不得调用 get_provider_by_id")


class CodexOAuthImageServiceTest(unittest.TestCase):
    def make_service(self, provider=None, *, timeout=300, context=None, provider_id=None):
        if context is None:
            context = FakeContext(provider) if provider is not None else FakeContext()
        return CodexOAuthImageService(
            context=context,
            config={
                "codex_oauth_image_provider_id": (
                    provider_id or "openai_oauth/gpt-5.6-sol"
                ),
                "codex_oauth_image_model": "gpt-5.6-sol",
                "codex_oauth_image_default_size": "1024x1024",
                "codex_oauth_image_timeout": timeout,
            },
        )

    def assert_sanitized_error(self, error):
        rendered = "".join(traceback.format_exception(error))
        for sensitive in (
            "sensitive-token-value",
            "/private/codex/",
            "oauth-token.json",
            "results.json",
            "result.png",
            "config.json",
            "openai_oauth/private-provider",
        ):
            self.assertNotIn(sensitive, str(error))
            self.assertNotIn(sensitive, rendered)
        self.assertIsNone(error.__cause__)
        self.assertIsNone(error.__context__)

    def test_generate_uses_public_provider_list_without_mutating_timeout(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result_path = Path(tmpdir) / "result.png"
            result_path.write_bytes(b"png")
            unrelated = FakeProvider(result_path, "other/provider")
            provider = FakeProvider(result_path)
            context = FakeContext(unrelated, provider)
            result = asyncio.run(
                self.make_service(context=context).generate(
                    prompt="orange cat", size="16:9"
                )
            )

        self.assertEqual(context.get_all_calls, 1)
        self.assertEqual(provider.calls, [{
            "prompt": "orange cat",
            "model": "gpt-5.6-sol",
            "size": "1536x1024",
            "n": 1,
            "reference_images": None,
            "action": "generate",
        }])
        self.assertEqual(provider.observed_timeouts, [120])
        self.assertEqual(provider.timeout, 120)
        self.assertEqual(provider.timeout_write_count, 0)
        self.assertEqual(result.backend, "codex_oauth")
        self.assertEqual(result.revised_prompt, "revised")

    def test_adapter_source_uses_only_public_lookup_and_wait_for(self):
        source = (REPO_ROOT / "utils" / "codex_oauth_image_service.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("get_all_providers", source)
        self.assertIn("provider.meta()", source)
        self.assertIn("asyncio.wait_for", source)
        self.assertNotIn("get_provider_by_id", source)
        self.assertNotIn("_temporary_provider_timeout", source)
        self.assertNotIn("_provider_timeout_lock", source)
        self.assertNotIn("provider.timeout", source)

    def test_edit_passes_reference_path_and_edit_action(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "source.png"
            result_path = Path(tmpdir) / "result.png"
            source.write_bytes(b"source")
            result_path.write_bytes(b"result")
            provider = FakeProvider(result_path)
            asyncio.run(
                self.make_service(provider).edit(
                    prompt="change the sky", image_path=str(source)
                )
            )

        self.assertEqual(provider.calls[0]["reference_images"], [str(source)])
        self.assertEqual(provider.calls[0]["action"], "edit")
        self.assertEqual(provider.timeout_write_count, 0)

    def test_size_aliases_are_width_by_height(self):
        self.assertEqual(CodexOAuthImageService.normalize_size("1:1"), "1024x1024")
        self.assertEqual(
            CodexOAuthImageService.normalize_size("1920x1080"), "1536x1024"
        )
        self.assertEqual(
            CodexOAuthImageService.normalize_size("1080x1920"), "1024x1536"
        )
        with self.assertRaises(CodexOAuthImageUserError):
            CodexOAuthImageService.normalize_size("768x1360")

    def test_missing_capability_and_missing_result_are_safe_errors(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            provider = FakeProvider(Path(tmpdir) / "missing.png")
            provider.capabilities = {"image_generate": False, "image_edit": False}
            with self.assertRaises(CodexOAuthImageConfigError):
                asyncio.run(
                    self.make_service(provider).generate(prompt="cat", size="1:1")
                )

            provider.capabilities = {"image_generate": True, "image_edit": True}
            with self.assertRaises(CodexOAuthImageProviderError) as caught:
                asyncio.run(
                    self.make_service(provider).generate(prompt="cat", size="1:1")
                )

        self.assertNotIn(str(provider.result_path), str(caught.exception))
        self.assertNotIn("openai_oauth/gpt-5.6-sol", str(caught.exception))

    def test_provider_error_is_fixed_and_drops_sensitive_exception_chain(self):
        provider = FailingProvider(Path("unused.png"))
        with self.assertRaises(CodexOAuthImageProviderError) as caught:
            asyncio.run(
                self.make_service(provider).generate(prompt="cat", size="1:1")
            )

        self.assertEqual(
            str(caught.exception), "Codex OAuth 图片 Provider 调用失败。"
        )
        self.assertEqual(provider.timeout_write_count, 0)
        self.assert_sanitized_error(caught.exception)

    def test_concurrent_calls_do_not_serialize_or_mutate_provider_state(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result_path = Path(tmpdir) / "result.png"
            result_path.write_bytes(b"result")
            provider = ConcurrentProvider(result_path)
            first = self.make_service(provider, timeout=111)
            second = self.make_service(provider, timeout=222)

            async def run_concurrently():
                await asyncio.gather(
                    first.generate(prompt="first", size="1:1"),
                    second.generate(prompt="second", size="1:1"),
                )

            asyncio.run(run_concurrently())

        self.assertEqual(provider.max_active_calls, 2)
        self.assertEqual(provider.observed_timeouts, [120, 120])
        self.assertEqual(provider.timeout_write_count, 0)
        self.assertEqual(provider.timeout, 120)
        self.assertNotIn("_codex_oauth_image_timeout_lock", provider.__dict__)

    def test_timeout_cancels_provider_and_maps_to_fixed_error(self):
        provider = SlowProvider(Path("unused.png"))
        service = self.make_service(provider)
        service._resolve_timeout = lambda: 0.01

        with self.assertRaises(CodexOAuthImageProviderError) as caught:
            asyncio.run(service.generate(prompt="cat", size="1:1"))

        self.assertEqual(str(caught.exception), "Codex OAuth 图片 Provider 调用超时。")
        self.assertTrue(provider.cancelled)
        self.assertEqual(provider.timeout_write_count, 0)
        self.assert_sanitized_error(caught.exception)

    def test_provider_lookup_failures_do_not_expose_configured_id(self):
        sensitive_id = "openai_oauth/private-provider"

        class ExplodingContext:
            def get_all_providers(self):
                raise RuntimeError(
                    "lookup exposed sensitive-token-value at /private/codex/provider.json"
                )

        class ExplodingMetaProvider(FakeProvider):
            def meta(self):
                raise RuntimeError(
                    "meta exposed sensitive-token-value at /private/codex/meta.json"
                )

        cases = (
            self.make_service(context=ExplodingContext(), provider_id=sensitive_id),
            self.make_service(
                context=FakeContext(ExplodingMetaProvider(Path("unused.png"))),
                provider_id=sensitive_id,
            ),
        )
        for service in cases:
            with self.subTest(context=type(service.context).__name__):
                with self.assertRaises(CodexOAuthImageProviderError) as caught:
                    asyncio.run(service.generate(prompt="cat", size="1:1"))
                self.assertNotIn(sensitive_id, str(caught.exception))
                self.assert_sanitized_error(caught.exception)

        with self.assertRaises(CodexOAuthImageConfigError) as caught:
            asyncio.run(
                self.make_service(context=FakeContext(), provider_id=sensitive_id).generate(
                    prompt="cat", size="1:1"
                )
            )
        self.assertNotIn(sensitive_id, str(caught.exception))

    def test_invalid_timeouts_are_sanitized_configuration_errors(self):
        provider = FakeProvider(Path("unused.png"))
        for timeout in (
            ExplodingFloat(),
            float("nan"),
            float("inf"),
            float("-inf"),
        ):
            with self.subTest(timeout=type(timeout).__name__):
                with self.assertRaises(CodexOAuthImageConfigError) as caught:
                    asyncio.run(
                        self.make_service(provider, timeout=timeout).generate(
                            prompt="cat", size="1:1"
                        )
                    )
                self.assert_sanitized_error(caught.exception)

    def test_result_parsing_and_file_checks_are_sanitized(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result_path = Path(tmpdir) / "result.png"
            result_path.write_bytes(b"result")
            iterable_provider = FakeProvider(result_path)

            async def return_exploding_iterable(**kwargs):
                return ExplodingIterable()

            iterable_provider.generate_image = return_exploding_iterable
            result_provider = FakeProvider(result_path)

            async def return_exploding_result(**kwargs):
                return [ExplodingResult()]

            result_provider.generate_image = return_exploding_result

            for provider in (iterable_provider, result_provider):
                with self.subTest(provider=type(provider).__name__):
                    with self.assertRaises(CodexOAuthImageProviderError) as caught:
                        asyncio.run(
                            self.make_service(provider).generate(
                                prompt="cat", size="1:1"
                            )
                        )
                    self.assert_sanitized_error(caught.exception)

            provider = FakeProvider(result_path)
            with patch.object(
                Path,
                "is_file",
                side_effect=RuntimeError(
                    "file check exposed sensitive-token-value at /private/codex/file.png"
                ),
            ):
                with self.assertRaises(CodexOAuthImageProviderError) as caught:
                    asyncio.run(
                        self.make_service(provider).generate(prompt="cat", size="1:1")
                    )
            self.assert_sanitized_error(caught.exception)


if __name__ == "__main__":
    unittest.main()
