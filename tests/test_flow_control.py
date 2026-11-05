import asyncio
import ast
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import unittest


def load_flow():
    path = Path(__file__).parents[1] / "utils/flow_control.py"
    spec = importlib.util.spec_from_file_location("gcp_flow_control_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Clock:
    def __init__(self):
        self.tick = 0.0

    def __call__(self):
        return self.tick


class Event:
    unified_msg_origin = "default:GroupMessage:905956314"

    def __init__(self):
        self.extras = {}
        self.sent = []

    def get_extra(self, key, default=None):
        return self.extras.get(key, default)

    def set_extra(self, key, value):
        self.extras[key] = value

    def is_private_chat(self):
        return False

    async def send(self, *args, **kwargs):
        self.sent.append((args, kwargs))
        return "sdk-result"

    async def send_streaming(self, *args, **kwargs):
        return "stream-result"


def chain(text="natural progress"):
    return SimpleNamespace(chain=[SimpleNamespace(text=text)])


class FlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.flow = load_flow()
        self.clock = Clock()
        self.logs = []
        self.config = {"flow_optimization_sessions": [Event.unified_msg_origin]}
        self.controller = self.flow.FlowController(self.config, self.logs.append, self.clock)
        self.event = Event()

    def begin(self):
        trace = self.controller.start(self.event)
        self.controller.formal_request(self.event)
        return trace

    async def test_opt_in_uses_exact_umo_and_leaves_other_events_untouched(self):
        other = Event()
        other.unified_msg_origin = "other:GroupMessage:905956314"
        self.assertIsNone(self.controller.start(other))
        self.assertNotIn("send", vars(other))
        self.assertIsNone(self.flow.FlowController({}).start(self.event))

    async def test_window_closes_on_silence_and_bursts_never_extend_hard_limit(self):
        window = self.controller.new_window(self.event, 3000)
        self.assertEqual(window.deadline, 1.0)
        self.clock.tick = .2
        window.note_message()
        self.assertAlmostEqual(window.deadline, .8)
        for tick in [0.7, 1.2, 1.7, 2.2, 2.7, 2.95]:
            self.clock.tick = tick
            window.note_message()
            self.assertLessEqual(window.deadline, 3.0)
        self.clock.tick = 3.0
        self.assertTrue(window.expired())

    async def test_window_configuration_is_bounded_and_malformed_values_fall_back(self):
        for value, expected in [(None, 1), ("invalid", 1), (-1, .6), (100000, 1.6)]:
            policy = self.flow.AdaptiveWindow(30000, value, self.clock)
            self.assertEqual(policy.deadline, expected)
            self.assertEqual(policy.hard_deadline, 3)

    async def test_empty_submission_and_hook_only_fallback_do_not_claim_sdk_success(self):
        class LockedEvent(Event):
            def __setattr__(self, name, value):
                if name in {"send", "send_streaming"}:
                    raise AttributeError("read only")
                super().__setattr__(name, value)
        self.event = LockedEvent()
        trace = self.begin()
        await self.event.send(chain(""))
        self.controller.send_hook(self.event)
        self.assertEqual(trace.send_count, 0)
        self.assertTrue(any(row["outcome"] == "hook_only" for row in self.logs))
        self.assertNotIn("first_sdk_submit", [row["stage"] for row in self.logs])
        self.controller.finish(self.event, trace)

    async def test_successful_send_forwards_all_arguments_and_does_not_claim_receipt(self):
        trace = self.begin()
        self.clock.tick = 2.5
        payload = chain()
        self.assertEqual(await self.event.send(payload, flag="untouched"), "sdk-result")
        self.assertEqual(self.event.sent, [((payload,), {"flag": "untouched"})])
        self.assertEqual(trace.send_count, 1)
        self.assertNotIn("final_sdk_submit", [row["stage"] for row in self.logs])
        self.controller.agent_done(self.event)
        self.clock.tick = 3.5
        await self.event.send(chain("final answer"))
        self.controller.finish(self.event, trace)
        self.assertNotIn("send", vars(self.event))
        self.assertIn("first_sdk_submit", [row["stage"] for row in self.logs])
        self.assertIn("final_sdk_submit", [row["stage"] for row in self.logs])
        self.assertNotIn("natural progress", json.dumps(self.logs))
        self.assertNotIn("905956314", json.dumps(self.logs))

    async def test_send_failure_is_reraised_and_not_counted_as_success(self):
        async def fail(*args, **kwargs):
            raise OSError("secret token=PRIVATE")
        self.event.send = fail
        trace = self.begin()
        with self.assertRaises(OSError):
            await self.event.send(chain())
        self.assertEqual(trace.send_count, 0)
        self.controller.finish(self.event, trace)
        self.assertIs(self.event.send, fail)
        self.assertNotIn("PRIVATE", json.dumps(self.logs))

    async def test_only_confirmed_intermediate_progress_is_deduplicated(self):
        self.begin()
        result = SimpleNamespace(is_llm_result=lambda: True, async_stream=None)
        self.assertTrue(self.controller.keep_progress(self.event, result))
        self.assertFalse(self.controller.repeat_progress(self.event, "checking", result))
        self.controller.prepare_progress(self.event, "checking", result)
        await self.event.send(chain("checking"))
        self.assertTrue(self.controller.repeat_progress(self.event, "checking", result))
        self.controller.agent_done(self.event)
        self.assertFalse(self.controller.repeat_progress(self.event, "checking", result))
        self.assertTrue(self.controller.final_matches_progress(self.event, "checking"))

    async def test_streaming_is_not_whole_text_deduplicated_or_called_first_token(self):
        trace = self.begin()
        result = SimpleNamespace(is_llm_result=lambda: True, async_stream=object())
        self.assertFalse(self.controller.keep_progress(self.event, result))
        self.assertEqual(await self.event.send_streaming(object(), realtime=True), "stream-result")
        self.assertEqual(trace.send_count, 0)
        self.assertIn("stream_submit_completed", [row["stage"] for row in self.logs])
        self.assertNotIn("first_sdk_submit", [row["stage"] for row in self.logs])

    async def test_tool_pairing_ignores_arguments_and_reports_missing_end(self):
        trace = self.begin()
        tool = object()
        self.controller.tool_start(self.event, tool)
        self.clock.tick = 1
        self.controller.tool_end(self.event, tool, SimpleNamespace(isError=False))
        self.controller.tool_start(self.event, object())
        self.controller.agent_done(self.event)
        ends = [row for row in self.logs if row["stage"] == "tool_end"]
        self.assertEqual([row["outcome"] for row in ends], ["ok", "unobserved_end"])
        self.assertEqual(ends[0]["duration_ms"], 1000)
        self.assertEqual(trace.tools, [])

    async def test_old_generation_and_event_reuse_cannot_clear_new_trace(self):
        old = self.begin()
        old_wrapper = self.event.send
        newer = self.flow.FlowController(self.config, self.logs.append, self.clock)
        current = newer.start(self.event)
        newer.formal_request(self.event)
        self.controller.finish(self.event, old)
        self.assertIs(newer.trace(self.event), current)
        self.assertIsNone(self.controller.trace(self.event))
        await self.event.send(chain())
        self.assertEqual(current.send_count, 1)
        self.assertEqual(old.send_count, 0)
        newer.finish(self.event, current)
        await self.event.send(chain("after cleanup"))
        self.assertEqual(current.send_count, 1)
        self.assertNotIn("send", vars(self.event))

    async def test_new_module_generation_retires_old_active_trace_before_wrapping(self):
        old = self.begin()
        reloaded_module = load_flow()
        newer = reloaded_module.FlowController(self.config, self.logs.append, self.clock)
        current = newer.start(self.event)
        newer.formal_request(self.event)
        await self.event.send(chain())
        self.assertTrue(old.finished)
        self.assertEqual(old.send_count, 0)
        self.assertEqual(current.send_count, 1)
        self.assertEqual(self.controller.active, set())
        newer.finish(self.event, current)
        self.assertNotIn("send", vars(self.event))
        self.assertNotIn("send_streaming", vars(self.event))

    async def test_later_plugin_wrapper_is_preserved_and_terminate_deactivates_own(self):
        trace = self.begin()
        ours = self.event.send
        async def other(*args, **kwargs):
            return await ours(*args, **kwargs)
        self.event.send = other
        self.controller.close()
        self.assertIs(self.event.send, other)
        await self.event.send(chain())
        self.assertEqual(trace.send_count, 0)

    async def test_reused_event_old_task_cannot_count_send_against_new_request(self):
        entered, release = asyncio.Event(), asyncio.Event()
        old_traces = []
        async def old_request():
            old_traces.append(self.begin())
            entered.set()
            await release.wait()
            await self.event.send(chain("old request output"))
        worker = asyncio.create_task(old_request())
        await entered.wait()
        current = self.begin()
        release.set()
        await worker
        self.assertEqual(current.send_count, 0)
        await self.event.send(chain("new request output"))
        self.assertEqual(current.send_count, 1)
        self.assertEqual(old_traces[0].send_count, 0)
        self.controller.finish(self.event, current)

    async def test_child_task_stream_callbacks_keep_request_association(self):
        async def stream(*args, **kwargs):
            async def consume():
                self.controller.agent_done(self.event)
                await self.event.send(chain("last chunk"))
            await asyncio.create_task(consume())
            return "streamed"
        self.event.send_streaming = stream
        trace = self.begin()
        self.assertEqual(await self.event.send_streaming(object()), "streamed")
        self.assertTrue(trace.agent_done_seen)
        self.assertEqual(trace.send_count, 1)
        self.assertTrue(trace.stream_completed)
        self.controller.finish(self.event, trace)

    async def test_closed_old_controller_cannot_replace_new_generation_trace(self):
        old = self.begin()
        self.controller.close()
        newer = load_flow().FlowController(self.config, self.logs.append, self.clock)
        current = newer.start(self.event)
        send = self.event.send
        self.assertIsNone(self.controller.start(self.event))
        self.assertIsNone(self.controller.new_window(self.event, 3000))
        self.assertIs(newer.trace(self.event), current)
        self.assertIs(self.event.send, send)
        self.assertTrue(old.finished)
        newer.finish(self.event, current)


class WaitLoopTests(unittest.IsolatedAsyncioTestCase):
    async def run_window(self, *, scoped=True, action=None, cancel=False):
        clock = Clock()
        event = Event()
        if not scoped:
            event.unified_msg_origin = "default:GroupMessage:elsewhere"
        controller = load_flow().FlowController({"flow_optimization_sessions": [Event.unified_msg_origin]}, clock=clock)
        state = SimpleNamespace(flow_control=controller, group_wait_window_timeout_ms=3000,
                                _group_wait_window_max_extra=3, group_wait_window_max_users=5,
                                _group_wait_window_counter=0, _group_wait_windows={},
                                _group_wait_window_lock=asyncio.Lock(), debug_mode=False)
        async def sleep(seconds):
            clock.tick += seconds
            if action:
                action(state, clock)
            if cancel:
                raise asyncio.CancelledError()
        tree = ast.parse((Path(__file__).parents[1] / "main.py").read_text(encoding="utf-8"))
        method = next(node for node in ast.walk(tree) if isinstance(node, ast.AsyncFunctionDef) and node.name == "_run_group_wait_window")
        ns = {"asyncio": SimpleNamespace(sleep=sleep), "time": SimpleNamespace(time=clock),
              "logger": SimpleNamespace(info=lambda *args: None)}
        exec(compile(ast.Module(body=[method], type_ignores=[]), "main.py", "exec"), ns)
        if cancel:
            with self.assertRaises(asyncio.CancelledError):
                await ns[method.name](state, "chat", "user", event)
            self.assertEqual(state._group_wait_windows, {})
            return
        count = await ns[method.name](state, "chat", "user", event)
        return clock.tick, count, state

    async def test_scoped_silence_is_shorter_and_other_group_stays_three_seconds(self):
        fast, _, _ = await self.run_window()
        legacy, _, _ = await self.run_window(scoped=False)
        self.assertLessEqual(fast, 1.11)
        self.assertGreaterEqual(legacy, 3)

    async def test_inflight_cache_finishes_before_early_silence_close(self):
        def action(state, clock):
            window = state._group_wait_windows[("chat", "user")]
            window["inflight"] = 1
            if clock.tick >= 1.5:
                window["inflight"] = 0
                window["extra_count"] = 1
        elapsed, count, _ = await self.run_window(action=action)
        self.assertGreaterEqual(elapsed, 1.5)
        self.assertEqual(count, 1)
        self.assertLessEqual(elapsed, 3.1)

    async def test_force_close_max_extra_takeover_and_cancel_keep_existing_guards(self):
        for key, value in [("force_complete", True), ("extra_count", 3)]:
            elapsed, _, state = await self.run_window(action=lambda state, clock: state._group_wait_windows[("chat", "user")].__setitem__(key, value))
            self.assertLess(elapsed, .2)
            self.assertEqual(state._group_wait_windows, {})
        def takeover(state, clock):
            state._group_wait_windows[("chat", "user")] = {"token": 999, "extra_count": 0}
        _, count, state = await self.run_window(action=takeover)
        self.assertEqual(count, 0)
        self.assertEqual(state._group_wait_windows[("chat", "user")]["token"], 999)
        await self.run_window(cancel=True)

    async def test_late_media_preparation_returns_to_normal_flow_without_orphan_cache(self):
        from tests.test_wait_window_quoted_images import WaitWindowQuotedImagesTest, FakeEvent, FakePlain
        WaitWindowQuotedImagesTest.setUpClass()
        base = WaitWindowQuotedImagesTest()
        harness, cache = base._make_harness("")
        policy = load_flow().AdaptiveWindow(3000, 1000, Clock())
        harness._group_wait_windows[("chat-1", "sender-1")]["adaptive"] = policy
        entered, release = asyncio.Event(), asyncio.Event()
        async def collect(*args):
            entered.set()
            await release.wait()
            return []
        ns = type(base).intercept.__globals__
        previous = ns["ImageHandler"]
        ns["ImageHandler"] = SimpleNamespace(collect_message_images=collect)
        try:
            event = FakeEvent("follow up", chain=[FakePlain("follow up")])
            task = asyncio.create_task(type(base).intercept(harness, event, "chat-1", False, False, None, "aiocqhttp"))
            await asyncio.wait_for(entered.wait(), 2)
            harness._group_wait_windows.clear()
            release.set()
            self.assertFalse(await task)
            self.assertEqual(cache.messages, [])
        finally:
            ns["ImageHandler"] = previous


class NativeProgressTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_decorator_preserves_progress_and_does_not_suppress_same_final(self):
        from tests.test_reply_success_accounting import compile_method, Event as ResultEvent, Result
        from tests.test_reply_brevity_budget import Handler
        flow_module = load_flow()
        log = Mock()
        handler = SimpleNamespace(_is_final_gate_decline=lambda text: False,
                                  _contains_final_gate_sentinel=lambda text: False,
                                  _apply_group_chat_brevity_limit=Handler._apply_group_chat_brevity_limit)
        decorate = compile_method("on_decorating_result", {
            "AstrMessageEvent": object, "logger": log, "time": __import__("time"),
            "classify_raw_llm_failure": lambda text: None, "ReplyHandler": handler,
            "sanitize_tool_call_markup": lambda text: SimpleNamespace(had_markup=False),
        })
        controller = flow_module.FlowController({"flow_optimization_sessions": [Event.unified_msg_origin]})
        event = ResultEvent()
        event.unified_msg_origin = Event.unified_msg_origin
        event.clear_result = lambda: event.set_result(None)
        async def send(payload):
            return "submitted"
        event.send = send
        trace = controller.start(event)
        controller.formal_request(event)
        duplicate_checks = []
        harness = SimpleNamespace(
            flow_control=controller, _get_message_id=lambda event: "managed",
            _clear_sent_step_image_direct_result=lambda *args: False,
            processing_sessions={"managed": True}, debug_mode=False,
            _maybe_replace_step_image_intermediate_text=lambda *args: None,
            _replace_llm_result_text=lambda result, text: setattr(result.chain[0], "text", text),
            raw_reply_cache={}, _pending_bot_replies={}, content_filter=SimpleNamespace(process_for_output=lambda text: text),
            enable_duplicate_filter=True, _match_recent_duplicate_reply=lambda *args: duplicate_checks.append(args),
            recent_replies_cache={}, duplicate_filter_check_count=5, _DUPLICATE_CACHE_SIZE_LIMIT=20,
            typo_enabled=False, typing_simulator_enabled=False,
        )
        long_text = "这次需要先核对最新数据，" + "保留必要的动作说明，" * 8 + "拿到结果后再回答。"
        event.set_result(Result(long_text))
        await decorate(harness, event)
        self.assertEqual(event.result.chain[0].text, long_text)
        self.assertEqual(duplicate_checks, [])
        await event.send(event.result)
        event.set_result(Result(long_text))
        await decorate(harness, event)
        self.assertIsNone(event.result)  # Confirmed repeat intermediate, no second send.
        short_text = "核对一下。"
        event.set_result(Result(short_text))
        await decorate(harness, event)
        await event.send(event.result)
        controller.agent_done(event)
        event.set_result(Result(short_text))
        await decorate(harness, event)
        self.assertEqual(event.result.chain[0].text, short_text)
        self.assertEqual(duplicate_checks, [])
        controller.finish(event, trace)
        log.error.assert_not_called()


if __name__ == "__main__":
    unittest.main()
