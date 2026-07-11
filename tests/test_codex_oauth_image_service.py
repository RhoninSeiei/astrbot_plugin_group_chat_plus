import asyncio
import importlib.util
import inspect
import logging
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


class ExplicitTimeoutProvider(FakeProvider):
    async def generate_image(
        self,
        *,
        prompt,
        model,
        size,
        n,
        reference_images,
        action,
        timeout=None,
    ):
        self.calls.append({
            "prompt": prompt,
            "model": model,
            "size": size,
            "n": n,
            "reference_images": reference_images,
            "action": action,
            "timeout": timeout,
        })
        return [SimpleNamespace(path=str(self.result_path))]


class LegacyProvider(FakeProvider):
    async def generate_image(
        self,
        *,
        prompt,
        model,
        size,
        n,
        reference_images,
        action,
    ):
        self.calls.append({
            "prompt": prompt,
            "model": model,
            "size": size,
            "n": n,
            "reference_images": reference_images,
            "action": action,
        })
        return [SimpleNamespace(path=str(self.result_path))]


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
            "timeout": 300.0,
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
        self.assertIn("inspect.signature", source)
        self.assertIn("asyncio.wait_for", source)
        self.assertNotIn("get_provider_by_id", source)
        self.assertNotIn("_temporary_provider_timeout", source)
        self.assertNotIn("_provider_timeout_lock", source)
        self.assertNotIn("provider.timeout", source)

    def test_documentation_explains_provider_timeout_compatibility(self):
        expectations = {
            "README.md": (
                "支持可选 `timeout` 参数或 `**kwargs` 的 Provider",
                "旧 Provider 只受插件外层最大等待限制",
                "生产 Codex OAuth Provider 已支持单次超时参数",
            ),
            "docs/CONFIG_REFERENCE.md": (
                "支持可选 `timeout` 参数或 `**kwargs` 的 Provider",
                "实际请求仍可能受 Provider 自身 HTTP 超时约束",
            ),
            "docs/PROJECT_STRUCTURE.md": (
                "按签名检测可选 `timeout`",
                "旧 Provider 保留原调用参数",
            ),
            "docs/MESSAGE_WORKFLOW.md": (
                "单次 Provider 超时与外层最大等待值一致",
                "无法读取签名时按旧 Provider 处理",
            ),
            "CHANGELOG.md": (
                "逐个跳过元数据异常或无有效 ID 的 Provider 成员",
                "生产 Codex OAuth Provider 已支持单次超时参数",
            ),
        }
        for relative_path, snippets in expectations.items():
            with self.subTest(path=relative_path):
                source = (REPO_ROOT / relative_path).read_text(encoding="utf-8")
                for snippet in snippets:
                    self.assertIn(snippet, source)

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
        self.assertEqual(provider.calls[0]["timeout"], 300.0)
        self.assertEqual(provider.timeout_write_count, 0)

    def test_explicit_timeout_parameter_receives_same_outer_timeout_once(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result_path = Path(tmpdir) / "result.png"
            result_path.write_bytes(b"result")
            provider = ExplicitTimeoutProvider(result_path)
            observed_outer_timeouts = []

            async def recording_wait_for(awaitable, timeout):
                observed_outer_timeouts.append(timeout)
                return await awaitable

            with patch.object(
                service_module.asyncio,
                "wait_for",
                side_effect=recording_wait_for,
            ):
                asyncio.run(
                    self.make_service(provider, timeout=123).generate(
                        prompt="cat", size="1:1"
                    )
                )

        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(provider.calls[0]["timeout"], 123.0)
        self.assertEqual(observed_outer_timeouts, [123.0])

    def test_kwargs_provider_receives_configured_timeout_once(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result_path = Path(tmpdir) / "result.png"
            result_path.write_bytes(b"result")
            provider = FakeProvider(result_path)
            asyncio.run(
                self.make_service(provider, timeout=234).generate(
                    prompt="cat", size="1:1"
                )
            )

        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(provider.calls[0]["timeout"], 234.0)

    def test_legacy_provider_omits_timeout_and_is_called_once(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result_path = Path(tmpdir) / "result.png"
            result_path.write_bytes(b"result")
            provider = LegacyProvider(result_path)
            legacy_signature = inspect.signature(provider.generate_image)
            with patch(
                "inspect.signature", return_value=legacy_signature
            ) as signature:
                asyncio.run(
                    self.make_service(provider, timeout=345).generate(
                        prompt="cat", size="1:1"
                    )
                )

        signature.assert_called_once_with(provider.generate_image)
        self.assertEqual(len(provider.calls), 1)
        self.assertNotIn("timeout", provider.calls[0])

    def test_signature_read_failure_uses_legacy_call_once(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result_path = Path(tmpdir) / "result.png"
            result_path.write_bytes(b"result")
            provider = FakeProvider(result_path)
            with patch(
                "inspect.signature",
                side_effect=RuntimeError("signature read failed"),
            ) as signature:
                asyncio.run(
                    self.make_service(provider, timeout=456).generate(
                        prompt="cat", size="1:1"
                    )
                )

        signature.assert_called_once_with(provider.generate_image)
        self.assertEqual(len(provider.calls), 1)
        self.assertNotIn("timeout", provider.calls[0])

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

    def test_provider_lookup_skips_broken_member_before_target(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result_path = Path(tmpdir) / "result.png"
            result_path.write_bytes(b"result")

            class ExplodingMetaProvider(FakeProvider):
                def meta(self):
                    raise RuntimeError("unrelated provider metadata failed")

            provider = FakeProvider(result_path)
            context = FakeContext(ExplodingMetaProvider(result_path), provider)
            result = asyncio.run(
                self.make_service(context=context).generate(
                    prompt="cat", size="1:1"
                )
            )

        self.assertEqual(context.get_all_calls, 1)
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(result.path, str(result_path))

    def test_provider_list_failure_does_not_expose_configured_id(self):
        sensitive_id = "openai_oauth/private-provider"

        class ExplodingContext:
            def get_all_providers(self):
                raise RuntimeError(
                    "lookup exposed sensitive-token-value at /private/codex/provider.json"
                )

        service = self.make_service(
            context=ExplodingContext(), provider_id=sensitive_id
        )
        with self.assertRaises(CodexOAuthImageProviderError) as caught:
            asyncio.run(service.generate(prompt="cat", size="1:1"))
        self.assertNotIn(sensitive_id, str(caught.exception))
        self.assert_sanitized_error(caught.exception)

    def test_all_invalid_provider_members_return_fixed_safe_config_error(self):
        sensitive_id = "openai_oauth/private-provider"

        class ExplodingMetaProvider(FakeProvider):
            def meta(self):
                raise RuntimeError(
                    f"metadata failed for {sensitive_id} with sensitive-token-value"
                )

        class NullMetaProvider(FakeProvider):
            def meta(self):
                return None

        class MissingIdMetaProvider(FakeProvider):
            def meta(self):
                return SimpleNamespace(name=sensitive_id)

        records = []

        class RecordingHandler(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        handler = RecordingHandler()
        root_logger = logging.getLogger()
        root_logger.addHandler(handler)
        try:
            service = self.make_service(
                context=FakeContext(
                    ExplodingMetaProvider(Path("unused.png")),
                    NullMetaProvider(Path("unused.png")),
                    MissingIdMetaProvider(Path("unused.png")),
                ),
                provider_id=sensitive_id,
            )
            with self.assertRaises(CodexOAuthImageConfigError) as caught:
                asyncio.run(service.generate(prompt="cat", size="1:1"))
        finally:
            root_logger.removeHandler(handler)

        rendered = "".join(traceback.format_exception(caught.exception))
        self.assertEqual(
            str(caught.exception), "Codex OAuth 图片 Provider 不存在。"
        )
        self.assertNotIn(sensitive_id, "\n".join(records))
        self.assertNotIn(sensitive_id, rendered)
        self.assert_sanitized_error(caught.exception)

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
