"""Opt-in, request-local flow observation; never executes tools or sends messages."""
import hashlib
import contextvars
import time
import uuid
from types import SimpleNamespace

from .turn_continuity import TurnStateCache, build_turn_context
from .native_memory_budget import select_native_memory_parts
from .tool_progress_guard import RequestToolProgress


TRACE_KEY = "_gcp_flow_trace"
TRACE_PROTOCOL = "gcp.flow.trace.v1"
HANDOFF_REQUESTED = "_gcp_lm_handoff_requested"
HANDOFF_RESULT = "_gcp_lm_handoff"
HANDOFF_OWNER = "_gcp_lm_handoff_owner"
EXIT_REASONS = {"probability", "cooldown", "blacklist", "merged", "no_reply",
                "cancelled", "failed", "filtered", "duplicate", "completed",
                "decision_timeout", "decision_empty", "decision_protocol", "decision_provider",
                "decision_failed", "decision_unknown"}
TOOL_SIGNALS = {"started", "duplicate_pending", "duplicate_success", "retry_after_failure",
                "uncertain_repeat", "stale_pending", "success", "failed", "unknown",
                "unrecognized", "superseded_result", "pending", "repeated_result"}
METRIC_COUNTS = {"before_chars", "after_chars", "retained_entries", "budget_chars",
                 "context_chars", "attempts", "stalled", "entry_count_known"}
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

    def __init__(self, owner, emit, clock, release, context, behavior_enabled):
        self.owner, self.emit, self.clock = owner, emit, clock
        self.context = context
        self.cohort = "behavior_pilot" if behavior_enabled else "observe_only"
        self.exit_reason = None
        self.request_id = uuid.uuid4().hex[:16]
        self.started = clock()
        self.stages = {}
        self.tools = []
        self.tool_seq = self.send_count = 0
        self.formal_started = self.agent_done_seen = self.finished = False
        self.loop_request = None
        self.send_failed = self.stream_completed = self.failed = False
        self.progress_hashes = set()
        self.progress_candidate = None
        self.request_prepared = False
        self.turn_key = None
        self.tool_progress = None
        self.final_send_count = 0
        self.handoff_request = None
        self.restorers = []
        self.release = release
        self.point("request_start")

    def point(self, stage, outcome="ok", *, duration_ms=None, tool_seq=None, counts=None):
        if self.finished:
            return
        row = {"request_id": self.request_id, "cohort": self.cohort, "stage": stage,
               "elapsed_ms": round(max(0, self.clock() - self.started) * 1000, 1),
               "outcome": outcome}
        if duration_ms is not None:
            row["duration_ms"] = round(max(0, duration_ms), 1)
        if tool_seq is not None:
            row["tool_seq"] = tool_seq
        for key, value in (counts or {}).items():
            if key in METRIC_COUNTS and isinstance(value, (int, float)) and 0 <= value < 100000000:
                row[key] = value
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
            self.final_send_count += 1
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
        self.loop_request = None
        if self.context.get(None) is self:
            self.context.set(None)
        self.release(self)


