import importlib.util
from pathlib import Path
import sys
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_runtime_state():
    module_path = REPO_ROOT / "utils" / "runtime_state.py"
    spec = importlib.util.spec_from_file_location("runtime_state_test", module_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.RuntimeState


class RuntimeStateTest(unittest.TestCase):
    def test_instances_do_not_share_mutable_state(self):
        runtime_state = _load_runtime_state()
        first = runtime_state()
        second = runtime_state()

        first.processing_sessions["m1"] = "g1"
        first.pending_bot_replies["m1"] = ["reply"]
        first.agent_done_flags.add("m1")

        self.assertEqual(second.processing_sessions, {})
        self.assertEqual(second.pending_bot_replies, {})
        self.assertEqual(second.agent_done_flags, set())

    def test_clear_message_removes_per_message_runtime_entries(self):
        runtime_state = _load_runtime_state()
        state = runtime_state()

        state.processing_sessions["m1"] = "g1"
        state.message_cache_snapshots["m1"] = {"content": "message"}
        state.smart_batch_snapshots["m1"] = [{"content": "merged message"}]
        state.pending_bot_replies["m1"] = ["reply"]
        state.agent_done_flags.add("m1")
        state.duplicate_blocked_messages["m1"] = True
        state.raw_reply_cache["m1"] = "raw reply"
        state.saved_messages["m1"] = 1.0

        state.clear_message("m1")

        self.assertNotIn("m1", state.processing_sessions)
        self.assertNotIn("m1", state.message_cache_snapshots)
        self.assertNotIn("m1", state.smart_batch_snapshots)
        self.assertNotIn("m1", state.pending_bot_replies)
        self.assertNotIn("m1", state.agent_done_flags)
        self.assertNotIn("m1", state.duplicate_blocked_messages)
        self.assertNotIn("m1", state.raw_reply_cache)
        self.assertNotIn("m1", state.saved_messages)

    def test_clear_all_removes_every_runtime_container(self):
        runtime_state = _load_runtime_state()
        state = runtime_state()

        state.processing_sessions["m1"] = "g1"
        state.proactive_processing_sessions["g1"] = 1.0
        state.message_cache_snapshots["m1"] = {"content": "message"}
        state.smart_batch_snapshots["m1"] = [{"content": "merged message"}]
        state.pending_bot_replies["m1"] = ["reply"]
        state.agent_done_flags.add("m1")
        state.duplicate_blocked_messages["m1"] = True
        state.saved_messages["m1"] = 1.0
        state.seen_message_ids["m1"] = 1.0
        state.command_messages["m1"] = 1.0
        state.recent_replies_cache["g1"] = [{"content": "reply"}]
        state.raw_reply_cache["m1"] = "raw reply"

        state.clear_all()

        self.assertEqual(state.processing_sessions, {})
        self.assertEqual(state.proactive_processing_sessions, {})
        self.assertEqual(state.message_cache_snapshots, {})
        self.assertEqual(state.smart_batch_snapshots, {})
        self.assertEqual(state.pending_bot_replies, {})
        self.assertEqual(state.agent_done_flags, set())
        self.assertEqual(state.duplicate_blocked_messages, {})
        self.assertEqual(state.saved_messages, {})
        self.assertEqual(state.seen_message_ids, {})
        self.assertEqual(state.command_messages, {})
        self.assertEqual(state.recent_replies_cache, {})
        self.assertEqual(state.raw_reply_cache, {})

    def test_main_initializes_runtime_state_and_keeps_legacy_aliases(self):
        main_source = (REPO_ROOT / "main.py").read_text(encoding="utf-8")

        self.assertIn("RuntimeState", main_source)
        self.assertIn("self.runtime_state = RuntimeState()", main_source)
        self.assertIn(
            "self.processing_sessions = self.runtime_state.processing_sessions",
            main_source,
        )
        self.assertIn(
            "self._message_cache_snapshots = self.runtime_state.message_cache_snapshots",
            main_source,
        )
        self.assertIn(
            "self._pending_bot_replies = self.runtime_state.pending_bot_replies",
            main_source,
        )

    def test_global_reset_clears_runtime_state_container(self):
        main_source = (REPO_ROOT / "main.py").read_text(encoding="utf-8")

        reset_pos = main_source.index("async def _reset_plugin_data_and_reload")
        counts_pos = main_source.index("runtime_counts = {", reset_pos)
        clear_pos = main_source.index("self.runtime_state.clear_all()", reset_pos)
        legacy_clear_pos = main_source.index("self.pending_messages_cache.clear()", reset_pos)

        self.assertLess(counts_pos, clear_pos)
        self.assertLess(clear_pos, legacy_clear_pos)
        self.assertIn("【插件重置】已清空 RuntimeState: %s", main_source)
        self.assertIn('"processing_sessions"', main_source)
        self.assertIn('"pending_bot_reply_segments"', main_source)
        self.assertIn('"recent_reply_items"', main_source)


if __name__ == "__main__":
    unittest.main()
