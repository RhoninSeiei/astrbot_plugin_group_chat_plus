import asyncio
import importlib.util
import sys
import types
import traceback
import unittest
from pathlib import Path
from types import SimpleNamespace


REPO_ROOT = Path(__file__).resolve().parents[1]
TEST_PACKAGE = "group_image_service_test_utils"


def _load_service_module():
    package = types.ModuleType(TEST_PACKAGE)
    package.__path__ = [str(REPO_ROOT / "utils")]
    sys.modules[TEST_PACKAGE] = package

    module_name = f"{TEST_PACKAGE}.group_image_service"
    module_path = REPO_ROOT / "utils" / "group_image_service.py"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


service_module = _load_service_module()
GroupImageConfigError = service_module.GroupImageConfigError
GroupImageProviderError = service_module.GroupImageProviderError
GroupImageService = service_module.GroupImageService
GroupImageUserError = service_module.GroupImageUserError


class RecordingBackend:
    def __init__(self, name):
        self.name = name
        self.calls = []

    async def generate(self, **kwargs):
        self.calls.append(("generate", kwargs))
        return SimpleNamespace(
            path="result.png",
            mode="generate",
            backend=self.name,
            media_type="image/png",
            revised_prompt="revised",
        )

    async def edit(self, **kwargs):
        self.calls.append(("edit", kwargs))
        return SimpleNamespace(
            path="result.png",
            mode="edit",
            backend=self.name,
            media_type="image/webp",
            revised_prompt="revised",
        )


class FailingBackend:
    def __init__(self, error):
        self.error = error

    async def generate(self, **kwargs):
        raise self.error

    async def edit(self, **kwargs):
        raise self.error


SENSITIVE_ERROR = (
    "provider=openai_oauth/private-provider "
    "token=sk-test-sensitive-value "
    "file=C:\\private\\images\\result.png"
)


def raising_factory(error):
    def factory(**kwargs):
        raise error

    return factory


