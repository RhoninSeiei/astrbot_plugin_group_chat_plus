import importlib.util
import pathlib
import sys
import types
import unittest


REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _load_ai_response_filter():
    astrbot_module = types.ModuleType("astrbot")
    astrbot_api_module = types.ModuleType("astrbot.api")
    astrbot_api_module.logger = types.SimpleNamespace(
        info=lambda *_args, **_kwargs: None,
        warning=lambda *_args, **_kwargs: None,
        error=lambda *_args, **_kwargs: None,
    )
    sys.modules.setdefault("astrbot", astrbot_module)
    sys.modules.setdefault("astrbot.api", astrbot_api_module)

    module_path = REPO_ROOT / "utils" / "ai_response_filter.py"
    spec = importlib.util.spec_from_file_location("ai_response_filter_test", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DecisionAIConfigTest(unittest.TestCase):
    def setUp(self):
        self.schema = (REPO_ROOT / "_conf_schema.json").read_text(encoding="utf-8")
        self.main_source = (REPO_ROOT / "main.py").read_text(encoding="utf-8")
        self.decision_source = (REPO_ROOT / "utils" / "decision_ai.py").read_text(
            encoding="utf-8"
        )
        self.proactive_source = (
            REPO_ROOT / "utils" / "proactive_chat_manager.py"
        ).read_text(encoding="utf-8")

    def test_schema_exposes_judgment_persona_and_reasoning_settings(self):
        for key in (
            "decision_ai_include_persona",
            "decision_ai_persona_name",
            "enable_decision_ai_reasoning",
            "decision_ai_reasoning_log",
            "decision_ai_reasoning_log_mode",
            "judgment_reasoning_start_marker",
            "judgment_reasoning_end_marker",
            "enable_main_model_final_decision",
            "proactive_ai_judge_include_persona",
            "proactive_ai_judge_persona_name",
            "enable_proactive_ai_reasoning",
            "proactive_ai_reasoning_log",
            "proactive_ai_reasoning_log_mode",
            "frequency_ai_include_persona",
            "frequency_ai_persona_name",
            "enable_frequency_ai_reasoning",
            "frequency_ai_reasoning_log",
            "frequency_ai_reasoning_log_mode",
        ):
            self.assertIn(f'"{key}"', self.schema)

    def test_main_passes_decision_ai_judgment_settings(self):
        for fragment in (
            "self.decision_ai_include_persona",
            "self.decision_ai_persona_name",
            "self.enable_decision_ai_reasoning",
            "reasoning_start_marker=self.judgment_reasoning_start_marker",
            "include_persona=self.decision_ai_include_persona",
            "configured_persona_name=self.decision_ai_persona_name",
            "frequency_ai_include_persona",
            "frequency_ai_persona_name",
            "enable_frequency_ai_reasoning",
        ):
            self.assertIn(fragment, self.main_source)

    def test_decision_ai_uses_session_provider_and_judgment_persona_resolver(self):
        self.assertIn("resolve_judgment_persona", self.decision_source)
        self.assertIn("get_session_provider(context, event=event)", self.decision_source)
        self.assertIn("_ensure_reasoning_protocol", self.decision_source)
        self.assertIn("parse_decision_response", self.decision_source)

    def test_proactive_judge_uses_matching_judgment_options(self):
        for fragment in (
            "_proactive_ai_judge_include_persona",
            "_proactive_ai_judge_persona_name",
            "_enable_proactive_ai_reasoning",
            "resolve_judgment_persona",
            "_ensure_reasoning_protocol",
            "parse_decision_response",
        ):
            self.assertIn(fragment, self.proactive_source)

    def test_frequency_judge_uses_persona_and_reasoning_options(self):
        frequency_source = (
            REPO_ROOT / "utils" / "frequency_adjuster.py"
        ).read_text(encoding="utf-8")
        for fragment in (
            "frequency_ai_include_persona",
            "frequency_ai_persona_name",
            "enable_frequency_ai_reasoning",
            "DecisionAI._build_reasoning_protocol",
            "include_persona=self.frequency_ai_include_persona",
            "configured_persona_name=self.frequency_ai_persona_name",
            "context_label=\"频率动态调整器\"",
            "parse_frequency_response",
            "log_reasoning_output",
        ):
            self.assertIn(fragment, frequency_source)

    def test_custom_reasoning_protocol_keeps_final_answer(self):
        response_filter = _load_ai_response_filter().AIResponseFilter
        parsed = response_filter.parse_decision_response(
            "<gcp_reason>\n上下文里有人明确提问。\n</gcp_reason>\nyes",
            "<gcp_reason>",
            "</gcp_reason>",
        )

        self.assertTrue(parsed["parse_success"])
        self.assertEqual(parsed["normalized_answer"], "yes")
        self.assertIn("明确提问", parsed["reasoning_text"])
        self.assertTrue(parsed["protocol_followed"])


if __name__ == "__main__":
    unittest.main()
