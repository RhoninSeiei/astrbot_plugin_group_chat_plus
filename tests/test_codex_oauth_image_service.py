import asyncio
import importlib.util
import sys
import tempfile
import traceback
import unittest
import weakref
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

    def __init__(self, result_path: Path):
        self.result_path = result_path
        self.calls = []
        self.timeout = 120
        self.timeout_during_call = None

    async def generate_image(self, **kwargs):
        self.calls.append(kwargs)
        self.timeout_during_call = self.timeout
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
        self.timeout_during_call = self.timeout
        raise RuntimeError(
            "provider failed with sensitive-token-value at "
            "/private/codex/oauth-token.json"
        )


class ClassifiedFailingProvider(FakeProvider):
    def __init__(self, result_path: Path, error_type):
        super().__init__(result_path)
        self.error_type = error_type

    async def generate_image(self, **kwargs):
        self.calls.append(kwargs)
        self.timeout_during_call = self.timeout
        raise self.error_type(
            "provider exposed sensitive-token-value, "
            "openai_oauth/private-provider, and /private/codex/provider-error.json"
        )


class ConcurrentProvider(FakeProvider):
    __hash__ = None

    def __init__(self, result_path: Path):
        super().__init__(result_path)
        self.active_calls = 0
        self.max_active_calls = 0
        self.observed_timeouts = []

    async def generate_image(self, **kwargs):
        self.calls.append(kwargs)
        self.active_calls += 1
        self.max_active_calls = max(self.max_active_calls, self.active_calls)
        self.observed_timeouts.append(self.timeout)
        try:
            await asyncio.sleep(0.02)
            return [
                SimpleNamespace(
                    path=str(self.result_path),
                    mime_type="image/png",
                    revised_prompt="",
                )
            ]
        finally:
            self.active_calls -= 1


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
    def __init__(self, provider):
        self.provider = provider

    def get_provider_by_id(self, provider_id):
        if provider_id == "openai_oauth/gpt-5.6-sol":
            return self.provider
        return None


