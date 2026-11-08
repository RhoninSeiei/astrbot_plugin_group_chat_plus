"""Enriched requests must reach the native lock without lossy text capture."""
import asyncio
import importlib.util
import linecache
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


def load_guard(name="gcp_native_capture_guard_test"):
    spec = importlib.util.spec_from_file_location(name, ROOT / "utils/native_capture_guard.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


SOURCE = '''async def build_main_agent(*, event):
    event.seen.append("build")
    return event

async def call_event_hook(event, kind, result):
    event.seen.append("hook")

class InternalAgentSubStage:
    async def process(self, event):
        follow_up_capture = try_capture_follow_up(event)
        if follow_up_capture:
            event.seen.append("captured")
            return
        async with session_lock_manager.acquire_lock(event.unified_msg_origin):
            result = await build_main_agent(event=event)
            await call_event_hook(event, EventType.OnLLMRequestEvent, result)
            await self._save_to_history(event, result)

    async def _save_to_history(self, event, result):
        event.seen.append("save")
'''


class Lock:
    def acquire_lock(self, umo):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


def stage(source=SOURCE):
    module = types.ModuleType("controlled_capture_stage_" + str(id(source)))
    filename = "<" + module.__name__ + ">"
    linecache.cache[filename] = (len(source), None, source.splitlines(keepends=True), filename)
    module.calls = []

    def capture(event):
        module.calls.append(event)
        return event.capture_ticket

    module.try_capture_follow_up = capture
    module.session_lock_manager = Lock()
    module.EventType = types.SimpleNamespace(OnLLMRequestEvent="request")
    exec(compile(source, filename, "exec"), vars(module))
    return module


def event(*, capture_ticket=object()):
    return types.SimpleNamespace(unified_msg_origin="controlled:GroupMessage:one",
                                 capture_ticket=capture_ticket, seen=[])


class NativeCaptureGuardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.guard = load_guard()
        self.module = stage()
        self.original = self.module.try_capture_follow_up
        self.handles = []

    async def asyncTearDown(self):
        for handle in self.handles:
            handle.close()

    def install(self, authorize, module=None, guard=None):
        handle = (guard or self.guard).install_native_capture_guard(
            authorize=authorize, stage_module=module or self.module)
        self.handles.append(handle)
        return handle

    async def test_authorized_exact_event_bypasses_capture_and_reaches_lock_hooks_and_save(self):
        enriched = event()
        handle = self.install(lambda candidate: candidate is enriched)
        self.assertTrue(handle.supported)
        await self.module.InternalAgentSubStage().process(enriched)
        self.assertEqual(enriched.seen, ["build", "hook", "save"])
        self.assertEqual(self.module.calls, [])

    async def test_plain_and_other_plugin_events_preserve_original_capture(self):
        enriched, plain, other = event(), event(), event()
        self.install(lambda candidate: candidate is enriched)
        for candidate in (plain, other):
            await self.module.InternalAgentSubStage().process(candidate)
            self.assertEqual(candidate.seen, ["captured"])
        self.assertEqual(self.module.calls, [plain, other])

    async def test_same_values_different_event_does_not_inherit_authorization(self):
        enriched, copy = event(), event()
        self.install(lambda candidate: candidate is enriched)
        self.assertIs(self.module.try_capture_follow_up(copy), copy.capture_ticket)

    async def test_only_literal_true_authorizes_bypass(self):
        candidate = event()
        self.install(lambda candidate: "truthy")
        self.assertIs(self.module.try_capture_follow_up(candidate), candidate.capture_ticket)

    async def test_no_capture_ticket_preserves_normal_native_execution(self):
        candidate = event(capture_ticket=None)
        self.install(lambda candidate: False)
        await self.module.InternalAgentSubStage().process(candidate)
        self.assertEqual(candidate.seen, ["build", "hook", "save"])
        self.assertEqual(self.module.calls, [candidate])

    async def test_authorization_exception_never_falls_through_to_lossy_capture(self):
        def failing(candidate):
            raise RuntimeError("controlled authorization failure")
        self.install(failing)
        with self.assertRaisesRegex(RuntimeError, "authorization failure"):
            self.module.try_capture_follow_up(event())
        self.assertEqual(self.module.calls, [])

    async def test_close_is_idempotent_and_restores_exact_original(self):
        handle = self.install(lambda candidate: True)
        handle.close()
        handle.close()
        self.assertFalse(handle.supported)
        self.assertIs(self.module.try_capture_follow_up, self.original)

    async def test_two_import_generations_share_one_wrapper_and_close_independently(self):
        first_event, second_event = event(), event()
        first = self.install(lambda candidate: candidate is first_event)
        wrapper = self.module.try_capture_follow_up
        fresh_guard = load_guard("gcp_native_capture_guard_test_new_generation")
        second = self.install(lambda candidate: candidate is second_event, guard=fresh_guard)
        self.assertTrue(first.supported)
        self.assertTrue(second.supported)
        self.assertIs(self.module.try_capture_follow_up, wrapper)
        first.close()
        self.assertIs(self.module.try_capture_follow_up(first_event), first_event.capture_ticket)
        self.assertIsNone(self.module.try_capture_follow_up(second_event))
        second.close()
        self.assertIs(self.module.try_capture_follow_up, self.original)

    async def test_later_foreign_wrapper_is_preserved_and_reinstallation_declines(self):
        first = self.install(lambda candidate: True)
        own_wrapper = self.module.try_capture_follow_up
        def foreign(candidate):
            return own_wrapper(candidate)
        self.module.try_capture_follow_up = foreign
        self.assertFalse(first.supported)
        second = self.install(lambda candidate: True)
        self.assertFalse(second.supported)
        first.close()
        self.assertIs(self.module.try_capture_follow_up, foreign)
        candidate = event()
        self.assertIs(foreign(candidate), candidate.capture_ticket)

    async def test_restored_own_wrapper_can_be_reused_without_stacking(self):
        first = self.install(lambda candidate: True)
        wrapper = self.module.try_capture_follow_up
        self.module.try_capture_follow_up = lambda candidate: wrapper(candidate)
        first.close()
        self.module.try_capture_follow_up = wrapper
        second = self.install(lambda candidate: True)
        self.assertTrue(second.supported)
        self.assertIs(self.module.try_capture_follow_up, wrapper)
        second.close()
        self.assertIs(self.module.try_capture_follow_up, self.original)

    async def test_unknown_core_and_capture_after_lock_do_not_mutate_binding(self):
        invalids = [
            SOURCE.replace("follow_up_capture = try_capture_follow_up(event)",
                           "follow_up_capture = None"),
            SOURCE.replace("follow_up_capture = try_capture_follow_up(event)",
                           "follow_up_capture = alternate_capture(event)"),
            SOURCE.replace("follow_up_capture = try_capture_follow_up(event)",
                           "follow_up_capture = try_capture_follow_up(event)\n        try_capture_follow_up(event)"),
            SOURCE.replace("event.unified_msg_origin", "'different-lock'"),
            SOURCE.replace("await self._save_to_history(event, result)", "pass"),
            SOURCE.replace("follow_up_capture = try_capture_follow_up(event)",
                           "follow_up_capture = None").replace(
                               "result = await build_main_agent(event=event)",
                               "follow_up_capture = try_capture_follow_up(event)\n            result = await build_main_agent(event=event)"),
        ]
        for source in invalids:
            with self.subTest(source=source):
                module = stage(source)
                original = module.try_capture_follow_up
                handle = self.install(lambda candidate: True, module=module)
                self.assertFalse(handle.supported)
                self.assertIs(module.try_capture_follow_up, original)

    async def test_unavailable_source_and_invalid_capture_boundary_leave_stage_unchanged(self):
        with patch.object(self.guard.inspect, "getsource", side_effect=OSError("source unavailable")):
            self.assertFalse(self.install(lambda candidate: True).supported)
        self.assertIs(self.module.try_capture_follow_up, self.original)
        for capture in (None, lambda event, other: None):
            self.module.try_capture_follow_up = capture
            self.assertFalse(self.install(lambda candidate: True).supported)
            self.assertIs(self.module.try_capture_follow_up, capture)

    async def test_existing_unknown_bridge_is_not_overwritten(self):
        marker = {"protocol": "foreign"}
        setattr(self.module, self.guard._BRIDGE_ATTR, marker)
        self.assertFalse(self.install(lambda candidate: True).supported)
        self.assertIs(self.module.try_capture_follow_up, self.original)
        self.assertIs(getattr(self.module, self.guard._BRIDGE_ATTR), marker)

    async def test_mismatched_process_globals_declines_without_mutation(self):
        module = stage()
        other = stage(SOURCE + "\n# another module\n")
        module.InternalAgentSubStage = other.InternalAgentSubStage
        original = module.try_capture_follow_up
        handle = self.install(lambda candidate: True, module=module)
        self.assertFalse(handle.supported)
        self.assertIs(module.try_capture_follow_up, original)

    async def test_non_callable_authorization_is_rejected(self):
        with self.assertRaises(TypeError):
            self.guard.install_native_capture_guard(authorize=None, stage_module=self.module)


if __name__ == "__main__":
    unittest.main()
