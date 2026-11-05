"""Opt-in, request-local flow observation; never executes tools or sends messages."""
import hashlib
import contextvars
import time
import uuid


TRACE_KEY = "_gcp_flow_trace"
TRACE_PROTOCOL = "gcp.flow.trace.v1"
PROGRESS_INSTRUCTION = (
    "\n[耗时工具的自然反馈]\n"
    "实际需要搜索、查询、绘图等可能让对方等待的操作时，可以在调用工具前，"
    "按当前人格自然说明准备做的事情。动作说明不属于内部思考，无需解释判断过程。"
    "快速操作无需额外寒暄；不要使用固定进度句式、重复通知、编造进度或提前宣称完成。"
    "工具结束后直接回应结果；保留当前人格，不改变语气，不复述工具协议、参数或内部标识。"
)


def _digest(text):
    return hashlib.sha256(" ".join(str(text or "").split()).encode()).digest()


def _milliseconds(value, default, minimum, maximum):
    try:
        value = int(value)
    except (TypeError, ValueError, OverflowError):
        value = default
    return max(minimum, min(maximum, value))


def _visible(chain):
    for part in getattr(chain, "chain", None) or []:
        if str(getattr(part, "text", "") or "").strip():
            return True
        name = type(part).__name__.lower()
        if name not in {"at", "atall", "reply", "plain"} and name in {
            "image", "record", "video", "file", "face", "poke", "node", "nodes",
            "forward", "dice", "rps", "contact", "location", "json", "wechatemoji",
            "music", "share",
        }:
            if any(getattr(part, key, None) not in (None, "", [], {})
                   for key in ("file", "url", "file_", "data", "content", "id", "md5", "audio", "lat", "value")):
                return True
    return False


class AdaptiveWindow:
    def __init__(self, timeout_ms, quiet_ms, clock):
        self.clock = clock
        now = clock()
        self.hard_deadline = now + _milliseconds(timeout_ms, 3000, 200, 3000) / 1000
        self.quiet_seconds = _milliseconds(quiet_ms, 1000, 600, 1600) / 1000
        self.deadline = min(self.hard_deadline, now + self.quiet_seconds)
        self.last_arrival = now
        self.average_gap = None

    def note_message(self, arrival=None):
        now = self.clock() if arrival is None else arrival
        gap = max(0, now - self.last_arrival)
        self.average_gap = gap if self.average_gap is None else .5 * (self.average_gap + gap)
        self.quiet_seconds = min(1.6, max(.6, 1.5 * self.average_gap))
        self.last_arrival = now
        self.deadline = min(self.hard_deadline, now + self.quiet_seconds)

    def expired(self):
        return self.clock() >= self.deadline


class FlowTrace:
    protocol = TRACE_PROTOCOL

    def __init__(self, owner, emit, clock, release, context):
        self.owner, self.emit, self.clock = owner, emit, clock
        self.context = context
        self.request_id = uuid.uuid4().hex[:16]
        self.started = clock()
        self.stages = {}
        self.tools = []
        self.tool_seq = self.send_count = 0
        self.formal_started = self.agent_done_seen = self.finished = False
        self.send_failed = self.stream_completed = self.failed = False
        self.progress_hashes = set()
        self.progress_candidate = None
        self.restorers = []
        self.release = release
        self.point("request_start")

    def point(self, stage, outcome="ok", *, duration_ms=None, tool_seq=None):
        if self.finished:
            return
        row = {"request_id": self.request_id, "stage": stage,
               "elapsed_ms": round(max(0, self.clock() - self.started) * 1000, 1),
               "outcome": outcome}
        if duration_ms is not None:
            row["duration_ms"] = round(max(0, duration_ms), 1)
        if tool_seq is not None:
            row["tool_seq"] = tool_seq
        try:
            self.emit(row)
        except Exception:
            pass  # Observability never changes the request outcome.

    def begin(self, stage):
        if stage not in self.stages and not self.finished:
            self.stages[stage] = self.clock()
            self.point(stage + "_start")

    def end(self, stage, outcome="ok"):
        started = self.stages.pop(stage, None)
        if started is not None:
            self.point(stage + "_end", outcome, duration_ms=(self.clock() - started) * 1000)

    def submitted(self, chain):
        if self.finished or not self.formal_started or not _visible(chain):
            return
        self.send_count += 1
        if self.send_count == 1:
            self.point("first_sdk_submit")
        self.point("sdk_submit")
        if self.agent_done_seen:
            self.point("final_sdk_submit")
        else:
            text = "".join(str(getattr(p, "text", "") or "") for p in getattr(chain, "chain", []))
            if text.strip():
                self.progress_hashes.add(_digest(text))
                if self.progress_candidate is not None:
                    self.progress_hashes.add(self.progress_candidate)
        self.progress_candidate = None

    def close_tools(self):
        for _, seq, started in self.tools:
            self.point("tool_end", "unobserved_end", duration_ms=(self.clock() - started) * 1000, tool_seq=seq)
        self.tools.clear()

    def finish(self, outcome):
        if self.finished:
            return
        self.close_tools()
        for stage in list(self.stages):
            self.end(stage, "interrupted")
        self.point("complete", outcome)
        self.finished = True
        for restore in reversed(self.restorers):
            try:
                restore()
            except Exception:
                pass
        self.restorers.clear()
        if self.context.get(None) is self:
            self.context.set(None)
        self.release(self)


