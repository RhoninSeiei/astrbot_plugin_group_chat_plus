from pathlib import Path
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]


class MainReplyDeclineCacheTest(unittest.TestCase):
    def test_main_model_decline_uses_current_message_cache(self):
        source = (REPO_ROOT / "main.py").read_text(encoding="utf-8")
        start = source.index("    async def _generate_and_send_reply")
        end = source.index("    async def _do_poke_after_reply")
        function_source = source[start:end]

        self.assertIn("declined_message_cache = current_message_cache", function_source)
        self.assertIn("source=\"主模型最终判断过滤\"", function_source)
        self.assertNotIn("if cached_message_data:", function_source)


if __name__ == "__main__":
    unittest.main()