class FlowController:
    def __init__(self, config, emit=None, clock=None, part_factory=None):
        self.config = config or {}
        self.emit = emit or (lambda row: None)
        self.clock = clock or time.monotonic
        self.owner = object()
        self.active = set()
        self.sequence = 0
        self.reset_orders = {}
        self.closed = False
        self.loop_release = None
        # Child tasks inherit the request association; a reused event in an
        # older task does not inherit a newer request's observation context.
        self.context = contextvars.ContextVar("gcp_flow_request", default=None)
        self.part_factory = part_factory
        self.turn_cache = TurnStateCache(
            ttl_seconds=_milliseconds(self.config.get("flow_continuity_ttl_seconds", 180), 180, 30, 600),
            max_entries=_milliseconds(self.config.get("flow_continuity_max_entries", 128), 128, 16, 512),
            clock=self.clock,
            max_fields=12,
        )

    def enabled(self, event, feature=None):
        sessions = self.config.get("flow_optimization_sessions", [])
        return bool(not self.closed and event is not None and isinstance(sessions, list)
                    and str(getattr(event, "unified_msg_origin", "") or "") in sessions
                    and not event.is_private_chat()
                    and (feature is None or self.config.get(feature, True)))

    def metrics_enabled(self, event):
        sessions = self.config.get("flow_metrics_sessions", self.config.get("flow_optimization_sessions", []))
        return bool(not self.closed and event is not None and isinstance(sessions, list)
                    and str(getattr(event, "unified_msg_origin", "") or "") in sessions
                    and not event.is_private_chat() and self.config.get("flow_metrics_enabled", True))

    def trace(self, event):
        trace = event.get_extra(TRACE_KEY) if event is not None else None
        return trace if (getattr(trace, "protocol", None) == TRACE_PROTOCOL
                         and trace.owner is self.owner and not trace.finished
                         and trace.context.get(None) is trace) else None

    def tool_loop_token(self, event, request):
        """Authorize only this prepared request, never another plugin's runner."""
        trace = self.trace(event)
        return trace if (trace and trace.formal_started
                         and trace.loop_request is request and request is not None
                         and self.enabled(event, 'flow_tool_guard_enabled')) else None

    def tool_loop_observation(self, event, signal):
        trace = self.trace(event)
        if trace:
            reason = signal if signal in {'armed', 'blocked', 'finalizing', 'finalized',
                                          'fallback', 'released'} else 'unrecognized'
            trace.point('tool_loop_limit', reason)

    def start(self, event):
        if not (self.enabled(event) or self.metrics_enabled(event)):
            return None
        old = event.get_extra(TRACE_KEY)
        # The stable protocol survives module reloads; the old class need not
        # be an instance of this generation's FlowTrace class.
        if getattr(old, "protocol", None) == TRACE_PROTOCOL and not old.finished:
            old.finish("superseded")
        emit = self.emit if self.metrics_enabled(event) else lambda row: None
        trace = FlowTrace(self.owner, emit, self.clock, self.active.discard, self.context, self.enabled(event))
        self.sequence += 1
        trace.order = self.sequence
        self.context.set(trace)
        if self.config.get("flow_tool_guard_enabled", True):
            trace.tool_progress = RequestToolProgress(
                trace.request_id,
                stale_seconds=_milliseconds(self.config.get("flow_tool_stale_seconds", 45), 45, 5, 300),
                clock=self.clock,
            )
        event.set_extra(TRACE_KEY, trace)
        self.active.add(trace)
        self._observe_sender(event, trace, "send")
        self._observe_sender(event, trace, "send_streaming")
        def clear_event():
            if event.get_extra(TRACE_KEY) is trace:
                event.set_extra(TRACE_KEY, None)
        trace.restorers.append(clear_event)
        trace.restorers.append(lambda: self.clear_memory_handoff(event, trace.handoff_request, trace))
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

    def exit(self, event, reason):
        trace = self.trace(event)
        if trace:
            trace.exit_reason = reason if reason in EXIT_REASONS else "filtered"

    def outcome(self, event, default):
        trace = self.trace(event)
        return trace.exit_reason if trace and trace.exit_reason else default

    def formal_request(self, event, *, ready=False):
        trace = self.trace(event)
        if trace and not trace.formal_started:
            trace.formal_started = True
            trace.begin("formal_total")
        if trace and ready:
            trace.point("formal_request_ready")

    def request_memory_handoff(self, event, req):
        trace = self.trace(event)
        if trace and trace.formal_started and self.enabled(event, "flow_memory_budget_enabled"):
            if trace.handoff_request is not req:
                self.clear_memory_handoff(event, trace.handoff_request, trace)
            trace.handoff_request = req
            event.set_extra(HANDOFF_REQUESTED, req)
            event.set_extra(HANDOFF_OWNER, trace)

    def clear_memory_handoff(self, event, req, trace):
        if req is None or trace is None or event.get_extra(HANDOFF_OWNER) is not trace:
            return
        if event.get_extra(HANDOFF_REQUESTED) is req:
            event.set_extra(HANDOFF_REQUESTED, None)
        handoff = event.get_extra(HANDOFF_RESULT)
        if isinstance(handoff, dict) and handoff.get("request") is req:
            event.set_extra(HANDOFF_RESULT, None)
        if not event.get_extra(HANDOFF_REQUESTED) and not event.get_extra(HANDOFF_RESULT):
            event.set_extra(HANDOFF_OWNER, None)

    def prepare_request(self, event, req, persona_id=""):
        trace = self.trace(event)
        try:
            return self._prepare_request(event, req, persona_id)
        finally:
            self.clear_memory_handoff(event, req, trace)

    def _prepare_request(self, event, req, persona_id=""):
        trace = self.trace(event)
        if not trace or trace.request_prepared:
            return
        trace.request_prepared = True
        trace.loop_request = req
        release = self.loop_release
        if callable(release):
            trace.restorers.append(lambda: release(event, req))
        persona_id = str(persona_id or "").strip()
        valid_persona = bool(persona_id and persona_id != "[%None]")
        query = str(event.get_message_str() or "")
        budget = _milliseconds(self.config.get("flow_memory_budget_chars", 2400), 2400, 800, 12000)
        select = valid_persona and self.enabled(event, "flow_memory_budget_enabled")
        target = req if select else SimpleNamespace(extra_user_content_parts=list(getattr(req, "extra_user_content_parts", []) or []))
        handoff = event.get_extra(HANDOFF_RESULT) if select else None
        if handoff is not None:
            stats = select_native_memory_parts(target, budget_chars=budget, query=query, handoff=handoff)
        else:
            stats = select_native_memory_parts(target, budget_chars=budget if select else 100000000, query=query)
        reason = stats["reason"] if select else ("persona_missing" if self.enabled(event) and not valid_persona else "observe_only")
        trace.point("memory_budget", reason, counts={
            "before_chars": stats["before_chars"],
            "after_chars": stats["after_chars"] if select else stats["before_chars"],
            "retained_entries": stats["retained_entries"], "budget_chars": budget,
            "entry_count_known": stats.get("entry_count_known", 0),
        })
        if not self.enabled(event, "flow_continuity_enabled"):
            return
        sender = str(event.get_sender_id() or "").strip()
        if not valid_persona or not sender:
            trace.point("continuity_context", "persona_missing" if not valid_persona else "sender_missing")
            return
        key = (str(event.unified_msg_origin), sender, persona_id)
        previous = self.turn_cache.get(*key)
        if (int(previous.get("request_order", "0")) > trace.order
                or self.clock() - trace.started >= self.turn_cache.ttl_seconds
                or trace.order <= self.reset_orders.get(key[0], 0)):
            trace.point("continuity_context", "superseded")
            return
        # On-demand observation only: a follow-up may reveal that the current
        # pending tool has exceeded the stale threshold. Never resurrect an
        # expired/evicted record, or update state owned by a newer request.
        if previous:
            for prior in tuple(self.active):
                if (prior is not trace and prior.turn_key == key
                        and previous.get("last_request_id") == prior.request_id):
                    self._check_tool_progress(prior)
            previous = self.turn_cache.get(*key)
        text = build_turn_context({key: previous[key] for key in (
            "topic", "pending_request", "unanswered_question", "task_request", "task_state", "answered"
        ) if key in previous}, query)
        if text:
            factory = self.part_factory
            if factory is None:
                from astrbot.core.agent.message import TextPart
                factory = TextPart
            part = factory(text=text)
            part.mark_as_temp()
            req.extra_user_content_parts.append(part)
        trace.turn_key = key
        trace.point("continuity_context", "attached" if text else "empty", counts={"context_chars": len(text)})
        if query.strip():
            excerpt = query.strip()[:256]
            trace.user_excerpt = excerpt
            self.turn_cache.update(*key, {"topic": excerpt, "pending_request": excerpt,
                "unanswered_question": excerpt if any(mark in query for mark in ("?", "？")) else None,
                "last_request_id": trace.request_id, "request_order": str(trace.order),
                "tool_state": None, "answered": None})

    def _tool_signal(self, trace, signal, seq=None, *, starting=False):
        reason = signal.reason if signal.reason in TOOL_SIGNALS else "unrecognized"
        trace.point("tool_progress", {"success": "returned", "duplicate_success": "duplicate_returned"}.get(reason, reason), tool_seq=seq,
                    counts={"attempts": signal.attempts, "stalled": int(signal.stalled)})
        if not trace.turn_key:
            return
        state = self.turn_cache.get(*trace.turn_key)
        if not state:
            return
        labels = {
            "started": "工具已开始，尚未观测到结果", "pending": "尚未观测到工具结果",
            "stale_pending": "等待已超过观测阈值，尚未观测到结果，原因未知",
            "success": "工具已返回，未标记错误", "failed": "工具返回并标记错误",
            "unknown": "工具结果状态未知", "unrecognized": "工具结果状态未知",
            "duplicate_pending": "同参数重复调用，仍有结果尚未观测到",
            "duplicate_success": "同参数重复调用，先前返回未标记错误；不代表结果相同",
            "retry_after_failure": "同请求中在失败后再次调用，结果未定",
            "uncertain_repeat": "同参数重复调用，先前结果状态未知",
            "repeated_result": "相同参数与相同结果指纹重复",
        }
        if starting:
            if int(state.get("task_order", "0")) > trace.order:
                return
            self.turn_cache.update(*trace.turn_key, {
                "task_request_id": trace.request_id, "task_order": str(trace.order),
                "task_call_seq": str(seq), "task_request": getattr(trace, "user_excerpt", ""),
                "task_state": labels.get(reason, "工具结果状态未知"),
            })
        elif (state.get("task_request_id") == trace.request_id
              and state.get("task_call_seq") == str(seq)):
            self.turn_cache.update(*trace.turn_key, {"task_state": labels.get(reason, "工具结果状态未知")})

    def _check_tool_progress(self, trace):
        if trace.tool_progress:
            for _, seq, _ in trace.tools:
                signal = trace.tool_progress.check(str(seq))
                if signal.stalled:
                    self._tool_signal(trace, signal, seq)

    def tool_start(self, event, tool, tool_args=None):
        trace = self.trace(event)
        if trace and trace.formal_started:
            self._check_tool_progress(trace)
            trace.tool_seq += 1
            trace.tools.append((id(tool), trace.tool_seq, self.clock()))
            trace.point("tool_start", tool_seq=trace.tool_seq)
            if trace.tool_progress:
                signal = trace.tool_progress.begin(str(trace.tool_seq), str(getattr(tool, "name", "") or ""), tool_args)
                self._tool_signal(trace, signal, trace.tool_seq, starting=True)

    def tool_end(self, event, tool, result):
        trace = self.trace(event)
        if not trace or not trace.formal_started:
            return
        for index, (identity, seq, started) in enumerate(trace.tools):
            if identity == id(tool):
                trace.tools.pop(index)
                trace.point("tool_end", "failed" if getattr(result, "isError", False) else "ok",
                            duration_ms=(self.clock() - started) * 1000, tool_seq=seq)
                if trace.tool_progress:
                    status = "unknown" if result is None else "failed" if getattr(result, "isError", False) else "success"
                    self._tool_signal(trace, trace.tool_progress.finish(str(seq), status), seq)
                break

    def agent_done(self, event, response=None):
        trace = self.trace(event)
        if trace:
            trace.agent_done_seen = True
            trace.failed = getattr(response, "role", None) == "err"
            self._check_tool_progress(trace)
            if trace.tool_progress:
                for _, seq, _ in trace.tools:
                    self._tool_signal(trace, trace.tool_progress.finish(str(seq), "unknown"), seq)
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
                       trace.exit_reason if trace.exit_reason else
                       "agent_done" if trace.agent_done_seen else
                       "incomplete_agent" if trace.formal_started else "filtered")
        self._check_tool_progress(trace)
        if trace.tool_progress:
            for _, seq, _ in trace.tools:
                self._tool_signal(trace, trace.tool_progress.finish(str(seq), "unknown"), seq)
        if (trace.turn_key and trace.final_send_count and not trace.failed and not trace.send_failed
                and self.turn_cache.get(*trace.turn_key).get("last_request_id") == trace.request_id):
            self.turn_cache.update(*trace.turn_key, {"pending_request": None, "unanswered_question": None, "answered": "yes"})
        trace.finish(outcome)
        self.active.discard(trace)
        if event.get_extra(TRACE_KEY) is trace:
            event.set_extra(TRACE_KEY, None)

    def close(self):
        self.closed = True
        for trace in tuple(self.active):
            trace.finish("terminated")
        self.active.clear()
        self.turn_cache.clear()
        self.reset_orders.clear()

    def reset_scope(self, umo):
        if umo in self.config.get("flow_optimization_sessions", []):
            self.turn_cache.clear_scope(umo)
            self.reset_orders[umo] = self.sequence
