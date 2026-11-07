"""Fail-closed native queue capability and request-local history refresh.

This module neither queues messages nor reads storage. The caller owns the exact
opt-in scope, native-runner selection and authoritative, cutoff-filtered reader.
No module-global request, history, or reset state is retained.
"""
from __future__ import annotations

import ast
import asyncio
import copy
from dataclasses import dataclass, field
import importlib
import inspect
import textwrap
from typing import Callable


@dataclass(frozen=True)
class NativeQueueCapability:
    supported: bool
    reason: str


def inspect_native_queue_source(source: str) -> NativeQueueCapability:
    """Check the actual internal stage implementation, never a version string.

The UMO lock must lexically enclose agent construction, the request hook and
history save, in that order. Unknown source layouts retain the legacy path.
"""
    try:
        tree = ast.parse(textwrap.dedent(source))
        for node in ast.walk(tree):
            if not isinstance(node, ast.AsyncWith):
                continue
            locked = any(
                isinstance(item.context_expr, ast.Call)
                and ast.unparse(item.context_expr.func) == "session_lock_manager.acquire_lock"
                and len(item.context_expr.args) == 1
                and ast.unparse(item.context_expr.args[0]) == "event.unified_msg_origin"
                for item in node.items
            )
            if not locked:
                continue
            calls = [value for statement in node.body for value in ast.walk(statement)
                     if isinstance(value, ast.Call)]
            builds = [call.lineno for call in calls if ast.unparse(call.func) == "build_main_agent"]
            hooks = [call.lineno for call in calls
                     if ast.unparse(call.func) == "call_event_hook"
                     and len(call.args) >= 2
                     and ast.unparse(call.args[0]) == "event"
                     and ast.unparse(call.args[1]) == "EventType.OnLLMRequestEvent"]
            saves = [call.lineno for call in calls if ast.unparse(call.func) == "self._save_to_history"]
            if any(build < hook < save for build in builds for hook in hooks for save in saves):
                return NativeQueueCapability(True, "verified_umo_lock_and_request_hook")
        return NativeQueueCapability(False, "unverified_native_lock_order")
    except (SyntaxError, TypeError, ValueError, RecursionError):
        return NativeQueueCapability(False, "unavailable_core_source")


def detect_native_queue_capability(stage_module=None) -> NativeQueueCapability:
    """Inspect the loaded native internal stage and its actual lock binding."""
    try:
        if stage_module is None:
            stage_module = importlib.import_module(
                "astrbot.core.pipeline.process_stage.method.agent_sub_stages.internal"
            )
        process = inspect.unwrap(stage_module.InternalAgentSubStage.process)
        lock = stage_module.session_lock_manager
        if (not callable(getattr(lock, "acquire_lock", None))
                or getattr(process, "__globals__", {}).get("session_lock_manager") is not lock):
            return NativeQueueCapability(False, "unavailable_native_lock")
        return inspect_native_queue_source(inspect.getsource(process))
    except (ImportError, AttributeError, OSError, TypeError, ValueError):
        return NativeQueueCapability(False, "unavailable_native_stage")


def can_delegate_native(capability: NativeQueueCapability, *, native_provider_request: bool,
                        direct_provider: bool = False) -> bool:
    """The caller must separately select its existing scope and safe payloads."""
    return (isinstance(capability, NativeQueueCapability) and capability.supported is True
            and native_provider_request is True and direct_provider is False)


@dataclass(frozen=True)
class HistoryRefreshResult:
    status: str
    payload: str = field(repr=False)

    @property
    def changed(self) -> bool:
        return self.status == "refreshed"


