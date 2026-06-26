import asyncio
import importlib.util
import pathlib
import sys
import types
import unittest


REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


class CountingAsyncLock:
    def __init__(self):
        self.enter_count = 0

    async def __aenter__(self):
        self.enter_count += 1
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


def _load_smart_manager():
    astrbot_module = sys.modules.get("astrbot") or types.ModuleType("astrbot")
    astrbot_api_module = sys.modules.get("astrbot.api") or types.ModuleType("astrbot.api")
    astrbot_api_module.logger = types.SimpleNamespace(
        info=lambda *_args, **_kwargs: None,
        warning=lambda *_args, **_kwargs: None,
        error=lambda *_args, **_kwargs: None,
    )
    sys.modules["astrbot"] = astrbot_module
    sys.modules["astrbot.api"] = astrbot_api_module

    module_path = REPO_ROOT / "utils" / "smart_concurrent_manager.py"
    spec = importlib.util.spec_from_file_location("smart_concurrent_manager_test", module_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.SmartConcurrentManager


class SmartConcurrentManagerTest(unittest.TestCase):
    def setUp(self):
        self.manager_class = _load_smart_manager()
        self.manager = self.manager_class()

    def test_anchor_claims_ready_followers_by_arrival_order(self):
        async def scenario():
            await self.manager.register_arrival("g1", "m1", arrival_seq=1)
            await self.manager.register_arrival("g1", "m2", arrival_seq=2)
            await self.manager.attach_payload("g1", "m1", "第一条", "A", "1", {"message_id": "m1"})
            await self.manager.attach_payload("g1", "m2", "第二条", "B", "2", {"message_id": "m2"})

            claim = await self.manager.claim_batch("g1", "m1")

            self.assertTrue(claim["is_anchor"])
            self.assertEqual(
                [entry["processing_id"] for entry in claim["merged_entries"]],
                ["m2"],
            )
            self.assertTrue(await self.manager.is_consumed("m2"))

        asyncio.run(scenario())

    def test_later_message_waits_for_earlier_pending(self):
        async def scenario():
            await self.manager.register_arrival("g1", "m1", arrival_seq=1)
            await self.manager.register_arrival("g1", "m2", arrival_seq=2)

            self.assertTrue(await self.manager.has_earlier_pending("g1", "m2"))
            self.assertFalse(await self.manager.has_earlier_pending("g1", "m1"))

        asyncio.run(scenario())

    def test_forced_follower_is_batch_boundary(self):
        async def scenario():
            await self.manager.register_arrival("g1", "m1", arrival_seq=1)
            await self.manager.register_arrival("g1", "m2", arrival_seq=2)
            await self.manager.attach_payload("g1", "m1", "第一条", "A", "1", {"message_id": "m1"})
            await self.manager.attach_payload(
                "g1", "m2", "第二条", "B", "2", {"message_id": "m2"}, is_forced=True
            )

            claim = await self.manager.claim_batch("g1", "m1")

            self.assertEqual(claim["merged_entries"], [])
            self.assertFalse(await self.manager.is_consumed("m2"))

        asyncio.run(scenario())

    def test_instances_keep_pending_and_consumed_state_isolated(self):
        async def scenario():
            first = self.manager_class(expire_seconds=30.0, max_batch_size=1)
            second = self.manager_class(expire_seconds=5.0, max_batch_size=10)

            await first.register_arrival("g1", "m1", arrival_seq=1)
            await first.register_arrival("g1", "m2", arrival_seq=2)
            await first.attach_payload("g1", "m1", "第一条", "A", "1", {"message_id": "m1"})
            await first.attach_payload("g1", "m2", "第二条", "B", "2", {"message_id": "m2"})

            second_claim = await second.claim_batch("g1", "m1")
            first_claim = await first.claim_batch("g1", "m1")

            self.assertTrue(second_claim["is_missing"])
            self.assertTrue(first_claim["is_anchor"])
            self.assertEqual(
                [entry["processing_id"] for entry in first_claim["merged_entries"]],
                ["m2"],
            )
            self.assertFalse(await second.is_consumed("m2"))
            self.assertTrue(await first.is_consumed("m2"))

        asyncio.run(scenario())

    def test_consumed_reads_use_manager_lock(self):
        async def scenario():
            counting_lock = CountingAsyncLock()
            self.manager._lock = counting_lock
            self.manager._consumed["m2"] = {
                "consumed_at": 1.0,
                "anchor_processing_id": "m1",
            }

            self.assertTrue(await self.manager.is_consumed("m2"))
            self.assertEqual(await self.manager.get_consumer("m2"), "m1")
            self.assertEqual(counting_lock.enter_count, 2)

        asyncio.run(scenario())

    def test_main_exposes_smart_concurrent_switch(self):
        main_source = (REPO_ROOT / "main.py").read_text(encoding="utf-8")
        schema = (REPO_ROOT / "_conf_schema.json").read_text(encoding="utf-8")

        self.assertIn("SmartConcurrentManager", main_source)
        self.assertIn("self.smart_concurrent = SmartConcurrentManager", main_source)
        self.assertNotIn("SmartConcurrentManager._EXPIRE_SECONDS", main_source)
        self.assertNotIn("SmartConcurrentManager._MAX_BATCH_SIZE", main_source)
        self.assertIn("concurrent_mode", main_source)
        self.assertIn('"concurrent_mode"', schema)
        self.assertIn("smart_batch_dynamic_hint", main_source)


if __name__ == "__main__":
    unittest.main()
