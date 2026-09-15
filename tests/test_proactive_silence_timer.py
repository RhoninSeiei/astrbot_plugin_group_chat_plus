import ast
import asyncio
import copy
from pathlib import Path
from types import SimpleNamespace
from typing import Tuple
import unittest


SOURCE = Path(__file__).resolve().parents[1] / "utils/proactive_chat_manager.py"


def methods():
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "ProactiveChatManager")
    return {n.name: n for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


class ProactiveSilenceTimerTest(unittest.IsolatedAsyncioTestCase):
    async def run_checks(self, rolls, *, silence=5400, last_reply=0, activity=True, probability=0.0675, cooldown=False):
        clock = SimpleNamespace(now=10000)
        random_values = iter(rolls)
        state = {"last_bot_reply_time": last_reply, "proactive_attempts_count": 0,
                 "user_message_count": 3, "user_message_timestamps": [10000, 10000, 10000], "cooldown_until": 20000}
        errors = []
        selected = methods()
        nodes = [copy.deepcopy(selected[n]) for n in ("_background_check_loop", "should_trigger_proactive_chat")]
        for node in nodes:
            node.decorator_list = []
        namespace = {"Context": object, "Tuple": Tuple, "time": SimpleNamespace(time=lambda: clock.now),
                     "random": SimpleNamespace(random=lambda: next(random_values)),
                     "logger": SimpleNamespace(info=lambda *a, **kw: None, error=lambda *a, **kw: errors.append(a))}
        checks = []
        class Harness:
            _is_running = True
            _debug_mode = False
            _proactive_check_interval = 120
            _proactive_temp_boost_duration = 120
            _chat_states = {"group": state}
            _temp_probability_boost = {}
            _proactive_require_user_activity = True
            _proactive_min_user_messages = 3
            _proactive_probability = probability
            _proactive_probability_scale = 1
            _enable_adaptive_proactive = False
            _enable_proactive_retry_sequence = False
            get_chat_state = classmethod(lambda cls, key: state)
            is_group_enabled = classmethod(lambda cls, key: True)
            is_in_cooldown = classmethod(lambda cls, key: cooldown)
            check_user_activity = classmethod(lambda cls, key: activity)
            calculate_effective_probability = classmethod(lambda cls, value, config=None: value)
            calculate_adaptive_parameters = classmethod(lambda cls, key: {
                "max_failures": 1, "cooldown_duration": 1800,
                "silence_threshold": silence, "prob_multiplier": 1,
            })
            _save_states_to_disk = classmethod(lambda cls: None)
            apply_score_decay = classmethod(lambda cls: None)
            apply_complaint_decay = classmethod(lambda cls: None)
            @classmethod
            async def trigger_proactive_chat(cls, context, config, plugin, key):
                checks.append(clock.now)
                # A successful send remains the event that starts a new silence interval.
                state["last_bot_reply_time"] = clock.now
        ticks = 0
        async def sleep(interval):
            nonlocal ticks
            ticks += 1
            clock.now += interval
            if ticks == len(rolls):
                Harness._is_running = False
        namespace["asyncio"] = SimpleNamespace(sleep=sleep, CancelledError=asyncio.CancelledError)
        module = ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[]))
        exec(compile(module, str(SOURCE), "exec"), namespace)
        for node in nodes:
            setattr(Harness, node.name, classmethod(namespace[node.name]))
        await Harness._background_check_loop(None, {}, None)
        self.assertEqual(errors, [])
        return checks, state

    async def test_probability_miss_can_succeed_next_check_without_new_silence_wait(self):
        sent, state = await self.run_checks([0.9, 0.01])
        self.assertEqual(sent, [10240])
        self.assertEqual(state["last_bot_reply_time"], 10240)

    async def test_repeated_misses_do_not_forge_a_bot_reply(self):
        sent, state = await self.run_checks([0.9, 0.8, 0.7])
        self.assertEqual(sent, [])
        self.assertEqual(state["last_bot_reply_time"], 0)

    async def test_real_reply_still_requires_new_silence_interval(self):
        sent, state = await self.run_checks([0.01, 0.01])
        self.assertEqual(sent, [10120])

    async def test_user_activity_requirement_is_preserved(self):
        sent, state = await self.run_checks([0.01, 0.01], activity=False)
        self.assertEqual(sent, [])
        self.assertEqual(state["last_bot_reply_time"], 0)

    async def test_disabled_time_period_does_not_send_or_reset_timer(self):
        sent, state = await self.run_checks([0.01, 0.01], probability=0)
        self.assertEqual(sent, [])
        self.assertEqual(state["last_bot_reply_time"], 0)

    async def test_cooldown_does_not_send_or_reset_timer(self):
        sent, state = await self.run_checks([0.01, 0.01], cooldown=True)
        self.assertEqual(sent, [])
        self.assertEqual(state["last_bot_reply_time"], 0)

    async def test_ai_veto_keeps_real_last_reply_time(self):
        trigger = methods()["_process_proactive_chat_simplified"]
        branch = next(n for n in ast.walk(trigger) if isinstance(n, ast.If)
                      and isinstance(n.test, ast.UnaryOp) and isinstance(n.test.op, ast.Not)
                      and isinstance(n.test.operand, ast.Name) and n.test.operand.id == "judge_pass")
        function = ast.parse("def veto(cls, chat_key, judge_pass):\n    pass\n").body[0]
        function.body = [copy.deepcopy(branch)]
        namespace = {"time": SimpleNamespace(time=lambda: 10000), "logger": SimpleNamespace(info=lambda *a: None)}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[])), str(SOURCE), "exec"), namespace)
        state = {"last_bot_reply_time": 1234}
        cls = SimpleNamespace(get_chat_state=lambda key: state)
        namespace["veto"](cls, "group", False)
        self.assertEqual(state["last_bot_reply_time"], 1234)


if __name__ == "__main__":
    unittest.main()
