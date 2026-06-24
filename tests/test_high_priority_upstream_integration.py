from pathlib import Path
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]


class HighPriorityUpstreamIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.main_source = (REPO_ROOT / "main.py").read_text(encoding="utf-8")
        self.cleaner_source = (REPO_ROOT / "utils" / "message_cleaner.py").read_text(
            encoding="utf-8"
        )
        self.processor_source = (
            REPO_ROOT / "utils" / "message_processor.py"
        ).read_text(encoding="utf-8")
        self.cache_source = (
            REPO_ROOT / "utils" / "message_cache_manager.py"
        ).read_text(encoding="utf-8")
        self.server_source = (REPO_ROOT / "legacy" / "web" / "server.py").read_text(
            encoding="utf-8"
        )
        self.api_source = (
            REPO_ROOT / "legacy" / "web" / "static" / "js" / "api.js"
        ).read_text(encoding="utf-8")
        self.session_mgr_source = (
            REPO_ROOT / "legacy" / "web" / "static" / "js" / "session-mgr.js"
        ).read_text(encoding="utf-8")

    def test_main_has_poke_persistence_and_smart_batch_fallback(self):
        self.assertIn("def _append_persistent_event_text", self.main_source)
        self.assertIn("async def _save_poke_assistant_event", self.main_source)
        self.assertIn("persistent_poke_event_text", self.main_source)
        self.assertIn("AI决策过滤-smart-batch", self.main_source)
        self.assertIn("smart_merged", self.main_source)

    def test_empty_at_uses_structured_mention_modes(self):
        self.assertIn("mode: str = \"only_ai\"", self.cleaner_source)
        self.assertIn("contains_ai", self.cleaner_source)
        self.assertIn("mention_info=mention_info", self.main_source)
        self.assertIn("mode=\"contains_ai\"", self.main_source)
        self.assertIn("mode=\"only_ai\"", self.main_source)

    def test_poke_and_smart_cache_helpers_are_integrated(self):
        self.assertIn("build_persistent_poke_event_text", self.processor_source)
        self.assertIn("format_message_for_context_display", self.processor_source)
        self.assertIn("_append_persistent_poke_event_text", self.cache_source)
        self.assertIn("[Smart合并]", self.cache_source)

    def test_empty_message_filter_and_web_cleanup_are_registered(self):
        self.assertIn("真空消息", self.main_source)
        self.assertIn("/api/session/clean-ghosts", self.server_source)
        self.assertIn("async def _handle_clean_ghost_sessions", self.server_source)
        self.assertIn("sessionCleanGhosts", self.api_source)
        self.assertIn("_cleanupGhostSessions", self.session_mgr_source)


if __name__ == "__main__":
    unittest.main()