class CodexOAuthImageServiceTest(unittest.TestCase):
    def make_service(self, provider, *, timeout=300, context=None):
        return CodexOAuthImageService(
            context=context or FakeContext(provider),
            config={
                "codex_oauth_image_provider_id": "openai_oauth/gpt-5.6-sol",
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
        ):
            self.assertNotIn(sensitive, str(error))
            self.assertNotIn(sensitive, rendered)
        self.assertIsNone(error.__cause__)
        self.assertIsNone(error.__context__)

    def test_generate_calls_public_provider_api_and_restores_timeout(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result_path = Path(tmpdir) / "result.png"
            result_path.write_bytes(b"png")
            provider = FakeProvider(result_path)
            result = asyncio.run(
                self.make_service(provider).generate(prompt="orange cat", size="16:9")
            )

        self.assertEqual(provider.calls, [{
            "prompt": "orange cat",
            "model": "gpt-5.6-sol",
            "size": "1536x1024",
            "n": 1,
            "reference_images": None,
            "action": "generate",
        }])
        self.assertEqual(provider.timeout_during_call, 300.0)
        self.assertEqual(provider.timeout, 120)
        self.assertEqual(result.backend, "codex_oauth")
        self.assertEqual(result.revised_prompt, "revised")

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

    def test_size_aliases_are_width_by_height(self):
        self.assertEqual(CodexOAuthImageService.normalize_size("1:1"), "1024x1024")
        self.assertEqual(CodexOAuthImageService.normalize_size("1920x1080"), "1536x1024")
        self.assertEqual(CodexOAuthImageService.normalize_size("1080x1920"), "1024x1536")
        with self.assertRaises(CodexOAuthImageUserError):
            CodexOAuthImageService.normalize_size("768x1360")

    def test_missing_capability_and_missing_result_are_safe_errors(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            provider = FakeProvider(Path(tmpdir) / "missing.png")
            provider.capabilities = {"image_generate": False, "image_edit": False}
            with self.assertRaises(CodexOAuthImageConfigError):
                asyncio.run(self.make_service(provider).generate(prompt="cat", size="1:1"))

            provider.capabilities = {"image_generate": True, "image_edit": True}
            with self.assertRaises(CodexOAuthImageProviderError) as caught:
                asyncio.run(self.make_service(provider).generate(prompt="cat", size="1:1"))

        message = str(caught.exception)
        self.assertNotIn(str(provider.result_path), message)
        self.assertNotIn("openai_oauth/gpt-5.6-sol", message)

    def test_provider_error_drops_sensitive_exception_chain_and_restores_timeout(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            provider = FailingProvider(Path(tmpdir) / "unused.png")
            with self.assertRaises(CodexOAuthImageProviderError) as caught:
                asyncio.run(
                    self.make_service(provider).generate(prompt="cat", size="1:1")
                )

        self.assertEqual(provider.timeout_during_call, 300.0)
        self.assertEqual(provider.timeout, 120)
        self.assert_sanitized_error(caught.exception)

    def test_provider_classified_errors_are_always_sanitized_as_provider_errors(self):
        for error_type in (
            CodexOAuthImageUserError,
            CodexOAuthImageConfigError,
        ):
            with self.subTest(error_type=error_type.__name__):
                provider = ClassifiedFailingProvider(Path("unused.png"), error_type)
                with self.assertRaises(CodexOAuthImageProviderError) as caught:
                    asyncio.run(
                        self.make_service(provider).generate(prompt="cat", size="1:1")
                    )

                self.assertEqual(provider.timeout_during_call, 300.0)
                self.assertEqual(provider.timeout, 120)
                self.assert_sanitized_error(caught.exception)
                rendered = "".join(traceback.format_exception(caught.exception))
                self.assertNotIn("openai_oauth/private-provider", rendered)
                self.assertNotIn("openai_oauth/private-provider", str(caught.exception))

    def test_same_provider_calls_are_serialized_with_isolated_timeouts(self):
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

        self.assertEqual(provider.max_active_calls, 1)
        self.assertEqual(provider.observed_timeouts, [111.0, 222.0])
        self.assertEqual(provider.timeout, 120)
        self.assertIsInstance(
            service_module._PROVIDER_TIMEOUT_LOCKS,
            weakref.WeakKeyDictionary,
        )
        self.assertIn("_codex_oauth_image_timeout_lock", provider.__dict__)

    def test_invalid_timeout_drops_sensitive_exception_chain(self):
        provider = FakeProvider(Path("unused.png"))
        with self.assertRaises(CodexOAuthImageConfigError) as caught:
            asyncio.run(
                self.make_service(provider, timeout=ExplodingFloat()).generate(
                    prompt="cat", size="1:1"
                )
            )

        self.assert_sanitized_error(caught.exception)

    def test_non_finite_timeouts_are_configuration_errors(self):
        provider = FakeProvider(Path("unused.png"))
        for timeout in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(timeout=timeout):
                with self.assertRaises(CodexOAuthImageConfigError) as caught:
                    asyncio.run(
                        self.make_service(provider, timeout=timeout).generate(
                            prompt="cat", size="1:1"
                        )
                    )
                self.assertIsNone(caught.exception.__cause__)
                self.assertIsNone(caught.exception.__context__)

    def test_external_provider_interactions_are_sanitized(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result_path = Path(tmpdir) / "result.png"
            result_path.write_bytes(b"result")

            class ExplodingContext:
                def get_provider_by_id(self, provider_id):
                    raise RuntimeError(
                        "lookup exposed sensitive-token-value at /private/codex/provider.json"
                    )

            class ExplodingCapabilitiesProvider(FakeProvider):
                @property
                def capabilities(self):
                    raise RuntimeError(
                        "capabilities exposed sensitive-token-value at /private/codex/capabilities.json"
                    )

            class ExplodingMethodProvider(FakeProvider):
                @property
                def generate_image(self):
                    raise RuntimeError(
                        "method exposed sensitive-token-value at /private/codex/method.json"
                    )

            iterable_provider = FakeProvider(result_path)

            async def return_exploding_iterable(**kwargs):
                return ExplodingIterable()

            iterable_provider.generate_image = return_exploding_iterable
            result_provider = FakeProvider(result_path)

            async def return_exploding_result(**kwargs):
                return [ExplodingResult()]

            result_provider.generate_image = return_exploding_result

            cases = (
                self.make_service(
                    FakeProvider(result_path),
                    context=ExplodingContext(),
                ),
                self.make_service(ExplodingCapabilitiesProvider(result_path)),
                self.make_service(ExplodingMethodProvider(result_path)),
                self.make_service(iterable_provider),
                self.make_service(result_provider),
            )

            for service in cases:
                with self.subTest(service=type(service.context).__name__):
                    with self.assertRaises(CodexOAuthImageProviderError) as caught:
                        asyncio.run(service.generate(prompt="cat", size="1:1"))
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
