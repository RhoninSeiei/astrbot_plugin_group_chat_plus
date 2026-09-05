import ast
import asyncio
import copy
from pathlib import Path
from types import SimpleNamespace
import unittest

import test_provider_selection_compatibility as selection_tests


ROOT = Path(__file__).resolve().parents[1]


class HostedSearchProvider:
    provider_config = {"id": "openai_oauth/test", "oauth_web_search": "live"}

    def __init__(self, fail=False):
        self.fail = fail
        self.calls = []

    async def text_chat(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs.get("tool_choice") != "none":
            raise AssertionError("Judgment allowed hosted tools")
        if kwargs.get("retry_rate_limits") is not False:
            raise AssertionError("Judgment allowed rate-limit retries")
        if kwargs.get("oauth_web_search") != "disabled":
            raise AssertionError("Judgment inherited hosted search")
        if "request_max_retries" in kwargs:
            raise AssertionError("Judgment overrode non-429 retry policy")
        if self.fail:
            error = RuntimeError("rate limit")
            error.status_code = 429
            raise error
        return SimpleNamespace(role="assistant", completion_text="yes")


class OAuthJudgmentPolicyTest(unittest.TestCase):
    def test_quota_failure_notice_does_not_call_another_model(self):
        tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
        method = copy.deepcopy(next(n for n in ast.walk(tree)
            if isinstance(n, ast.AsyncFunctionDef) and n.name == "_build_persona_llm_failure_reply"))
        method.decorator_list = []
        module = ast.fix_missing_locations(ast.Module(body=[
            ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
            method], type_ignores=[]))
        namespace = {"DEFAULT_PERSONA_FAILURE_REPLY": "暂时无法回复"}
        exec(compile(module, "main.py", "exec"), namespace)
        for reason in ("provider_rate_limit", "provider_quota"):
            with self.subTest(reason=reason):
                self.assertEqual(asyncio.run(namespace[method.name](object(), object(), reason)), "暂时无法回复")

    def test_both_decision_requests_disable_hosted_tools_and_retries(self):
        tree = ast.parse((ROOT / "utils/decision_ai.py").read_text(encoding="utf-8"))
        for name in ("call_decision_ai", "_call_ai"):
            with self.subTest(name=name):
                method = next(
                    n for n in ast.walk(tree)
                    if isinstance(n, ast.AsyncFunctionDef) and n.name == name
                    and not n.args.args
                )
                provider = HostedSearchProvider()
                namespace = dict(provider=provider, full_prompt="judge", prompt="judge",
                                 image_urls=[], persona_prompt="persona")
                module = ast.fix_missing_locations(ast.Module(
                    body=[copy.deepcopy(method)], type_ignores=[]))
                exec(compile(module, "decision_ai.py", "exec"), namespace)
                self.assertEqual(asyncio.run(namespace[name]()), "yes")
                self.assertEqual(len(provider.calls), 1)

    def test_final_gate_rate_limit_does_not_recover_with_fallback(self):
        harness = selection_tests.ProviderSelectionCompatibilityTest()
        harness.setUpClass()
        primary = HostedSearchProvider(fail=True)
        fallback = HostedSearchProvider()
        method = harness._build_harness(lambda event, context: primary)
        method.__globals__["_get_fallback_chat_providers"] = lambda *args: [fallback]
        event, request = harness._event_and_request()
        context = SimpleNamespace(get_config=lambda origin: {"provider_settings": {}})
        with self.assertRaisesRegex(RuntimeError, "rate limit"):
            asyncio.run(method(event, context, request))
        self.assertEqual(len(primary.calls), 1)
        self.assertEqual(fallback.calls, [])

    def test_final_gate_normal_response_still_succeeds(self):
        harness = selection_tests.ProviderSelectionCompatibilityTest()
        harness.setUpClass()
        provider = HostedSearchProvider()
        response, _, _, _ = harness._run_case(lambda *args: provider)
        self.assertEqual(response.completion_text, "yes")

    def test_structured_rate_limit_response_does_not_use_fallback(self):
        harness = selection_tests.ProviderSelectionCompatibilityTest()
        harness.setUpClass()
        primary = HostedSearchProvider()
        fallback = HostedSearchProvider()

        async def limited(**kwargs):
            return SimpleNamespace(role="err", completion_text="limited", status_code=429)

        primary.text_chat = limited
        method = harness._build_harness(lambda *args: primary)
        method.__globals__["_get_fallback_chat_providers"] = lambda *args: [fallback]
        event, request = harness._event_and_request()
        context = SimpleNamespace(get_config=lambda origin: {"provider_settings": {}})
        response, _, _, _ = asyncio.run(method(event, context, request))
        self.assertEqual(response.status_code, 429)
        self.assertEqual(fallback.calls, [])

    def test_other_errors_can_still_use_fallback(self):
        harness = selection_tests.ProviderSelectionCompatibilityTest()
        harness.setUpClass()
        primary = HostedSearchProvider()
        fallback = HostedSearchProvider()

        async def unavailable(**kwargs):
            error = RuntimeError("temporarily unavailable")
            error.status_code = 503
            raise error

        primary.text_chat = unavailable
        method = harness._build_harness(lambda *args: primary)
        method.__globals__["_get_fallback_chat_providers"] = lambda *args: [fallback]
        event, request = harness._event_and_request()
        context = SimpleNamespace(get_config=lambda origin: {"provider_settings": {}})
        response, _, _, _ = asyncio.run(method(event, context, request))
        self.assertEqual(response.completion_text, "yes")
        self.assertEqual(len(fallback.calls), 1)


if __name__ == "__main__":
    unittest.main()