class RequestHistoryRecipe:
    """A single event/request's immutable formatter snapshot.

``bind`` is called with the actual ProviderRequest inside the native lock.
``refresh`` returns a replacement for the caller's full plugin prompt; it never
changes req.prompt, leaving short-query recall and other plugins undisturbed.
The reader receives ``(event, exact_umo, original_history_copy)`` and must return
the final authoritative history list, including its own ID/cutoff/max filtering.
No content-based deduplication is performed here.
"""

    @classmethod
    def capture(cls, event, *, history_messages, current_message, bot_id,
                rendered_fragment, formatter, reset_token,
                window_buffered_messages=None, include_timestamp=True,
                include_sender_info=True):
        recipe = cls()
        recipe._event = event
        recipe._request = None
        recipe._umo = getattr(event, "unified_msg_origin", None)
        recipe._fragment = rendered_fragment
        recipe._formatter = formatter
        recipe._capture_error = None
        recipe._refreshing = False
        recipe._finished = False
        try:
            recipe._inputs = copy.deepcopy({
                "history_messages": history_messages,
                "current_message": current_message,
                "bot_id": bot_id,
                "window_buffered_messages": window_buffered_messages,
                "include_timestamp": include_timestamp,
                "include_sender_info": include_sender_info,
            })
            recipe._reset_token = copy.deepcopy(reset_token)
            if (not isinstance(recipe._umo, str) or not recipe._umo
                    or not isinstance(rendered_fragment, str) or not rendered_fragment
                    or not isinstance(history_messages, list) or not callable(formatter)):
                recipe._capture_error = "invalid_snapshot"
        except Exception:
            recipe._capture_error = "snapshot_failed"
        return recipe

    def _matches_event(self, event):
        return (event is self._event
                and getattr(event, "unified_msg_origin", None) == self._umo)

    def bind(self, event, req) -> bool:
        if self._capture_error or req is None or not self._matches_event(event):
            return False
        if self._request is None:
            self._request = req
        return self._request is req

    async def refresh(self, event, req, payload: str, *, reader: Callable,
                      get_reset_token: Callable) -> HistoryRefreshResult:
        """Refresh once; every unsafe/error branch returns the original payload.

Reset tokens are checked before work and after each await. Cancellation always
propagates, and cannot leave a half-written payload or request mutation.
"""
        result = lambda status: HistoryRefreshResult(status, payload)
        if self._capture_error:
            return result(self._capture_error)
        if self._request is None or req is not self._request or not self._matches_event(event):
            return result("binding_mismatch")
        if self._refreshing or self._finished:
            return result("already_attempted")
        if not isinstance(payload, str) or payload.count(self._fragment) != 1:
            return result("fragment_mismatch")
        self._refreshing = True
        phase = "reset_check_failed"
        try:
            if get_reset_token(self._umo) != self._reset_token:
                return result("reset_changed")
            phase = "format_failed"
            original = await self._formatter(**copy.deepcopy(self._inputs))
            if get_reset_token(self._umo) != self._reset_token:
                return result("reset_changed")
            if not self._matches_event(event):
                return result("binding_mismatch")
            if original != self._fragment:
                return result("original_render_mismatch")
            # Render the unchanged current/window section independently. The
            # native formatter's error fallback is only current_message; it
            # must not silently replace a full history-bearing prompt.
            tail_inputs = copy.deepcopy(self._inputs)
            tail_inputs["history_messages"] = []
            protected_tail = await self._formatter(**tail_inputs)
            if get_reset_token(self._umo) != self._reset_token:
                return result("reset_changed")
            if not self._matches_event(event):
                return result("binding_mismatch")
            if (not isinstance(protected_tail, str) or not protected_tail
                    or protected_tail == self._inputs["current_message"]
                    or not original.endswith(protected_tail)):
                return result("original_render_mismatch")
            phase = "read_failed"
            fresh = await reader(event, self._umo, copy.deepcopy(self._inputs["history_messages"]))
            if get_reset_token(self._umo) != self._reset_token:
                return result("reset_changed")
            if not self._matches_event(event):
                return result("binding_mismatch")
            if not isinstance(fresh, list):
                return result("invalid_history")
            phase = "format_failed"
            inputs = copy.deepcopy(self._inputs)
            inputs["history_messages"] = copy.deepcopy(fresh)
            refreshed = await self._formatter(**inputs)
            if get_reset_token(self._umo) != self._reset_token:
                return result("reset_changed")
            if not self._matches_event(event):
                return result("binding_mismatch")
            if (not isinstance(refreshed, str) or not refreshed
                    or not refreshed.endswith(protected_tail)):
                return result("invalid_render")
            if refreshed == self._fragment:
                return result("unchanged")
            return HistoryRefreshResult("refreshed", payload.replace(self._fragment, refreshed, 1))
        except asyncio.CancelledError:
            raise
        except Exception:
            return result(phase)
        finally:
            self._refreshing = False
            self._finished = True