class GroupImageServiceTest(unittest.TestCase):
    def make_service(
        self,
        *,
        config=None,
        stepfun=None,
        codex=None,
        output_dir=None,
        stepfun_factory=None,
        codex_factory=None,
    ):
        return GroupImageService(
            context=object(),
            config=config or {},
            output_dir=Path("unused") if output_dir is None else output_dir,
            stepfun_factory=stepfun_factory
            or (lambda **_: stepfun or RecordingBackend("stepfun")),
            codex_factory=codex_factory
            or (lambda **_: codex or RecordingBackend("codex_oauth")),
        )

    def test_max_prompt_chars_uses_active_backend_adapter_limit(self):
        codex = self.make_service(config={"image_tool_backend": "codex_oauth"})
        stepfun = self.make_service(config={"image_tool_backend": "stepfun"})

        self.assertEqual(codex.max_prompt_chars(), 2048)
        self.assertEqual(stepfun.max_prompt_chars(), 512)

    def test_codex_generate_accepts_2048_characters(self):
        backend = RecordingBackend("codex_oauth")
        service = self.make_service(
            config={"image_tool_backend": "codex_oauth"},
            codex=backend,
        )

        asyncio.run(service.generate(prompt="a" * 2048, size="1:1"))

        self.assertEqual(backend.calls[0][1]["prompt"], "a" * 2048)

    def test_codex_generate_rejects_2049_characters_before_backend_call(self):
        backend = RecordingBackend("codex_oauth")
        service = self.make_service(
            config={"image_tool_backend": "codex_oauth"},
            codex=backend,
        )

        with self.assertRaisesRegex(GroupImageUserError, "2048"):
            asyncio.run(service.generate(prompt="a" * 2049, size="1:1"))

        self.assertEqual(backend.calls, [])

    def test_stepfun_edit_rejects_513_characters_before_backend_call(self):
        backend = RecordingBackend("stepfun")
        service = self.make_service(
            config={"image_tool_backend": "stepfun"},
            stepfun=backend,
        )

        with self.assertRaisesRegex(GroupImageUserError, "512"):
            asyncio.run(
                service.edit(prompt="a" * 513, image_path="input.png")
            )

        self.assertEqual(backend.calls, [])

    def test_old_config_without_backend_uses_stepfun(self):
        stepfun = RecordingBackend("stepfun")
        codex = RecordingBackend("codex_oauth")
        service = self.make_service(
            config={"enable_step_image_tools": True},
            stepfun=stepfun,
            codex=codex,
        )

        result = asyncio.run(service.generate(prompt="cat", size=""))

        self.assertEqual(stepfun.calls[0][1]["size"], "768x1360")
        self.assertEqual(codex.calls, [])
        self.assertEqual(result.backend, "stepfun")
        self.assertEqual(service.display_name(), "阶跃星辰 Step Image Edit 2")

    def test_explicit_codex_backend_uses_codex_defaults(self):
        stepfun = RecordingBackend("stepfun")
        codex = RecordingBackend("codex_oauth")
        service = self.make_service(
            config={
                "enable_step_image_tools": True,
                "image_tool_backend": "codex_oauth",
                "codex_oauth_image_default_size": "1024x1024",
            },
            stepfun=stepfun,
            codex=codex,
        )

        result = asyncio.run(service.generate(prompt="cat", size=""))

        self.assertEqual(codex.calls[0][1]["size"], "1024x1024")
        self.assertEqual(stepfun.calls, [])
        self.assertEqual(result.backend, "codex_oauth")
        self.assertEqual(service.display_name(), "OpenAI Codex 图像生成服务")

    def test_master_switch_and_backend_validation(self):
        self.assertFalse(
            GroupImageService.is_enabled({"enable_step_image_tools": False})
        )
        self.assertTrue(
            GroupImageService.is_enabled({"enable_step_image_tools": "true"})
        )
        with self.assertRaises(GroupImageConfigError):
            self.make_service(config={"image_tool_backend": "unknown"}).display_name()

    def test_stepfun_requires_output_directory(self):
        service = GroupImageService(
            context=object(),
            config={},
            output_dir=None,
            stepfun_factory=lambda **_: RecordingBackend("stepfun"),
        )

        with self.assertRaises(GroupImageConfigError):
            asyncio.run(service.generate(prompt="cat"))

    def test_edit_routes_arguments_and_converts_result(self):
        codex = RecordingBackend("codex_oauth")
        service = self.make_service(
            config={"image_tool_backend": " CODEX_OAUTH "},
            codex=codex,
        )

        result = asyncio.run(service.edit(prompt="blue sky", image_path="input.png"))

        self.assertEqual(
            codex.calls,
            [("edit", {"prompt": "blue sky", "image_path": "input.png"})],
        )
        self.assertEqual(result.path, "result.png")
        self.assertEqual(result.mode, "edit")
        self.assertEqual(result.media_type, "image/webp")
        self.assertEqual(result.revised_prompt, "revised")

    def test_stepfun_result_keeps_legacy_defaults(self):
        class LegacyStepBackend:
            async def generate(self, **kwargs):
                return SimpleNamespace(path="legacy.png", mode="generate")

        service = self.make_service(stepfun=LegacyStepBackend())

        result = asyncio.run(service.generate(prompt="cat", size="1024x1024"))

        self.assertEqual(result.backend, "stepfun")
        self.assertEqual(result.media_type, "image/png")
        self.assertEqual(result.revised_prompt, "")

    @staticmethod
    def error_cases():
        return (
            (
                service_module.StepImageUserError,
                GroupImageUserError,
                "stepfun",
                "图片提示词不能为空。",
                "图片提示词不能为空。",
            ),
            (
                service_module.StepImageConfigError,
                GroupImageConfigError,
                "stepfun",
                SENSITIVE_ERROR,
                "图片工具配置不可用。",
            ),
            (
                service_module.StepImageProviderError,
                GroupImageProviderError,
                "stepfun",
                SENSITIVE_ERROR,
                "图片服务调用失败。",
            ),
            (
                service_module.CodexOAuthImageUserError,
                GroupImageUserError,
                "codex_oauth",
                "图片尺寸无效。",
                "图片尺寸无效。",
            ),
            (
                service_module.CodexOAuthImageConfigError,
                GroupImageConfigError,
                "codex_oauth",
                SENSITIVE_ERROR,
                "图片工具配置不可用。",
            ),
            (
                service_module.CodexOAuthImageProviderError,
                GroupImageProviderError,
                "codex_oauth",
                SENSITIVE_ERROR,
                "图片服务调用失败。",
            ),
        )

    def assert_safe_mapping(self, caught, expected_message):
        self.assertEqual(str(caught.exception), expected_message)
        if expected_message in {
            "图片工具配置不可用。",
            "图片服务调用失败。",
        }:
            rendered = "".join(traceback.format_exception(caught.exception))
            for sensitive_value in (
                "private-provider",
                "sk-test-sensitive-value",
                "C:\\private\\images\\result.png",
            ):
                self.assertNotIn(sensitive_value, str(caught.exception))
                self.assertNotIn(sensitive_value, rendered)

    def test_factory_errors_map_for_generate_and_edit(self):
        for (
            source_error,
            expected_error,
            backend_name,
            source_message,
            expected_message,
        ) in self.error_cases():
            for operation in ("generate", "edit"):
                with self.subTest(
                    source_error=source_error.__name__, operation=operation
                ):
                    factory = raising_factory(source_error(source_message))
                    service = self.make_service(
                        config={"image_tool_backend": backend_name},
                        stepfun_factory=factory if backend_name == "stepfun" else None,
                        codex_factory=(
                            factory if backend_name == "codex_oauth" else None
                        ),
                    )

                    with self.assertRaises(expected_error) as caught:
                        if operation == "generate":
                            asyncio.run(service.generate(prompt="cat"))
                        else:
                            asyncio.run(
                                service.edit(
                                    prompt="blue sky", image_path="input.png"
                                )
                            )

                    self.assert_safe_mapping(caught, expected_message)

    def test_runtime_generate_errors_map_without_sensitive_details(self):
        for (
            source_error,
            expected_error,
            backend_name,
            source_message,
            expected_message,
        ) in self.error_cases():
            with self.subTest(source_error=source_error.__name__):
                backend = FailingBackend(source_error(source_message))
                service = self.make_service(
                    config={"image_tool_backend": backend_name},
                    stepfun=backend if backend_name == "stepfun" else None,
                    codex=backend if backend_name == "codex_oauth" else None,
                )

                with self.assertRaises(expected_error) as caught:
                    asyncio.run(service.generate(prompt="cat"))

                self.assert_safe_mapping(caught, expected_message)

    def test_runtime_edit_errors_map_without_sensitive_details(self):
        for (
            source_error,
            expected_error,
            backend_name,
            source_message,
            expected_message,
        ) in self.error_cases():
            with self.subTest(source_error=source_error.__name__):
                backend = FailingBackend(source_error(source_message))
                service = self.make_service(
                    config={"image_tool_backend": backend_name},
                    stepfun=backend if backend_name == "stepfun" else None,
                    codex=backend if backend_name == "codex_oauth" else None,
                )

                with self.assertRaises(expected_error) as caught:
                    asyncio.run(
                        service.edit(prompt="blue sky", image_path="input.png")
                    )

                self.assert_safe_mapping(caught, expected_message)

    def test_codex_provider_reason_code_and_backend_are_preserved(self):
        source_error = service_module.CodexOAuthImageProviderError(
            SENSITIVE_ERROR,
            reason_code="provider_timeout",
        )
        backend = FailingBackend(source_error)
        service = self.make_service(
            config={"image_tool_backend": "codex_oauth"},
            codex=backend,
        )

        with self.assertRaises(GroupImageProviderError) as caught:
            asyncio.run(service.generate(prompt="cat"))

        self.assertEqual(str(caught.exception), "图片服务调用失败。")
        self.assertEqual(caught.exception.reason_code, "provider_timeout")
        self.assertEqual(caught.exception.backend, "codex_oauth")
        self.assertIsNone(caught.exception.__cause__)
        self.assertIsNone(caught.exception.__context__)
        self.assert_safe_mapping(caught, "图片服务调用失败。")

    def test_stepfun_provider_error_uses_safe_default_reason_code(self):
        backend = FailingBackend(service_module.StepImageProviderError(SENSITIVE_ERROR))
        service = self.make_service(
            config={"image_tool_backend": "stepfun"},
            stepfun=backend,
        )

        with self.assertRaises(GroupImageProviderError) as caught:
            asyncio.run(service.generate(prompt="cat"))

        self.assertEqual(caught.exception.reason_code, "provider_call_failed")
        self.assertEqual(caught.exception.backend, "stepfun")
        self.assertIsNone(caught.exception.__cause__)
        self.assertIsNone(caught.exception.__context__)
        self.assert_safe_mapping(caught, "图片服务调用失败。")

    def test_provider_error_rejects_untrusted_classification_values(self):
        error = GroupImageProviderError(
            "safe message",
            reason_code="sensitive-token-value",
            backend="private-provider",
        )

        self.assertEqual(error.reason_code, "provider_call_failed")
        self.assertEqual(error.backend, "unknown")


if __name__ == "__main__":
    unittest.main()
