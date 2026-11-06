"""Request-owned execution limit for identical, explicitly read-only pending polls.

The class bridge only dispatches to an authorized request handle. It never
changes a Provider, global tool registry, handler or permission wrapper. Unknown
metadata/results remain observational. Completion returns a plain response to
the native runner, which owns final messages, DONE state and agent hooks.
"""
import asyncio
import hashlib
import importlib
import inspect
import json
import weakref
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass


_STATE_ATTR = '_gcp_tool_loop_limiter_state_v1'
_MISSING = object()
FINALIZATION_NOTE = (
    '同一只读查询连续返回相同的未完成状态，本轮停止重复调用工具。'
    '请依据已保留的实际工具返回，按当前人格自然回复已确认的信息和未完成部分。'
    '没有证据不要推断原因或声称已经完成；无需解释内部控制逻辑，不再调用工具。'
)
FALLBACK_TEXT = '这几次查询得到的内容没有变化，先停下重复查询。目前没有取得新的信息。'


def _digest(value, limit):
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                             separators=(',', ':'), allow_nan=False).encode('utf8')
        return hashlib.sha256(encoded).digest() if len(encoded) <= limit else None
    except (TypeError, ValueError, RecursionError):
        return None


def _hint(annotations):
    if isinstance(annotations, dict):
        return annotations.get('readOnlyHint', _MISSING)
    return getattr(annotations, 'readOnlyHint', _MISSING)


def is_explicitly_read_only(tool):
    """Inspect at most four permission wrappers; execution uses the outer tool."""
    seen = set()
    try:
        for _ in range(4):
            if tool is None or id(tool) in seen:
                return False
            seen.add(id(tool))
            for annotations in (getattr(tool, 'annotations', None),
                                getattr(getattr(tool, 'mcp_tool', None), 'annotations', None)):
                hint = _hint(annotations)
                if hint is not _MISSING and hint is not None:
                    return hint is True
            tool = getattr(tool, '_wrapped', None)
    except Exception:
        return False
    return False


def _pending_digest(result):
    """Require complete text-only JSON and an explicit unfinished state."""
    try:
        if getattr(result, 'isError', None) is not False:
            return None
        content = getattr(result, 'content', None)
        if not isinstance(content, list) or len(content) != 1:
            return None
        part = content[0]
        if getattr(part, 'type', None) != 'text' or not isinstance(getattr(part, 'text', None), str):
            return None
        text = part.text
        if len(text.encode('utf8')) > 65536:
            return None
        def unique_keys(items):
            value = {}
            for key, item in items:
                if key in value:
                    raise ValueError('ambiguous JSON key')
                value[key] = item
            return value
        payload = json.loads(text, object_pairs_hook=unique_keys)
        if not isinstance(payload, dict):
            return None
        if (payload.get('ok') is False or payload.get('success') is False
                or payload.get('error') or payload.get('errors')):
            return None
        statuses = [payload[key] for key in ('status', 'state') if key in payload]
        if not statuses or any(not isinstance(value, str) or value.casefold() not in {'pending', 'running', 'queued', 'in_progress'} for value in statuses):
            return None
        # Do not discard timestamps, progress or other fields: their changes
        # are evidence against declaring an identical result.
        return _digest(payload, 65536)
    except (ValueError, TypeError, AttributeError, RecursionError, UnicodeError):
        return None


def _call_digest(tool, args):
    name = getattr(tool, 'name', None)
    return _digest([name, args], 16384) if isinstance(name, str) and name else None


def _blocked_result(*, context_inactive=False):
    from mcp.types import CallToolResult, TextContent
    text = ('本请求的执行上下文已结束，本次工具未执行。'
            '已开始的操作以实际返回为准，不要声称后续工具已经执行成功。') if context_inactive else (
        '本次重复查询未执行：前面的只读查询连续返回相同未完成状态。'
        '请保留已有结果并直接说明当前可确认的信息，不要宣称任务完成或推断原因。'
    )
    return CallToolResult(isError=True, content=[TextContent(type='text', text=text)])


def _fallback_response():
    from astrbot.core.provider.entities import LLMResponse
    return LLMResponse(role='assistant', completion_text=FALLBACK_TEXT)


