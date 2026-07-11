import asyncio
import importlib.util
import sys
import types
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


class GroupImageServiceTest(unittest.TestCase):
    def make_service(self, *, config=None, stepfun=None, codex=None, output_dir=None):
        return GroupImageService(
            context=object(),
            config=config or {},
            output_dir=Path("unused") if output_dir is None else output_dir,
            stepfun_factory=lambda **_: stepfun or RecordingBackend("stepfun"),
            codex_factory=lambda **_: codex or RecordingBackend("codex_oauth"),
        )

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

    def test_backend_errors_map_to_unified_errors_without_added_details(self):
        cases = (
            (service_module.StepImageUserError, GroupImageUserError, "stepfun"),
            (service_module.StepImageConfigError, GroupImageConfigError, "stepfun"),
            (
                service_module.StepImageProviderError,
                GroupImageProviderError,
                "stepfun",
            ),
            (
                service_module.CodexOAuthImageUserError,
                GroupImageUserError,
                "codex_oauth",
            ),
            (
                service_module.CodexOAuthImageConfigError,
                GroupImageConfigError,
                "codex_oauth",
            ),
            (
                service_module.CodexOAuthImageProviderError,
                GroupImageProviderError,
                "codex_oauth",
            ),
        )

        for source_error, expected_error, backend_name in cases:
            with self.subTest(source_error=source_error.__name__):
                backend = FailingBackend(source_error("图像服务调用失败。"))
                service = self.make_service(
                    config={"image_tool_backend": backend_name},
                    stepfun=backend if backend_name == "stepfun" else None,
                    codex=backend if backend_name == "codex_oauth" else None,
                )

                with self.assertRaises(expected_error) as caught:
                    asyncio.run(service.generate(prompt="cat"))

                self.assertEqual(str(caught.exception), "图像服务调用失败。")
                self.assertNotIn("private-provider", str(caught.exception))
                self.assertNotIn("/private/images/result.png", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
