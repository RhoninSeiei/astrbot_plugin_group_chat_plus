import copy
import importlib.util
from pathlib import Path
import tempfile
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]
GUARD_PATH = REPO_ROOT / "utils" / "llm_runtime_guard.py"


def _load_guard_module():
    spec = importlib.util.spec_from_file_location(
        "llm_runtime_guard_test_module",
        GUARD_PATH,
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class LLMRequestImageSanitizationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.guard = _load_guard_module()

    def test_removes_missing_context_images_without_mutating_input(self):
        missing_path = "/AstrBot/data/temp/missing-history-image.jpg"
        contexts = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "保留这段文字"},
                    {
                        "type": "image_url",
                        "image_url": {"url": missing_path, "detail": "auto"},
                    },
                    {
                        "type": "image_url",
                        "image_url": {"url": "https://example.com/valid.jpg"},
                    },
                ],
            },
            {
                "role": "assistant",
                "content": [
                    {"type": "input_image", "image_url": missing_path},
                ],
            },
        ]
        original = copy.deepcopy(contexts)

        result = self.guard.sanitize_llm_request_images(contexts, [])

        self.assertEqual(contexts, original)
        self.assertEqual(result.removed_context_parts, 2)
        self.assertEqual(result.removed_empty_messages, 1)
        self.assertEqual(len(result.contexts), 1)
        self.assertEqual(
            result.contexts[0]["content"],
            [
                {"type": "text", "text": "保留这段文字"},
                {
                    "type": "image_url",
                    "image_url": {"url": "https://example.com/valid.jpg"},
                },
            ],
        )

    def test_keeps_remote_data_and_existing_local_images(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            local_image = Path(temp_dir) / "present.jpg"
            local_image.write_bytes(b"image")
            image_urls = [
                "https://example.com/a.jpg",
                "http://example.com/b.jpg",
                "data:image/png;base64,AAAA",
                str(local_image),
                "/AstrBot/data/temp/missing-current-image.jpg",
                "relative/missing-image.jpg",
            ]

            result = self.guard.sanitize_llm_request_images([], image_urls)

        self.assertEqual(
            result.image_urls,
            image_urls[:4],
        )
        self.assertEqual(result.removed_image_urls, 2)

    def test_preserves_non_image_context_parts_and_plain_text_messages(self):
        contexts = [
            {"role": "system", "content": "人格提示词"},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "问题"},
                    {"type": "input_text", "text": "补充"},
                ],
            },
        ]

        result = self.guard.sanitize_llm_request_images(contexts, None)

        self.assertEqual(result.contexts, contexts)
        self.assertEqual(result.image_urls, [])
        self.assertEqual(result.removed_context_parts, 0)
        self.assertEqual(result.removed_image_urls, 0)
        self.assertEqual(result.removed_empty_messages, 0)


class RawLLMFailureClassificationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.guard = _load_guard_module()

    def test_classifies_quota_failure_without_returning_raw_details(self):
        raw = (
            "LLM 响应错误: All chat models failed: PermissionDeniedError: "
            "insufficient_user_quota request id: secret-request-id"
        )

        reason = self.guard.classify_raw_llm_failure(raw)
        prompt = self.guard.build_persona_failure_prompt(reason)

        self.assertEqual(reason, "provider_quota")
        self.assertNotIn("secret-request-id", prompt)
        self.assertNotIn("insufficient_user_quota", prompt)
        self.assertNotIn("PermissionDeniedError", prompt)

    def test_classifies_provider_timeout(self):
        raw = (
            "LLM 响应错误: All chat models failed: InternalServerError: "
            "Error 522: Connection timed out by Cloudflare"
        )

        self.assertEqual(
            self.guard.classify_raw_llm_failure(raw),
            "provider_timeout",
        )

    def test_classifies_invalid_history_image(self):
        raw = (
            "LLM 响应错误: Invalid 'input[32].content[1].image_url'. "
            "Expected a valid URL, but got a value with an invalid format."
        )

        self.assertEqual(
            self.guard.classify_raw_llm_failure(raw),
            "invalid_history_image",
        )

    def test_ignores_normal_discussion_of_error_text(self):
        text = "日志里出现了 All chat models failed，这是什么意思？"

        self.assertIsNone(self.guard.classify_raw_llm_failure(text))

    def test_rejects_persona_reply_that_contains_internal_details(self):
        unsafe = (
            "供应商额度不足，request id: abc，详见 https://internal.example/error"
        )

        self.assertEqual(
            self.guard.sanitize_persona_failure_reply(unsafe),
            "",
        )

    def test_accepts_short_persona_reply_and_normalizes_whitespace(self):
        safe = "  画笔临时卡住了，\n稍后再试一次。  "

        self.assertEqual(
            self.guard.sanitize_persona_failure_reply(safe),
            "画笔临时卡住了，稍后再试一次。",
        )


class LLMRuntimeGuardIntegrationSourceTest(unittest.TestCase):
    def setUp(self):
        self.main_source = (REPO_ROOT / "main.py").read_text(encoding="utf-8")

    def test_incoming_request_is_sanitized_before_plugin_marker_gate(self):
        method = self.main_source.split(
            "async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):",
            1,
        )[1].split("@filter.on_llm_response", 1)[0]

        sanitize_pos = method.index(
            'self._sanitize_llm_request_images(event, req, stage="incoming")'
        )
        marker_pos = method.index("is_plugin_request = event.get_extra")
        restored_pos = method.index(
            'self._sanitize_llm_request_images(event, req, stage="plugin_restored")'
        )
        contexts_restore_pos = method.index("req.contexts = plugin_contexts")

        self.assertLess(sanitize_pos, marker_pos)
        self.assertLess(contexts_restore_pos, restored_pos)

    def test_raw_failure_is_personalized_before_processing_session_gate(self):
        method = self.main_source.split(
            "async def on_decorating_result(self, event: AstrMessageEvent):",
            1,
        )[1].split("@filter.after_message_sent", 1)[0]

        failure_pos = method.index("classify_raw_llm_failure(reply_text)")
        processing_gate_pos = method.index(
            "if message_id not in self.processing_sessions:"
        )

        self.assertLess(failure_pos, processing_gate_pos)
        self.assertIn("await self._build_persona_llm_failure_reply", method)
        self.assertIn("LLM_RUNTIME_FAILURE_DETECTED", method)

    def test_persona_rewrite_uses_no_history_images_tools_or_raw_error(self):
        helper = self.main_source.split(
            "async def _build_persona_llm_failure_reply(",
            1,
        )[1].split("@filter.on_decorating_result", 1)[0]

        self.assertIn("contexts=[]", helper)
        self.assertIn("image_urls=[]", helper)
        self.assertIn("tools=None", helper)
        self.assertIn("build_persona_failure_prompt(reason_code)", helper)
        self.assertNotIn("raw_error", helper)
        self.assertNotIn("raw_text", helper)


if __name__ == "__main__":
    unittest.main()