def _inactive_response():
    from astrbot.core.provider.entities import LLMResponse
    return LLMResponse(role='assistant', completion_text=(
        '本轮执行上下文已结束，后续工具没有继续执行。已开始的操作以实际返回为准。'))


def _final_note():
    from astrbot.core.agent.message import Message, TextPart
    message = Message(role='user', content=[TextPart(text=FINALIZATION_NOTE).mark_as_temp()])
    message._no_save = True
    return message


@contextmanager
def _owned_overrides(obj, values):
    originals = {name: vars(obj).get(name, _MISSING) for name in values}
    for name, value in values.items():
        setattr(obj, name, value)
    try:
        yield
    finally:
        for name, value in values.items():
            # Preserve a later foreign wrapper/value instead of overwriting it.
            if vars(obj).get(name, _MISSING) is value:
                if originals[name] is _MISSING:
                    delattr(obj, name)
                else:
                    setattr(obj, name, originals[name])


@dataclass
class _RequestState:
    runner_ref: object
    event: object
    request: object
    token: object
    follow_up_seq: object
    active: bool = True
    call_key: object = None
    result_key: object = None
    identical: int = 0
    armed: bool = False
    final_attempted: bool = False
    final_response: object = None
    inflight: int = 0
    retire_gate: object = None
    retire_original: object = None

    def reset_progress(self):
        self.call_key = self.result_key = None
        self.identical = 0
        self.armed = False

    def refresh_follow_up(self, runner):
        generation = getattr(runner, '_follow_up_seq', None)
        if generation != self.follow_up_seq:
            self.follow_up_seq = generation
            self.reset_progress()
            self.final_attempted = False
            self.final_response = None


class _ExecutionProxy:
    def __init__(self, handle, state, original):
        self.handle, self.state, self.original = handle, state, original

    def __getattr__(self, name):
        return getattr(self.original, name)

    async def execute(self, tool, run_context, **args):
        handle, state = self.handle, self.state
        owned = handle._owns(state)
        if not owned:
            # This proxy was created for an authorized batch. Losing that
            # request cannot authorize the batch's remaining side effects.
            handle._emit(state.event, 'blocked')
            yield _blocked_result(context_inactive=True)
            return
        if owned:
            state.refresh_follow_up(state.runner_ref())
        call_key = _call_digest(tool, args) if owned else None
        read_only = is_explicitly_read_only(tool) if owned else False
        if owned and state.armed and call_key != state.call_key and not state.final_attempted:
            state.reset_progress()
        if owned and (state.final_attempted or (state.armed and read_only and call_key == state.call_key)):
            handle._emit(state.event, 'blocked')
            yield _blocked_result()
            return
        stream = self.original.execute(tool=tool, run_context=run_context, **args)
        last = None
        outputs = 0
        complete = False
        try:
            async for item in stream:
                last = item
                outputs += 1
                yield item  # Keep original result identity and protocol intact.
            complete = True
        finally:
            close = getattr(stream, 'aclose', None)
            if close is not None:
                await close()
            if handle._owns(state):
                # Multiple yields may carry changing early results even when
                # the last pending snapshot repeats. Do not discard that data.
                result_key = _pending_digest(last) if complete and outputs == 1 and read_only and call_key else None
                if result_key is None:
                    state.reset_progress()
                else:
                    same = state.call_key == call_key and state.result_key == result_key
                    state.identical = state.identical + 1 if same else 1
                    state.call_key, state.result_key = call_key, result_key
                    if state.identical >= handle.identical_pending_limit and not state.armed:
                        state.armed = True
                        handle._emit(state.event, 'armed')


