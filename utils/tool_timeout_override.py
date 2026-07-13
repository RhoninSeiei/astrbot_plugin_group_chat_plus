"""Scoped AstrBot tool timeout override for Group Chat Plus image tools."""

from dataclasses import dataclass
import importlib
import math
from threading import RLock
from typing import Any, Mapping


GROUP_IMAGE_TOOL_NAMES = frozenset(
    {"gcp_step_image_generate", "gcp_step_image_edit"}
)
_STATE_ATTR = "_gcp_image_tool_timeout_override_state"
_LOCK_ATTR = "_gcp_image_tool_timeout_override_lock"
_TIMEOUT_STATE_KEYS = frozenset(
    {"original_descriptor", "wrapper_descriptor", "timeouts", "lock"}
)


@dataclass(frozen=True)
class ToolTimeoutOverrideHandle:
    executor_cls: type
    token: object


def _compatible_timeout_states(
    executor_cls: type,
    excluded_state: dict[str, Any] | None = None,
):
    for attribute_name, candidate in tuple(executor_cls.__dict__.items()):
        if candidate is excluded_state or not isinstance(candidate, dict):
            continue
        if _TIMEOUT_STATE_KEYS.issubset(candidate):
            yield attribute_name, candidate


def _state_for_wrapper(
    executor_cls: type,
    wrapper_descriptor: Any,
    excluded_state: dict[str, Any],
):
    for attribute_name, state in _compatible_timeout_states(
        executor_cls, excluded_state
    ):
        if state["wrapper_descriptor"] is wrapper_descriptor:
            return attribute_name, state
    return None


def _delete_timeout_state(
    executor_cls: type,
    state_attribute: str,
    state: dict[str, Any],
) -> None:
    if executor_cls.__dict__.get(state_attribute) is not state:
        return
    delattr(executor_cls, state_attribute)
    state_lock = state["lock"]
    if any(
        candidate["lock"] is state_lock
        for _, candidate in _compatible_timeout_states(executor_cls)
    ):
        return
    for attribute_name, value in tuple(executor_cls.__dict__.items()):
        if attribute_name.endswith("_lock") and value is state_lock:
            delattr(executor_cls, attribute_name)


def _skip_empty_timeout_wrappers(
    executor_cls: type,
    descriptor: Any,
    excluded_state: dict[str, Any],
):
    seen = set()
    while id(descriptor) not in seen:
        seen.add(id(descriptor))
        match = _state_for_wrapper(
            executor_cls, descriptor, excluded_state
        )
        if match is None:
            break
        state_attribute, state = match
        state_lock = state["lock"]
        with state_lock:
            if executor_cls.__dict__.get(state_attribute) is not state:
                continue
            if state["timeouts"]:
                break
            descriptor = state["original_descriptor"]
            _delete_timeout_state(executor_cls, state_attribute, state)
    return descriptor


def _rewire_outer_timeout_wrapper(
    executor_cls: type,
    current_descriptor: Any,
    removed_descriptor: Any,
    replacement_descriptor: Any,
    excluded_state: dict[str, Any],
) -> bool:
    descriptor = current_descriptor
    seen = set()
    while id(descriptor) not in seen:
        seen.add(id(descriptor))
        match = _state_for_wrapper(
            executor_cls, descriptor, excluded_state
        )
        if match is None:
            return False
        state_attribute, state = match
        state_lock = state["lock"]
        with state_lock:
            if executor_cls.__dict__.get(state_attribute) is not state:
                continue
            original_descriptor = state["original_descriptor"]
            if original_descriptor is removed_descriptor:
                state["original_descriptor"] = replacement_descriptor
                return True
        descriptor = original_descriptor
    return False


def resolve_group_image_tool_timeout(config: Mapping[str, Any]) -> int | float:
    backend = str(config.get("image_tool_backend") or "").strip().lower()
    if backend == "codex_oauth":
        raw_timeout = config.get("codex_oauth_image_timeout")
    elif backend == "stepfun":
        raw_timeout = config.get("step_image_timeout")
    else:
        raise ValueError("unsupported image tool backend")
    timeout = float(raw_timeout)
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("image tool timeout must be a finite positive number")
    return int(timeout) if timeout.is_integer() else timeout


def _default_executor_cls() -> type:
    module = importlib.import_module("astrbot.core.astr_agent_tool_exec")
    executor_cls = getattr(module, "FunctionToolExecutor", None)
    if executor_cls is None:
        executor_cls = getattr(module, "AstrAgentToolExec", None)
    if executor_cls is None:
        raise ImportError("AstrBot function tool executor is unavailable")
    return executor_cls


