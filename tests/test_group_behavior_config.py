import pathlib
import unittest


REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


class GroupBehaviorConfigTest(unittest.TestCase):
    def setUp(self):
        self.schema = (REPO_ROOT / "_conf_schema.json").read_text(encoding="utf-8")
        self.main_source = (REPO_ROOT / "main.py").read_text(encoding="utf-8")
        self.attention_source = (REPO_ROOT / "utils" / "attention_manager.py").read_text(
            encoding="utf-8"
        )
        self.cache_source = (
            REPO_ROOT / "utils" / "message_cache_manager.py"
        ).read_text(encoding="utf-8")
        self.context_source = (REPO_ROOT / "utils" / "context_manager.py").read_text(
            encoding="utf-8"
        )

    def test_schema_exposes_group_behavior_controls(self):
        for key in (
            "enable_idle_cache_flush",
            "idle_cache_flush_delay_seconds",
            "group_wait_window_at_mode",
            "group_wait_window_keyword_mode",
            "group_wait_window_poke_mode",
            "enable_pending_attention_cooldown",
            "pending_cooldown_grace_user_messages",
            "pending_cooldown_max_wait_seconds",
            "pending_cooldown_same_user_probability_floor",
        ):
            self.assertIn(f'"{key}"', self.schema)

    def test_main_wires_group_behavior_controls(self):
        for fragment in (
            "self.enable_idle_cache_flush",
            "self._idle_flush_tasks",
            "_reset_idle_flush_timer",
            "_idle_flush_worker",
            "flush_cached_messages_by_params",
            "self.group_wait_window_at_mode",
            "self.group_wait_window_keyword_mode",
            "self.group_wait_window_poke_mode",
            "_collect_pending_cooldown_context",
            "pending_cooldown_context=pending_cooldown_context",
            "transient_probability_boost=at_all_transient_probability_boost",
        ):
            self.assertIn(fragment, self.main_source)

    def test_support_modules_expose_required_hooks(self):
        for fragment in (
            "pending_probability_floor",
            "is_in_pending_cooldown",
            "PENDING_COOLDOWN_SAME_USER_PROBABILITY_FLOOR",
        ):
            self.assertIn(fragment, self.attention_source)

        self.assertIn("convert_window_buffered_to_regular", self.cache_source)
        self.assertIn("flush_cached_messages_by_params", self.context_source)


if __name__ == "__main__":
    unittest.main()