class ToolLoopLimiterHandle:
    def __init__(self, runner_cls, bridge, authorize, emit, identical_pending_limit, max_requests):
        self.runner_cls, self.bridge, self.authorize, self.emit = runner_cls, bridge, authorize, emit
        self.identical_pending_limit = max(2, min(10, int(identical_pending_limit)))
        self.max_requests = max(1, min(512, int(max_requests)))
        self.requests = OrderedDict()
        self.active = True
        bridge['handles'].append(self)

    def _emit(self, event, signal):
        if self.emit is not None:
            try:
                self.emit(event, signal)
            except Exception:
                pass  # Observation cannot interrupt execution or completion.

    def _token(self, event, request):
        if not self.active:
            return None
        try:
            return self.authorize(event, request)
        except Exception:
            return None

    def _owns(self, state):
        runner = state.runner_ref()
        return bool(self.active and state.active and runner is not None
                    and getattr(runner, 'req', None) is state.request
                    and getattr(getattr(getattr(runner, 'run_context', None), 'context', None), 'event', None) is state.event
                    and self._token(state.event, state.request) is state.token)

    def bind(self, runner):
        request = getattr(runner, 'req', None)
        event = getattr(getattr(getattr(runner, 'run_context', None), 'context', None), 'event', None)
        token = self._token(event, request) if event is not None and request is not None else None
        if token is None:
            return None
        key = id(runner)
        previous = self.requests.get(key)
        if previous and self._owns(previous):
            previous.refresh_follow_up(runner)
            self.requests.move_to_end(key)
            return previous
        if previous:
            previous.active = False
            del self.requests[key]
        try:
            runner_ref = weakref.ref(runner)
        except TypeError:
            return None
        state = _RequestState(runner_ref, event, request, token, getattr(runner, '_follow_up_seq', None))
        self.requests[key] = state
        while len(self.requests) > self.max_requests:
            _, evicted = self.requests.popitem(last=False)
            self._retire(evicted)
        return state

    def release(self, event, req=None):
        for key, state in tuple(self.requests.items()):
            if state.event is event and (req is None or state.request is req):
                self._retire(state)
                del self.requests[key]
                self._emit(event, 'released')

    def _retire(self, state):
        state.active = False
        runner = state.runner_ref()
        if runner is None or getattr(runner, 'req', None) is not state.request or state.retire_gate is not None:
            return
        try:
            done = bool(runner.done())
        except Exception:
            done = False
        # A request can be between provider/tool rounds without an active
        # generator. Retiring it must still prevent a fresh tool round. Native
        # DONE requests already finalized normally and need no instance gate.
        if not state.inflight and done:
            return
        name = '_iter_llm_responses_with_fallback'
        state.retire_original = vars(runner).get(name, _MISSING)

        async def gate():
            current = state.runner_ref()
            if current is None:
                return
            if getattr(current, 'req', None) is state.request:
                try:
                    yield _inactive_response()
                finally:
                    _clear_retire_gate(state)
                return
            _clear_retire_gate(state)
            stream = current._iter_llm_responses_with_fallback()
            try:
                async for item in stream:
                    yield item
            finally:
                await stream.aclose()

        state.retire_gate = gate
        setattr(runner, name, gate)

    def close(self):
        if not self.active:
            return
        self.active = False
        for state in self.requests.values():
            self._retire(state)
        self.requests.clear()
        self.bridge['handles'][:] = [handle for handle in self.bridge['handles'] if handle is not self]
        if self.bridge['handles']:
            return
        # Restore together only if both class hooks are still our exact owner.
        # An outer image/foreign wrapper can keep an inert bridge for reload.
        if any(self.runner_cls.__dict__.get(name) is not wrapper
               for name, wrapper in self.bridge['wrappers'].items()):
            return
        for name, original in self.bridge['originals'].items():
            setattr(self.runner_cls, name, original)
        if self.runner_cls.__dict__.get(_STATE_ATTR) is self.bridge:
            delattr(self.runner_cls, _STATE_ATTR)


def _binding(bridge, runner):
    for handle in reversed(tuple(bridge['handles'])):
        state = handle.bind(runner) if handle.active else None
        if state is not None:
            return handle, state
    return None


def _clear_retire_gate(state):
    if state.retire_gate is None:
        return
    runner = state.runner_ref()
    name = '_iter_llm_responses_with_fallback'
    if runner is not None and vars(runner).get(name, _MISSING) is state.retire_gate:
        if state.retire_original is _MISSING:
            delattr(runner, name)
        else:
            setattr(runner, name, state.retire_original)
    state.retire_gate = None