def install_group_image_tool_timeout_override(
    timeout_seconds: Any,
    executor_cls: type | None = None,
) -> ToolTimeoutOverrideHandle:
    target_cls = executor_cls or _default_executor_cls()
    timeout = float(timeout_seconds)
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout_seconds must be a finite positive number")
    normalized_timeout = int(timeout) if timeout.is_integer() else timeout
    token = object()

    lock = target_cls.__dict__.get(_LOCK_ATTR)
    if lock is None:
        lock = RLock()
        setattr(target_cls, _LOCK_ATTR, lock)

    with lock:
        state = target_cls.__dict__.get(_STATE_ATTR)
        if state is None:
            original_descriptor = target_cls.__dict__.get("_execute_local")
            if not isinstance(original_descriptor, classmethod):
                raise TypeError("executor _execute_local must be a classmethod")
            state = {
                "original_descriptor": original_descriptor,
                "wrapper_descriptor": None,
                "timeouts": {},
                "lock": lock,
            }

            async def execute_local_with_group_image_timeout(
                cls,
                tool,
                run_context,
                *,
                tool_call_timeout=None,
                **tool_args,
            ):
                registered_timeout = None
                current_state = cls.__dict__.get(_STATE_ATTR)
                if isinstance(current_state, dict):
                    state_lock = current_state["lock"]
                    with state_lock:
                        if getattr(tool, "name", None) in GROUP_IMAGE_TOOL_NAMES:
                            registered_timeout = max(
                                current_state["timeouts"].values(),
                                default=None,
                            )
                        if (
                            registered_timeout is None
                            and not current_state["timeouts"]
                            and cls.__dict__.get("_execute_local")
                            is current_state["wrapper_descriptor"]
                        ):
                            setattr(
                                cls,
                                "_execute_local",
                                current_state["original_descriptor"],
                            )
                            delattr(cls, _STATE_ATTR)
                            if cls.__dict__.get(_LOCK_ATTR) is state_lock:
                                delattr(cls, _LOCK_ATTR)

                effective_timeout = tool_call_timeout
                if registered_timeout is not None:
                    if effective_timeout is None:
                        effective_timeout = registered_timeout
                    else:
                        try:
                            effective_timeout = max(
                                effective_timeout,
                                registered_timeout,
                            )
                        except TypeError:
                            effective_timeout = registered_timeout
                original_method = original_descriptor.__get__(None, cls)
                async for result in original_method(
                    tool,
                    run_context,
                    tool_call_timeout=effective_timeout,
                    **tool_args,
                ):
                    yield result

            wrapper_descriptor = classmethod(
                execute_local_with_group_image_timeout
            )
            state["wrapper_descriptor"] = wrapper_descriptor
            setattr(target_cls, _STATE_ATTR, state)
            setattr(target_cls, "_execute_local", wrapper_descriptor)
        elif not isinstance(state, dict) or not {
            "original_descriptor",
            "wrapper_descriptor",
            "timeouts",
            "lock",
        }.issubset(state):
            raise TypeError("executor timeout override state is invalid")

        state["timeouts"][token] = normalized_timeout

    return ToolTimeoutOverrideHandle(target_cls, token)


def remove_group_image_tool_timeout_override(
    handle: ToolTimeoutOverrideHandle,
) -> None:
    lock = handle.executor_cls.__dict__.get(_LOCK_ATTR)
    if lock is None:
        return
    with lock:
        state = handle.executor_cls.__dict__.get(_STATE_ATTR)
        if not isinstance(state, dict):
            return
        state["timeouts"].pop(handle.token, None)
        if state["timeouts"]:
            return

        replacement_descriptor = _skip_empty_timeout_wrappers(
            handle.executor_cls,
            state["original_descriptor"],
            state,
        )
        current_descriptor = handle.executor_cls.__dict__.get(
            "_execute_local"
        )
        collapsed_current = _skip_empty_timeout_wrappers(
            handle.executor_cls,
            current_descriptor,
            state,
        )
        if collapsed_current is not current_descriptor:
            if (
                handle.executor_cls.__dict__.get("_execute_local")
                is current_descriptor
            ):
                setattr(
                    handle.executor_cls,
                    "_execute_local",
                    collapsed_current,
                )
            current_descriptor = handle.executor_cls.__dict__.get(
                "_execute_local"
            )

        if current_descriptor is state["wrapper_descriptor"]:
            setattr(
                handle.executor_cls,
                "_execute_local",
                replacement_descriptor,
            )
            _delete_timeout_state(handle.executor_cls, _STATE_ATTR, state)
            return

        if _rewire_outer_timeout_wrapper(
            handle.executor_cls,
            current_descriptor,
            state["wrapper_descriptor"],
            replacement_descriptor,
            state,
        ):
            _delete_timeout_state(handle.executor_cls, _STATE_ATTR, state)
