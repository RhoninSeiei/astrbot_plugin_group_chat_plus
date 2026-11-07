"""Exact request ownership across plugin generations; no completion inference.

Only the request coroutine acquires/releases a lease. Child callbacks inherit
its context and may resolve its owner, including after a module reload.
"""

import asyncio
import contextvars


LEASE_KEY = '_gcp_request_lease'
LEASE_PROTOCOL = 'gcp.request-lease.v1'
MANAGER_PROTOCOL = 'gcp.request-lifecycle.v1'
MANAGER_KEY = '_gcp_request_lifecycle'


def resolve_request_lease(event):
    lease = event.get_extra(LEASE_KEY) if event is not None else None
    if (getattr(lease, 'protocol', None) != LEASE_PROTOCOL
            or lease.released or lease.event is not event
            or lease.umo != str(getattr(event, 'unified_msg_origin', '') or '')
            or lease.context.get(None) is not lease.token):
        return None
    return lease


def resolve_request_owner(event, fallback=None):
    lease = resolve_request_lease(event)
    return lease.owner if lease is not None else fallback


class RequestLease:
    protocol = LEASE_PROTOCOL

    def __init__(self, manager, event):
        self.manager = manager
        self.owner, self.runtime = manager.owner, manager.runtime
        self.event = event
        self.umo = str(getattr(event, 'unified_msg_origin', '') or '')
        self.task = asyncio.current_task()
        self.token = object()
        self.context = contextvars.ContextVar('gcp_request_lease', default=None)
        self.context.set(self.token)
        self.released = False


class RequestLifecycle:
    protocol = MANAGER_PROTOCOL

    def __init__(self, owner, runtime):
        self.owner, self.runtime = owner, runtime
        self.accepting = True
        self.active = set()
        self._empty = asyncio.Event()
        self._empty.set()
        self._retired = asyncio.Event()
        self._changed = asyncio.Event()
        self._ready = False
        self._successor = None
        self._attached = False

    def attach(self, context):
        """Publish a successor link only; request state remains instance-local."""
        if self._attached:
            return
        previous = getattr(context, MANAGER_KEY, None)
        self._attached = True
        setattr(context, MANAGER_KEY, self)
        if (previous is not self
                and getattr(previous, 'protocol', None) == MANAGER_PROTOCOL):
            previous._successor = self
            previous._changed.set()

    def mark_ready(self):
        self._ready = True
        self._changed.set()

    def mark_retired(self):
        """Signal only after draining and resource cleanup have completed."""
        self._retired.set()
        self._changed.set()

    async def wait_successor(self, timeout=5):
        """Wait through retirement, then bound the wait for a usable successor.

        Waiting messages own no lease, so cannot hold up the old drain. Neither
        cancellation nor timeout is propagated to active request tasks.
        """
        while not self._retired.is_set():
            self._changed.clear()
            # A cancelled termination can resume this generation. The waiting
            # incoming message has no lease and may safely retry its intake.
            if self.accepting and self._ready:
                return self.owner
            await self._changed.wait()

        async def find_ready():
            candidate, seen = self, {id(self)}
            while True:
                candidate._changed.clear()
                successor = candidate._successor
                if successor is not None:
                    if id(successor) in seen:
                        return None
                    seen.add(id(successor))
                    candidate = successor
                    continue
                if (candidate is not self and candidate._ready
                        and candidate.accepting and not candidate._retired.is_set()):
                    return candidate.owner
                await candidate._changed.wait()

        try:
            return await asyncio.wait_for(find_ready(), timeout=timeout)
        except TimeoutError:
            return None

    def acquire(self, event):
        if not self.accepting:
            return None
        existing = event.get_extra(LEASE_KEY)
        # Even a different task cannot overwrite an outstanding event lease.
        if existing is not None:
            return None
        lease = RequestLease(self, event)
        event.set_extra(LEASE_KEY, lease)
        self.active.add(lease)
        self._empty.clear()
        return lease

    def release(self, event, lease):
        # Child callbacks may route, but only the original coroutine owns finally.
        if (lease is None or lease.manager is not self or lease.released
                or lease.event is not event or lease.task is not asyncio.current_task()
                or lease.context.get(None) is not lease.token):
            return False
        lease.released = True
        if event.get_extra(LEASE_KEY) is lease:
            event.set_extra(LEASE_KEY, None)
        lease.context.set(None)
        self.active.discard(lease)
        if not self.active:
            self._empty.set()
        return True

    def stop_intake(self):
        self.accepting = False
        self._changed.set()

    def resume_intake(self):
        """Recover from cancelled drain, before resource retirement starts."""
        if not self._retired.is_set():
            self.accepting = True
            self._changed.set()

    async def drain(self):
        """Wait for request finally blocks; never cancel or synthesize DONE.

        Call stop_intake first. Resource disposal belongs after this await.
        Cancelling this waiter does not cancel any active request.
        """
        await self._empty.wait()
