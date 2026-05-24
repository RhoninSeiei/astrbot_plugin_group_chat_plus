import importlib.util
import pathlib
import sys
import unittest


REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _load_rewriter():
    module_path = REPO_ROOT / "utils" / "system_prompt_rewriter.py"
    spec = importlib.util.spec_from_file_location("system_prompt_rewriter_test", module_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.SystemPromptRewriter


class SystemPromptRewriterTest(unittest.TestCase):
    def test_exact_match_preserves_other_plugin_prefix_and_suffix(self):
        rewriter = _load_rewriter()

        result = rewriter.rewrite(
            "prefix plugin\npersona body\nsuffix plugin",
            "persona body",
        )

        self.assertEqual(
            result.merged_system_prompt,
            "prefix plugin\npersona body\nsuffix plugin",
        )
        self.assertEqual(result.strategy, "exact-match")
        self.assertTrue(result.preserved_order)

    def test_strips_known_platform_ltm_from_prefix(self):
        rewriter = _load_rewriter()
        ltm = (
            "You are now in a chatroom. The chat history is as follows:\n"
            "[Alice/12:00:00]: hello\n---\n[Bob/12:00:01]: hi\n"
        )

        result = rewriter.rewrite(ltm + "\npersona body", "persona body")

        self.assertEqual(result.merged_system_prompt, "persona body")
        self.assertTrue(result.ltm_detected)

    def test_wrapped_persona_header_match(self):
        rewriter = _load_rewriter()

        result = rewriter.rewrite(
            "other\n# Persona Instructions\n\npersona\n\nbody\n",
            "persona\nbody",
        )

        self.assertEqual(result.strategy, "wrapped-persona-match")
        self.assertIn("other", result.merged_system_prompt)
        self.assertIn("persona\nbody", result.merged_system_prompt)

    def test_conservative_fallback_prepends_plugin_persona(self):
        rewriter = _load_rewriter()

        result = rewriter.rewrite("unknown platform prompt", "persona body")

        self.assertEqual(result.strategy, "conservative-prepend-plugin")
        self.assertTrue(result.merged_system_prompt.startswith("persona body"))
        self.assertIn("unknown platform prompt", result.merged_system_prompt)

    def test_main_uses_system_prompt_rewriter_without_tool_merge(self):
        main_source = (REPO_ROOT / "main.py").read_text(encoding="utf-8")

        self.assertIn("SystemPromptRewriter", main_source)
        self.assertIn("rewrite_preserving_plugin_base", main_source)
        self.assertIn("req.func_tool = plugin_tool_set", main_source)
        self.assertNotIn("merge_tool", main_source)


if __name__ == "__main__":
    unittest.main()
