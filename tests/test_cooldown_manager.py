import asyncio
import importlib.util
import pathlib
import sys
import time
import types
import unittest


REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _load_cooldown_manager():
    astrbot_module = types.ModuleType("astrbot")
    astrbot_api_module = types.ModuleType("astrbot.api")
    astrbot_api_all_module = types.ModuleType("astrbot.api.all")
    logger = types.SimpleNamespace(
        info=lambda *_args, **_kwargs: None,
        warning=lambda *_args, **_kwargs: None,
        error=lambda *_args, **_kwargs: None,
    )
    astrbot_api_all_module.logger = logger
    sys.modules.setdefault("astrbot", astrbot_module)
    sys.modules.setdefault("astrbot.api", astrbot_api_module)
    sys.modules.setdefault("astrbot.api.all", astrbot_api_all_module)

    package = types.ModuleType("cooldown_utils")
    package.__path__ = [str(REPO_ROOT / "utils")]
    sys.modules.setdefault("cooldown_utils", package)

    module_path = REPO_ROOT / "utils" / "cooldown_manager.py"
    spec = importlib.util.spec_from_file_location(
        "cooldown_utils.cooldown_manager_test", module_path
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class CooldownManagerPendingTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        module = _load_cooldown_manager()
        self.manager = module.CooldownManager
        self.manager._cooldown_map = {}
        self.manager._pending_cooldown_map = {}
        self.manager._lock = asyncio.Lock()
        self.manager._initialized = True
        self.manager.ENABLE_PENDING_COOLDOWN = True
        self.manager.PENDING_COOLDOWN_GRACE_USER_MESSAGES = 1
        self.manager.PENDING_COOLDOWN_MAX_WAIT_SECONDS = 60
        self.manager.PENDING_COOLDOWN_SAME_USER_PROBABILITY_FLOOR = 0.18
        self.manager.ENABLE_AUTO_RELEASE = True

    async def test_pending_promotes_after_same_user_followup_no_reply(self):
        added = await self.manager.add_pending_cooldown(
            "group:1000",
            "user-1",
            "Alice",
            reason="decision_ai_no_reply",
            trigger_message_id="m1",
        )
        self.assertTrue(added)

        progress = await self.manager.consume_pending_by_same_user_message(
            "group:1000",
            "user-1",
            message_id="m2",
            message_timestamp=time.time(),
            is_at_ai=False,
            mention_other=False,
            has_trigger_keyword=False,
            is_empty_at=False,
        )
        self.assertTrue(progress["should_promote"])

        action = await self.manager.mark_pending_decision_result(
            "group:1000", "user-1", should_reply=False
        )
        self.assertEqual(action, "promote")

        promoted = await self.manager.promote_pending_to_active(
            "group:1000", "user-1", reason="same_user_followup_no_reply"
        )
        self.assertTrue(promoted)
        self.assertTrue(await self.manager.is_in_cooldown("group:1000", "user-1"))
        self.assertFalse(
            await self.manager.is_in_pending_cooldown("group:1000", "user-1")
        )

    async def test_reply_clears_pending_without_active_cooldown(self):
        await self.manager.add_pending_cooldown(
            "group:1000",
            "user-1",
            "Alice",
            reason="decision_ai_no_reply",
        )

        action = await self.manager.mark_pending_decision_result(
            "group:1000", "user-1", should_reply=True
        )
        self.assertEqual(action, "cancel")

        cleared = await self.manager.clear_pending_cooldown(
            "group:1000", "user-1", reason="reply_confirmed"
        )
        self.assertTrue(cleared)
        self.assertFalse(
            await self.manager.is_in_pending_cooldown("group:1000", "user-1")
        )
        self.assertFalse(await self.manager.is_in_cooldown("group:1000", "user-1"))


if __name__ == "__main__":
    unittest.main()
