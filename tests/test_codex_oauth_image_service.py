import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from utils.codex_oauth_image_service import (
    CodexOAuthImageConfigError,
    CodexOAuthImageProviderError,
    CodexOAuthImageService,
    CodexOAuthImageUserError,
)


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


class FakeContext:
    def __init__(self, provider):
        self.provider = provider

    def get_provider_by_id(self, provider_id):
        if provider_id == "openai_oauth/gpt-5.6-sol":
            return self.provider
        return None


class CodexOAuthImageServiceTest(unittest.TestCase):
    def make_service(self, provider):
        return CodexOAuthImageService(
            context=FakeContext(provider),
            config={
                "codex_oauth_image_provider_id": "openai_oauth/gpt-5.6-sol",
                "codex_oauth_image_model": "gpt-5.6-sol",
                "codex_oauth_image_default_size": "1024x1024",
                "codex_oauth_image_timeout": 300,
            },
        )

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