async def _final_responses(runner, handle, state, original):
    if state.final_attempted:
        if state.final_response is not None:
            yield state.final_response
        return
    state.final_attempted = True
    handle._emit(state.event, 'finalizing')
    note = _final_note()
    runner.run_context.messages.append(note)
    response = None
    no_tools = lambda: None
    try:
        with _owned_overrides(runner, {'_func_tool_for_provider': no_tools,
                'streaming': False, 'request_max_retries': 0,
                'EMPTY_OUTPUT_RETRY_ATTEMPTS': 1, 'fallback_providers': []}):
            stream = original(runner)
            try:
                async for candidate in stream:
                    if not getattr(candidate, 'is_chunk', False):
                        response = candidate
                        break
            finally:
                await stream.aclose()
    except (asyncio.CancelledError, GeneratorExit):
        raise
    except Exception:
        response = None
    finally:
        runner.run_context.messages[:] = [message for message in runner.run_context.messages if message is not note]
    stop_requested = getattr(runner, '_is_stop_requested', None)
    if callable(stop_requested) and stop_requested():
        # Native abort can end the generator normally without an exception.
        # Let _step take its own abort path; this is not an empty-model failure.
        return
    if not handle._owns(state):
        _clear_retire_gate(state)
        yield _inactive_response()
        return
    meaningful = bool(str(getattr(response, 'completion_text', '') or '').strip())
    if (getattr(response, 'role', None) != 'assistant' or not meaningful
            or getattr(response, 'tools_call_name', None)
            or getattr(response, 'rejected_tool_calls', None)):
        response = _fallback_response()
        handle._emit(state.event, 'fallback')
    state.final_response = response
    handle._emit(state.event, 'finalized')
    yield response  # Native _step calls its ordinary complete/on_agent_done path.


def install_tool_loop_limiter(*, authorize, emit=None, runner_cls=None,
                             identical_pending_limit=3, max_requests=128):
    if not callable(authorize):
        raise TypeError('request authorization callback is required')
    if runner_cls is None:
        runner_cls = importlib.import_module('astrbot.core.agent.runners.tool_loop_agent_runner').ToolLoopAgentRunner
    bridge = runner_cls.__dict__.get(_STATE_ATTR)
    if bridge is None:
        names = ('_handle_function_tools', '_iter_llm_responses_with_fallback')
        originals = {name: getattr(runner_cls, name, None) for name in names}
        if not all(inspect.isasyncgenfunction(method) for method in originals.values()):
            raise TypeError('unsupported native runner boundaries')
        bridge = {'handles': [], 'originals': originals, 'wrappers': {}}

        async def tools(runner, req, llm_response):
            binding = _binding(bridge, runner)
            original = bridge['originals']['_handle_function_tools']
            if binding is None or req is not getattr(runner, 'req', None):
                stream = original(runner, req, llm_response)
                try:
                    async for item in stream:
                        yield item
                finally:
                    await stream.aclose()
                return
            handle, state = binding
            proxy = _ExecutionProxy(handle, state, runner.tool_executor)
            state.inflight += 1
            try:
                with _owned_overrides(runner, {'tool_executor': proxy}):
                    stream = original(runner, req, llm_response)
                    try:
                        async for item in stream:
                            yield item
                    finally:
                        await stream.aclose()
            finally:
                state.inflight -= 1

        async def responses(runner):
            binding = _binding(bridge, runner)
            original = bridge['originals']['_iter_llm_responses_with_fallback']
            stream = (_final_responses(runner, *binding, original)
                      if binding is not None and (binding[1].armed or binding[1].final_attempted)
                      else original(runner))
            if binding is not None:
                binding[1].inflight += 1
            try:
                async for item in stream:
                    if binding is not None and not binding[0]._owns(binding[1]):
                        _clear_retire_gate(binding[1])
                        yield _inactive_response()
                        return
                    yield item
            finally:
                await stream.aclose()
                if binding is not None:
                    binding[1].inflight -= 1

        bridge['wrappers'] = dict(zip(names, (tools, responses)))
        for name, wrapper in bridge['wrappers'].items():
            setattr(runner_cls, name, wrapper)
        setattr(runner_cls, _STATE_ATTR, bridge)
    return ToolLoopLimiterHandle(runner_cls, bridge, authorize, emit, identical_pending_limit, max_requests)
