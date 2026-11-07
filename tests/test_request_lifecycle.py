import asyncio
import importlib.util
from pathlib import Path
import unittest


def load_module():
    path = Path(__file__).resolve().parents[1] / 'utils' / 'request_lifecycle.py'
    spec = importlib.util.spec_from_file_location('request_lifecycle_probe', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Event:
    unified_msg_origin = 'platform:group:1'
    message_id = 'same'

    def __init__(self):
        self.extra = {}

    def get_extra(self, key, default=None):
        return self.extra.get(key, default)

    def set_extra(self, key, value):
        self.extra[key] = value


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_termination_resumes_intake_and_wakes_waiting_message(self):
        m, event = load_module(), Event()
        manager = m.RequestLifecycle(object(), object())
        manager.mark_ready()
        lease = manager.acquire(event)
        manager.stop_intake()
        incoming = asyncio.create_task(manager.wait_successor(timeout=0.01))

        async def terminate():
            try:
                await manager.drain()
            except asyncio.CancelledError:
                manager.resume_intake()
                raise

        terminating = asyncio.create_task(terminate())
        await asyncio.sleep(0)
        self.assertFalse(incoming.done())
        terminating.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await terminating
        self.assertIs(await asyncio.wait_for(incoming, 0.1), manager.owner)
        self.assertFalse(lease.released)
        self.assertIs(m.resolve_request_owner(event), manager.owner)
        second_event = Event()
        second = manager.acquire(second_event)
        self.assertIsNotNone(second)
        self.assertTrue(manager.release(second_event, second))
        self.assertTrue(manager.release(event, lease))
        manager.stop_intake()
        await manager.drain()
        manager.mark_retired()
        manager.resume_intake()
        self.assertFalse(manager.accepting)

    async def test_successor_waits_for_drain_retirement_and_readiness(self):
        from types import SimpleNamespace
        m, context, event = load_module(), SimpleNamespace(), Event()
        old = m.RequestLifecycle(object(), object())
        old.attach(context)
        old.mark_ready()
        lease = old.acquire(event)
        old.stop_intake()
        forwarded = asyncio.create_task(old.wait_successor(timeout=0.05))
        # Drain time does not consume the successor initialization budget.
        await asyncio.sleep(0.07)
        self.assertFalse(forwarded.done())
        new = load_module().RequestLifecycle(object(), object())
        new.attach(context)
        self.assertIsNot(old.runtime, new.runtime)
        old.release(event, lease)
        await old.drain()
        old.mark_retired()
        await asyncio.sleep(0)
        self.assertFalse(forwarded.done())
        new.mark_ready()
        self.assertIs(await forwarded, new.owner)

    async def test_successor_skips_stopped_generation_and_times_out_on_disable(self):
        from types import SimpleNamespace
        m, context = load_module(), SimpleNamespace()
        old = m.RequestLifecycle(object(), object())
        old.attach(context)
        old.stop_intake()
        old.mark_retired()
        middle = load_module().RequestLifecycle(object(), object())
        middle.attach(context)
        middle.mark_ready()
        middle.stop_intake()
        last = load_module().RequestLifecycle(object(), object())
        last.attach(context)
        last.mark_ready()
        self.assertIs(await old.wait_successor(timeout=0.1), last.owner)
        last.stop_intake()
        last.mark_retired()
        self.assertIsNone(await last.wait_successor(timeout=0.01))

    async def test_cancel_successor_wait_preserves_active_request_and_failed_init_times_out(self):
        from types import SimpleNamespace
        m, context, event = load_module(), SimpleNamespace(), Event()
        old = m.RequestLifecycle(object(), object())
        old.attach(context)
        lease = old.acquire(event)
        old.stop_intake()
        waiting = asyncio.create_task(old.wait_successor(timeout=0.01))
        await asyncio.sleep(0)
        waiting.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiting
        self.assertIs(m.resolve_request_owner(event), old.owner)
        self.assertFalse(lease.released)
        old.release(event, lease)
        await old.drain()
        old.mark_retired()
        failed = load_module().RequestLifecycle(object(), object())
        failed.attach(context)
        self.assertIsNone(await old.wait_successor(timeout=0.01))

    async def test_generations_route_to_exact_owner_and_drain(self):
        first = load_module()
        owner, runtime, event = object(), object(), Event()
        manager = first.RequestLifecycle(owner, runtime)
        lease = manager.acquire(event)
        for _ in range(2):
            newer = load_module()
            self.assertIs(newer.resolve_request_owner(event), owner)
            self.assertIsNone(newer.RequestLifecycle(object(), object()).acquire(event))
        self.assertIs(lease.runtime, runtime)
        manager.stop_intake()
        self.assertIsNone(manager.acquire(Event()))
        draining = asyncio.create_task(manager.drain())
        await asyncio.sleep(0)
        self.assertFalse(draining.done())
        self.assertTrue(manager.release(event, lease))
        await draining
        self.assertFalse(manager.release(event, lease))
        self.assertIsNone(first.resolve_request_owner(event))

    async def test_same_id_other_event_and_changed_origin_are_rejected(self):
        m = load_module()
        manager = m.RequestLifecycle(object(), object())
        event, other = Event(), Event()
        lease = manager.acquire(event)
        other.extra = dict(event.extra)
        self.assertIsNone(m.resolve_request_owner(other))
        self.assertFalse(manager.release(other, lease))
        event.unified_msg_origin = 'different'
        self.assertIsNone(m.resolve_request_owner(event))
        event.unified_msg_origin = 'platform:group:1'
        self.assertTrue(manager.release(event, lease))

    async def test_old_task_cannot_route_or_release_reused_event(self):
        m = load_module()
        manager = m.RequestLifecycle(object(), object())
        event = Event()
        ready, resume = asyncio.Event(), asyncio.Event()

        async def stale():
            old = manager.acquire(event)
            manager.release(event, old)
            ready.set()
            await resume.wait()
            self.assertIsNone(m.resolve_request_owner(event))
            self.assertFalse(manager.release(event, current))
            self.assertFalse(manager.release(event, old))

        task = asyncio.create_task(stale())
        await ready.wait()
        current = manager.acquire(event)
        resume.set()
        await task
        self.assertIs(m.resolve_request_owner(event), manager.owner)
        manager.release(event, current)

    async def test_child_callback_inherits_context_but_unrelated_task_does_not(self):
        m = load_module()
        manager, event = m.RequestLifecycle(object(), object()), Event()
        go = asyncio.Event()

        async def unrelated():
            await go.wait()
            self.assertIsNone(m.resolve_request_owner(event))

        task = asyncio.create_task(unrelated())
        lease = manager.acquire(event)

        async def child():
            self.assertIs(m.resolve_request_owner(event), manager.owner)

        await asyncio.create_task(child())
        go.set()
        await task
        manager.release(event, lease)

    async def test_cancel_releases_and_drain_does_not_claim_terminal(self):
        m = load_module()
        manager, event = m.RequestLifecycle(object(), object()), Event()
        ready = asyncio.Event()

        async def request():
            lease = manager.acquire(event)
            try:
                ready.set()
                await asyncio.Event().wait()
            finally:
                manager.release(event, lease)

        task = asyncio.create_task(request())
        await ready.wait()
        manager.stop_intake()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await manager.drain()
        self.assertFalse(manager.active)

    async def test_drain_after_sdk_submit_preserves_actual_incomplete_flow(self):
        try:
            from tests.test_flow_control import Event as FlowEvent, chain, load_flow
        except ModuleNotFoundError:
            from test_flow_control import Event as FlowEvent, chain, load_flow
        m, flow_module = load_module(), load_flow()
        event, rows = FlowEvent(), []
        flow = flow_module.FlowController(
            {'flow_metrics_sessions': [event.unified_msg_origin]}, rows.append)
        manager = m.RequestLifecycle(object(), object())
        lease = manager.acquire(event)
        trace = flow.start(event)
        flow.formal_request(event)
        await event.send(chain('delivered but no terminal callback'))
        manager.stop_intake()
        flow.finish(event, trace)
        manager.release(event, lease)
        await manager.drain()
        self.assertFalse(trace.agent_done_seen)
        self.assertEqual(trace.send_count, 1)
        self.assertEqual([r['outcome'] for r in rows if r['stage'] == 'complete'],
                         ['incomplete_agent'])

    async def test_cancelled_drain_does_not_cancel_request_or_allow_child_release(self):
        m = load_module()
        manager, event = m.RequestLifecycle(object(), object()), Event()
        lease = manager.acquire(event)
        manager.stop_intake()
        waiter = asyncio.create_task(manager.drain())
        await asyncio.sleep(0)
        waiter.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiter

        async def child():
            self.assertFalse(manager.release(event, lease))
        await asyncio.create_task(child())
        self.assertIs(m.resolve_request_owner(event), manager.owner)
        self.assertTrue(manager.release(event, lease))
        await manager.drain()


if __name__ == '__main__':
    unittest.main()
