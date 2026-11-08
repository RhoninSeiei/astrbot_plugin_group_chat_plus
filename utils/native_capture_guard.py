"""Keep enriched GCP requests on the native lock rather than text-only capture.

Only an exact request authorized by its owning plugin can bypass the native
follow-up capture. Raw messages, runners and core source files are unchanged.
The caller must retain this handle until its in-flight requests have drained.
"""
from __future__ import annotations

import ast
import importlib
import inspect
import textwrap


_BRIDGE_ATTR = "_gcp_native_capture_guard_v1"
_PROTOCOL = "gcp.native-capture-guard.v1"


def _known_stage(stage_module):
    """Require the actual global capture binding before a complete UMO lock."""
    try:
        process = inspect.unwrap(stage_module.InternalAgentSubStage.process)
        namespace = getattr(process, "__globals__", None)
        if namespace is not vars(stage_module):
            return False
        lock = stage_module.session_lock_manager
        if (not callable(getattr(lock, "acquire_lock", None))
                or namespace.get("session_lock_manager") is not lock):
            return False
        tree = ast.parse(textwrap.dedent(inspect.getsource(process)))
        captures = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                    and ast.unparse(node.func) == "try_capture_follow_up"]
        if (len(captures) != 1 or len(captures[0].args) != 1
                or captures[0].keywords
                or ast.unparse(captures[0].args[0]) != "event"):
            return False
        capture = captures[0]
        if not any(isinstance(node, ast.Assign) and node.value is capture
                   and len(node.targets) == 1
                   and isinstance(node.targets[0], ast.Name)
                   and node.targets[0].id == "follow_up_capture"
                   for node in ast.walk(tree)):
            return False
        for node in ast.walk(tree):
            if not isinstance(node, ast.AsyncWith) or capture.lineno >= node.lineno:
                continue
            locked = any(isinstance(item.context_expr, ast.Call)
                         and ast.unparse(item.context_expr.func) == "session_lock_manager.acquire_lock"
                         and len(item.context_expr.args) == 1
                         and ast.unparse(item.context_expr.args[0]) == "event.unified_msg_origin"
                         for item in node.items)
            if not locked:
                continue
            calls = [call for statement in node.body for call in ast.walk(statement)
                     if isinstance(call, ast.Call)]
            builds = [call.lineno for call in calls
                      if ast.unparse(call.func) == "build_main_agent"]
            hooks = [call.lineno for call in calls
                     if ast.unparse(call.func) == "call_event_hook"
                     and len(call.args) >= 2
                     and ast.unparse(call.args[0]) == "event"
                     and ast.unparse(call.args[1]) == "EventType.OnLLMRequestEvent"]
            saves = [call.lineno for call in calls
                     if ast.unparse(call.func) == "self._save_to_history"]
            if any(build < hook < save for build in builds for hook in hooks for save in saves):
                return True
        return False
    except (AttributeError, OSError, TypeError, ValueError, SyntaxError, RecursionError):
        return False


class NativeCaptureGuardHandle:
    def __init__(self, stage_module, authorize, bridge=None):
        self.stage_module = stage_module
        self.authorize = authorize
        self.bridge = bridge
        self.closed = False
        if bridge is not None:
            bridge["handles"].append(self)

    @property
    def supported(self):
        bridge = self.bridge
        return (not self.closed and bridge is not None
                and getattr(self.stage_module, _BRIDGE_ATTR, None) is bridge
                and getattr(self.stage_module, "try_capture_follow_up", None) is bridge["wrapper"])

    def close(self):
        if self.closed:
            return
        self.closed = True
        bridge = self.bridge
        if bridge is None:
            return
        bridge["handles"][:] = [handle for handle in bridge["handles"] if handle is not self]
        if bridge["handles"]:
            return
        # A later plugin may retain our wrapper. Leave it inert and do not
        # overwrite that plugin's binding; a future known binding can reuse it.
        if getattr(self.stage_module, "try_capture_follow_up", None) is bridge["wrapper"]:
            self.stage_module.try_capture_follow_up = bridge["original"]
            if getattr(self.stage_module, _BRIDGE_ATTR, None) is bridge:
                delattr(self.stage_module, _BRIDGE_ATTR)


def install_native_capture_guard(*, authorize, stage_module=None):
    """Install once across import generations; unknown stages remain unchanged.

``authorize(event)`` must return literal True only for the owning active lease
of an enriched request already delegated to the native queue. Authorization
errors propagate before capture, rather than silently discarding that payload.
"""
    if not callable(authorize):
        raise TypeError("request authorization callback is required")
    if stage_module is None:
        try:
            stage_module = importlib.import_module(
                "astrbot.core.pipeline.process_stage.method.agent_sub_stages.internal")
        except ImportError:
            return NativeCaptureGuardHandle(None, authorize)
    if not _known_stage(stage_module):
        return NativeCaptureGuardHandle(stage_module, authorize)
    bridge = getattr(stage_module, _BRIDGE_ATTR, None)
    current = getattr(stage_module, "try_capture_follow_up", None)
    if bridge is not None:
        if (not isinstance(bridge, dict) or bridge.get("protocol") != _PROTOCOL
                or bridge.get("module") is not stage_module
                or not isinstance(bridge.get("handles"), list)
                or not callable(bridge.get("original"))
                or current is not bridge.get("wrapper")):
            return NativeCaptureGuardHandle(stage_module, authorize)
        return NativeCaptureGuardHandle(stage_module, authorize, bridge)
    if not callable(current) or inspect.iscoroutinefunction(current):
        return NativeCaptureGuardHandle(stage_module, authorize)
    try:
        parameters = list(inspect.signature(current).parameters.values())
        if (len(parameters) != 1
                or parameters[0].kind not in (inspect.Parameter.POSITIONAL_ONLY,
                                              inspect.Parameter.POSITIONAL_OR_KEYWORD)):
            return NativeCaptureGuardHandle(stage_module, authorize)
    except (TypeError, ValueError):
        return NativeCaptureGuardHandle(stage_module, authorize)
    bridge = {"protocol": _PROTOCOL, "module": stage_module, "handles": [],
              "original": current}

    def capture(event):
        for handle in reversed(tuple(bridge["handles"])):
            if not handle.closed and handle.authorize(event) is True:
                return None
        return bridge["original"](event)

    bridge["wrapper"] = capture
    stage_module.try_capture_follow_up = capture
    setattr(stage_module, _BRIDGE_ATTR, bridge)
    return NativeCaptureGuardHandle(stage_module, authorize, bridge)
