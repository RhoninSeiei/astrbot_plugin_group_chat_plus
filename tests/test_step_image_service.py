import asyncio
import base64
import importlib.util
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_service_module():
    module_path = REPO_ROOT / "utils" / "step_image_service.py"
    spec = importlib.util.spec_from_file_location("step_image_service_test", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Provider:
    def __init__(self):
        self.provider_config = {
            "id": "stepfun/step-image-edit-2",
            "model": "step-image-edit-2",
            "api_base": "https://api.stepfun.com/v1",
            "key": ["sk-live-secret"],
            "timeout": 30,
            "proxy": "",
        }


class _Context:
    def __init__(self):
        self.provider = _Provider()

    def get_provider_by_id(self, provider_id):
        return self.provider if provider_id == "stepfun/step-image-edit-2" else None

    def get_all_providers(self):
        return [self.provider]


class _SplitProvider:
    def __init__(self):
        self.provider_config = {
            "id": "stepfun/split-step-image",
            "model": "step-image-edit-2",
            "provider_source_id": "stepfun-source",
        }


class _SplitContext:
    def __init__(self):
        self.provider = _SplitProvider()
        self.provider_manager = type(
            "ProviderManager",
            (),
            {
                "provider_sources_config": [
                    {
                        "id": "stepfun-source",
                        "api_base": "https://api.stepfun.com/step_plan/v1",
                        "key": ["sk-source-secret"],
                        "timeout": 45,
                        "proxy": "",
                    }
                ]
            },
        )()

    def get_provider_by_id(self, provider_id):
        return self.provider if provider_id == "stepfun/split-step-image" else None

    def get_all_providers(self):
        return [self.provider]


class _Response:
    def __init__(self, payload=None, error=None):
        self._payload = payload or {
            "data": [{"b64_json": base64.b64encode(b"fake-png").decode()}]
        }
        self._error = error

    def raise_for_status(self):
        if self._error:
            raise RuntimeError(self._error)

    def json(self):
        return self._payload


class _Client:
    def __init__(self, recorder, response):
        self.recorder = recorder
        self.response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url, **kwargs):
        self.recorder.append((url, kwargs))
        return self.response


class StepImageServiceTest(unittest.TestCase):
    def test_enabled_flag_accepts_boolean_strings(self):
        module = _load_service_module()

        self.assertTrue(
            module.StepImageService.is_enabled({"enable_step_image_tools": "true"})
        )
        self.assertFalse(
            module.StepImageService.is_enabled({"enable_step_image_tools": "false"})
        )

    def test_generate_posts_json_and_writes_image(self):
        module = _load_service_module()
        calls = []

        with tempfile.TemporaryDirectory() as tmpdir:
            service = module.StepImageService(
                context=_Context(),
                config={
                    "step_image_provider_id": "stepfun/step-image-edit-2",
                    "step_image_model": "step-image-edit-2",
                },
                output_dir=Path(tmpdir),
                client_factory=lambda **_: _Client(calls, _Response()),
            )

            result = asyncio.run(
                service.generate(prompt="a small orange cat", size="1024x1024")
            )
            image_bytes = Path(result.path).read_bytes()

        self.assertEqual(calls[0][0], "https://api.stepfun.com/v1/images/generations")
        self.assertEqual(calls[0][1]["json"]["model"], "step-image-edit-2")
        self.assertEqual(calls[0][1]["json"]["prompt"], "a small orange cat")
        self.assertEqual(calls[0][1]["json"]["response_format"], "b64_json")
        self.assertTrue(result.path.endswith(".png"))
        self.assertEqual(image_bytes, b"fake-png")

    def test_edit_posts_multipart_without_exposing_key(self):
        module = _load_service_module()
        calls = []

        with tempfile.TemporaryDirectory() as tmpdir:
            image_path = Path(tmpdir) / "input.webp"
            image_path.write_bytes(b"source-image")
            service = module.StepImageService(
                context=_Context(),
                config={"step_image_provider_id": "stepfun/step-image-edit-2"},
                output_dir=Path(tmpdir),
                client_factory=lambda **_: _Client(calls, _Response()),
            )

            result = asyncio.run(
                service.edit(prompt="make the sky blue", image_path=str(image_path))
            )

        self.assertEqual(calls[0][0], "https://api.stepfun.com/v1/images/edits")
        self.assertEqual(calls[0][1]["data"]["model"], "step-image-edit-2")
        self.assertEqual(calls[0][1]["data"]["prompt"], "make the sky blue")
        self.assertIn("image", calls[0][1]["files"])
        self.assertTrue(result.path.endswith(".png"))

    def test_auto_discovers_provider_by_model(self):
        module = _load_service_module()

        service = module.StepImageService(
            context=_Context(),
            config={"step_image_provider_id": ""},
            output_dir=Path(tempfile.gettempdir()),
            client_factory=lambda **_: _Client([], _Response()),
        )

        settings = service.resolve_settings()
        self.assertEqual(settings.provider_id, "stepfun/step-image-edit-2")
        self.assertEqual(settings.api_base, "https://api.stepfun.com/v1")

    def test_resolves_provider_source_split_config(self):
        module = _load_service_module()

        service = module.StepImageService(
            context=_SplitContext(),
            config={"step_image_provider_id": "stepfun/split-step-image"},
            output_dir=Path(tempfile.gettempdir()),
            client_factory=lambda **_: _Client([], _Response()),
        )

        settings = service.resolve_settings()
        self.assertEqual(settings.provider_id, "stepfun/split-step-image")
        self.assertEqual(settings.api_base, "https://api.stepfun.com/step_plan/v1")
        self.assertEqual(settings.timeout, 45)

    def test_rejects_oversized_prompt_and_invalid_size(self):
        module = _load_service_module()
        service = module.StepImageService(
            context=_Context(),
            config={"step_image_provider_id": "stepfun/step-image-edit-2"},
            output_dir=Path(tempfile.gettempdir()),
            client_factory=lambda **_: _Client([], _Response()),
        )

        with self.assertRaises(module.StepImageUserError):
            asyncio.run(service.generate(prompt="x" * 513, size="1024x1024"))

        with self.assertRaises(module.StepImageUserError):
            asyncio.run(service.generate(prompt="cat", size="999x999"))

    def test_sanitizes_provider_errors(self):
        module = _load_service_module()
        calls = []
        service = module.StepImageService(
            context=_Context(),
            config={"step_image_provider_id": "stepfun/step-image-edit-2"},
            output_dir=Path(tempfile.gettempdir()),
            client_factory=lambda **_: _Client(
                calls, _Response(error="request failed with sk-live-secret")
            ),
        )

        with self.assertRaises(module.StepImageProviderError) as caught:
            asyncio.run(service.generate(prompt="cat", size="1024x1024"))

        self.assertNotIn("sk-live-secret", str(caught.exception))
        self.assertIn("[REDACTED]", str(caught.exception))

    def test_invalid_numeric_config_is_configuration_error(self):
        module = _load_service_module()
        service = module.StepImageService(
            context=_Context(),
            config={
                "step_image_provider_id": "stepfun/step-image-edit-2",
                "step_image_steps": "many",
            },
            output_dir=Path(tempfile.gettempdir()),
            client_factory=lambda **_: _Client([], _Response()),
        )

        with self.assertRaises(module.StepImageConfigError):
            service.resolve_settings()


if __name__ == "__main__":
    unittest.main()
