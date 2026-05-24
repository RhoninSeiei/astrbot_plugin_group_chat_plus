import asyncio
import importlib.util
import pathlib
import sys
import time
import types
import unittest


REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _load_probability_manager():
    astrbot_module = types.ModuleType("astrbot")
    astrbot_api_module = types.ModuleType("astrbot.api")
    astrbot_api_all_module = types.ModuleType("astrbot.api.all")
    astrbot_api_all_module.logger = types.SimpleNamespace(
        info=lambda *_args, **_kwargs: None,
        warning=lambda *_args, **_kwargs: None,
        error=lambda *_args, **_kwargs: None,
    )
    sys.modules.setdefault("astrbot", astrbot_module)
    sys.modules.setdefault("astrbot.api", astrbot_api_module)
    sys.modules.setdefault("astrbot.api.all", astrbot_api_all_module)

    package = types.ModuleType("pm_utils")
    package.__path__ = [str(REPO_ROOT / "utils")]
    sys.modules.setdefault("pm_utils", package)
    module_path = REPO_ROOT / "utils" / "probability_manager.py"
    spec = importlib.util.spec_from_file_location(
        "pm_utils.probability_manager_test", module_path
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class ProbabilityManagerTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        module = _load_probability_manager()
        self.manager = module.ProbabilityManager
        self.manager._probability_status = {}
        self.manager._lock = asyncio.Lock()
        self.manager.initialize(
            {
                "enable_dynamic_reply_probability": False,
                "reply_time_periods": "[]",
                "reply_time_transition_minutes": 30,
                "reply_time_min_factor": 0.1,
                "reply_time_max_factor": 2.0,
                "reply_time_use_smooth_curve": True,
                "enable_probability_hard_limit": False,
                "probability_min_limit": 0.05,
                "probability_max_limit": 0.8,
            }
        )

    async def test_frequency_base_probability_preserves_reply_boost(self):
        await self.manager.boost_probability(
            "aiocqhttp",
            False,
            "1000",
            boosted_probability=0.8,
            duration=60,
        )
        await self.manager.set_base_probability(
            "aiocqhttp",
            False,
            "1000",
            new_probability=0.1,
            duration=60,
        )

        probability = await self.manager.get_current_probability(
            "aiocqhttp",
            False,
            "1000",
            initial_probability=0.02,
        )
        self.assertEqual(probability, 0.8)

        chat_key = self.manager.get_chat_key("aiocqhttp", False, "1000")
        self.manager._probability_status[chat_key]["reply_boost_until"] = time.time() - 1
        probability_after_boost = await self.manager.get_current_probability(
            "aiocqhttp",
            False,
            "1000",
            initial_probability=0.02,
        )
        self.assertEqual(probability_after_boost, 0.1)


if __name__ == "__main__":
    unittest.main()