class FlowController:
    def __init__(self, config, emit=None, clock=None):
        self.config = config or {}
        self.emit = emit or (lambda row: None)
        self.clock = clock or time.monotonic
        self.owner = object()
        self.active = set()
        self.closed = False
        # Child tasks inherit the request association; a reused event in an
        # older task does not inherit a newer request's observation context.
        self.context = contextvars.ContextVar("gcp_flow_request", default=None)

    def enabled(self, event, feature=None):
        sessions = self.config.get("flow_optimization_sessions", [])
        return bool(not self.closed and event is not None and isinstance(sessions, list)
                    and str(getattr(event, "unified_msg_origin", "") or "") in sessions
                    and not event.is_private_chat()
                    and (feature is None or self.config.get(feature, True)))

    def trace(self, event):
        trace = event.get_extra(TRACE_KEY) if event is not None else None
        return trace if (getattr(trace, "protocol", None) == TRACE_PROTOCOL
                         and trace.owner is self.owner and not trace.finished
                         and trace.context.get(None) is trace) else None

    def start(self, event):
        if not self.enabled(event):
            return None
        old = event.get_extra(TRACE_KEY)
        # The stable protocol survives module reloads; the old class need not
        # be an instance of this generation's FlowTrace class.
        if getattr(old, "protocol", None) == TRACE_PROTOCOL and not old.finished:
            old.finish("superseded")
        emit = self.emit if self.config.get("flow_metrics_enabled", True) else lambda row: None
        trace = FlowTrace(self.owner, emit, self.clock, self.active.discard, self.context)
        self.context.set(trace)
        event.set_extra(TRACE_KEY, trace)
        self.active.add(trace)
        self._observe_sender(event, trace, "send")
        self._observe_sender(event, trace, "send_streaming")
        def clear_event():
            if event.get_extra(TRACE_KEY) is trace:
                event.set_extra(TRACE_KEY, None)
        trace.restorers.append(clear_event)
        return trace

    def _observe_sender(self, event, trace, name):
        try:
            original = getattr(event, name)
            namespace = vars(event)
            had_own = name in namespace
            previous = namespace.get(name)
            for _ in range(16):
                if (getattr(original, "_gcp_flow_protocol", None) != TRACE_PROTOCOL
                        or not original._gcp_flow_trace.finished):
                    break
                original = original._gcp_flow_original

            async def observed(*args, **kwargs):
                try:
                    value = await original(*args, **kwargs)
                except BaseException:
                    if not trace.finished and trace.context.get(None) is trace and trace.formal_started:
                        trace.send_failed = True
                        trace.point("send_failed", "failed")
                    raise
                if trace.finished or trace.context.get(None) is not trace:
                    return value
                if name == "send":
                    trace.submitted(args[0] if args else kwargs.get("message"))
                elif not trace.finished and trace.formal_started:
                    trace.stream_completed = True
                    trace.point("stream_submit_completed")
                return value

            setattr(event, name, observed)
            observed._gcp_flow_protocol = TRACE_PROTOCOL
            observed._gcp_flow_trace = trace
            observed._gcp_flow_original = original
            observed._gcp_flow_previous = previous
            observed._gcp_flow_had_own = had_own

            def restore():
                # Never overwrite a later wrapper installed by another plugin.
                if getattr(event, name, None) is observed:
                    restore_own, restore_value = had_own, previous
                    for _ in range(16):
                        if (getattr(restore_value, "_gcp_flow_protocol", None) != TRACE_PROTOCOL
                                or not restore_value._gcp_flow_trace.finished):
                            break
                        restore_own = restore_value._gcp_flow_had_own
                        restore_value = restore_value._gcp_flow_previous
                    if restore_own:
                        setattr(event, name, restore_value)
                    else:
                        delattr(event, name)

            trace.restorers.append(restore)
        except Exception:
            trace.point("send_observation", "hook_only")

    def begin(self, event, stage):
        trace = self.trace(event)
        if trace:
            trace.begin(stage)
        return trace

    def formal_request(self, event, *, ready=False):
        trace = self.trace(event)
        if trace and not trace.formal_started:
            trace.formal_started = True
            trace.begin("formal_total")
        if trace and ready:
            trace.point("formal_request_ready")

    def tool_start(self, event, tool):
        trace = self.trace(event)
        if trace and trace.formal_started:
            trace.tool_seq += 1
            trace.tools.append((id(tool), trace.tool_seq, self.clock()))
            trace.point("tool_start", tool_seq=trace.tool_seq)

    def tool_end(self, event, tool, result):
        trace = self.trace(event)
        if not trace or not trace.formal_started:
            return
        for index, (identity, seq, started) in enumerate(trace.tools):
            if identity == id(tool):
                trace.tools.pop(index)
                trace.point("tool_end", "failed" if getattr(result, "isError", False) else "ok",
                            duration_ms=(self.clock() - started) * 1000, tool_seq=seq)
                break

    def agent_done(self, event, response=None):
        trace = self.trace(event)
        if trace:
            trace.agent_done_seen = True
            trace.failed = getattr(response, "role", None) == "err"
            trace.close_tools()
            trace.end("formal_total", "failed" if getattr(response, "role", None) == "err" else "ok")
            trace.point("agent_done")

    def failure(self, event):
        trace = self.trace(event)
        if trace:
            trace.failed = True
            trace.point("request_failed", "failed")

    def send_hook(self, event):
        trace = self.trace(event)
        if trace and trace.formal_started:
            trace.point("send_hook")

    def keep_progress(self, event, result):
        trace = self.trace(event)
        kind = str(getattr(getattr(result, "result_content_type", None), "name", ""))
        return bool(trace and trace.formal_started and not trace.agent_done_seen
                    and self.enabled(event, "flow_tool_progress_enabled")
                    and not kind.startswith("STREAMING") and getattr(result, "async_stream", None) is None
                    and result.is_llm_result())

    def prepare_progress(self, event, text, result):
        if self.keep_progress(event, result):
            self.trace(event).progress_candidate = _digest(text)

    def repeat_progress(self, event, text, result):
        return bool(self.keep_progress(event, result) and _digest(text) in self.trace(event).progress_hashes)

    def final_matches_progress(self, event, text):
        trace = self.trace(event)
        return bool(trace and self.enabled(event, "flow_tool_progress_enabled")
                    and trace.agent_done_seen and _digest(text) in trace.progress_hashes)

    def new_window(self, event, timeout_ms):
        if self.enabled(event, "flow_adaptive_wait_enabled"):
            return AdaptiveWindow(timeout_ms, self.config.get("flow_wait_quiet_ms", 1000), self.clock)
        return None

    def finish(self, event, trace=None, outcome=None):
        trace = trace or self.trace(event)
        if trace is None or trace.owner is not self.owner:
            return
        if outcome is None:
            outcome = ("failed" if trace.send_failed or trace.failed else
                       "agent_done" if trace.agent_done_seen else
                       "incomplete_agent" if trace.formal_started else "no_formal_request")
        trace.finish(outcome)
        self.active.discard(trace)
        if event.get_extra(TRACE_KEY) is trace:
            event.set_extra(TRACE_KEY, None)

    def close(self):
        self.closed = True
        for trace in tuple(self.active):
            trace.finish("terminated")
        self.active.clear()
