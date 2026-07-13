import asyncio
import importlib.util
from pathlib import Path
import sys
from threading import RLock
from types import SimpleNamespace
import unittest
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "utils" / "tool_timeout_override.py"
MATOI_MODULE_PATH = (
    REPO_ROOT.parent
    / "astrbot_plugin_pso2_matoi_cc"
    / "utils"
    / "tool_timeout_override.py"
)
GCP_STATE_ATTR = "_gcp_image_tool_timeout_override_state"
GCP_LOCK_ATTR = "_gcp_image_tool_timeout_override_lock"
MATOI_STATE_ATTR = "_matoi_image_tool_timeout_override_state"
MATOI_LOCK_ATTR = "_matoi_image_tool_timeout_override_lock"


def load_module(module_name="gcp_tool_timeout_override_test_module"):
    if not MODULE_PATH.is_file():
        raise AssertionError("utils/tool_timeout_override.py is missing")
    spec = importlib.util.spec_from_file_location(
        module_name, MODULE_PATH
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def make_compatible_matoi_module():
    image_tool_names = frozenset(
        {"matoi_step_image_generate", "matoi_step_image_edit"}
    )

    def install_image_tool_timeout_override(timeout_seconds, executor_cls):
        timeout = float(timeout_seconds)
        if timeout <= 0:
            raise ValueError("timeout_seconds must be positive")
        normalized_timeout = int(timeout) if timeout.is_integer() else timeout
        token = object()

        lock = executor_cls.__dict__.get(MATOI_LOCK_ATTR)
        if lock is None:
            lock = RLock()
            setattr(executor_cls, MATOI_LOCK_ATTR, lock)

        with lock:
            state = executor_cls.__dict__.get(MATOI_STATE_ATTR)
            if state is None:
                original_descriptor = executor_cls.__dict__.get(
                    "_execute_local"
                )
                if not isinstance(original_descriptor, classmethod):
                    raise TypeError(
                        "executor _execute_local must be a classmethod"
                    )
                state = {
                    "original_descriptor": original_descriptor,
                    "wrapper_descriptor": None,
                    "timeouts": {},
                    "lock": lock,
                }

                async def execute_local_with_image_timeout(
                    cls,
                    tool,
                    run_context,
                    *,
                    tool_call_timeout=None,
                    **tool_args,
                ):
                    effective_timeout = tool_call_timeout
                    current_state = cls.__dict__.get(MATOI_STATE_ATTR)
                    registered_timeout = None
                    if isinstance(current_state, dict):
                        state_lock = current_state["lock"]
                        with state_lock:
                            if (
                                getattr(tool, "name", None)
                                in image_tool_names
                            ):
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
                                delattr(cls, MATOI_STATE_ATTR)
                                if (
                                    cls.__dict__.get(MATOI_LOCK_ATTR)
                                    is state_lock
                                ):
                                    delattr(cls, MATOI_LOCK_ATTR)

                    if registered_timeout is not None:
                        effective_timeout = (
                            registered_timeout
                            if tool_call_timeout is None
                            else max(tool_call_timeout, registered_timeout)
                        )
                    original_method = original_descriptor.__get__(None, cls)
                    async for result in original_method(
                        tool,
                        run_context,
                        tool_call_timeout=effective_timeout,
                        **tool_args,
                    ):
                        yield result

                wrapper_descriptor = classmethod(
                    execute_local_with_image_timeout
                )
                state["wrapper_descriptor"] = wrapper_descriptor
                setattr(executor_cls, MATOI_STATE_ATTR, state)
                setattr(executor_cls, "_execute_local", wrapper_descriptor)

            state["timeouts"][token] = normalized_timeout

        return SimpleNamespace(executor_cls=executor_cls, token=token)

    def remove_image_tool_timeout_override(handle):
        lock = handle.executor_cls.__dict__.get(MATOI_LOCK_ATTR)
        if lock is None:
            return
        with lock:
            state = handle.executor_cls.__dict__.get(MATOI_STATE_ATTR)
            if not isinstance(state, dict):
                return
            state["timeouts"].pop(handle.token, None)
            if state["timeouts"]:
                return
            if (
                handle.executor_cls.__dict__.get("_execute_local")
                is state["wrapper_descriptor"]
            ):
                setattr(
                    handle.executor_cls,
                    "_execute_local",
                    state["original_descriptor"],
                )
                delattr(handle.executor_cls, MATOI_STATE_ATTR)
                if handle.executor_cls.__dict__.get(MATOI_LOCK_ATTR) is lock:
                    delattr(handle.executor_cls, MATOI_LOCK_ATTR)

    return SimpleNamespace(
        install_image_tool_timeout_override=(
            install_image_tool_timeout_override
        ),
        remove_image_tool_timeout_override=remove_image_tool_timeout_override,
    )


def load_live_matoi_module():
    if not MATOI_MODULE_PATH.is_file():
        return None
    spec = importlib.util.spec_from_file_location(
        "matoi_tool_timeout_override_test_module",
        MATOI_MODULE_PATH,
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def iter_matoi_modules():
    yield "bundled", make_compatible_matoi_module()
    live_module = load_live_matoi_module()
    if live_module is not None:
        yield "live", live_module


def make_executor():
    class FakeExecutor:
        @classmethod
        async def _execute_local(
            cls,
            tool,
            run_context,
            *,
            tool_call_timeout=None,
            **tool_args,
        ):
            yield (
                tool_call_timeout
                if tool_call_timeout is not None
                else run_context.tool_call_timeout
            )

    return FakeExecutor


async def collect_timeout(
    executor,
    tool_name,
    context_timeout=180,
    explicit_timeout=None,
):
    values = []
    async for value in executor._execute_local(
        SimpleNamespace(name=tool_name),
        SimpleNamespace(tool_call_timeout=context_timeout),
        tool_call_timeout=explicit_timeout,
    ):
        values.append(value)
    return values


class GroupImageToolTimeoutTest(unittest.TestCase):
    def assert_timeout_override_state_removed(self, executor, original):
        self.assertIs(executor.__dict__["_execute_local"], original)
        for attribute_name in (
            GCP_STATE_ATTR,
            GCP_LOCK_ATTR,
            MATOI_STATE_ATTR,
            MATOI_LOCK_ATTR,
        ):
            self.assertNotIn(attribute_name, executor.__dict__)

    def test_group_image_tools_receive_registered_timeout(self):
        module = load_module()
        executor = make_executor()
        handle = module.install_group_image_tool_timeout_override(300, executor)
        try:
            for tool_name in (
                "gcp_step_image_generate",
                "gcp_step_image_edit",
            ):
                self.assertEqual(
                    asyncio.run(collect_timeout(executor, tool_name)),
                    [300],
                )
        finally:
            module.remove_group_image_tool_timeout_override(handle)

    def test_unrelated_tool_keeps_context_timeout(self):
        module = load_module()
        executor = make_executor()
        handle = module.install_group_image_tool_timeout_override(300, executor)
        try:
            self.assertEqual(
                asyncio.run(collect_timeout(executor, "web_search")),
                [180],
            )
        finally:
            module.remove_group_image_tool_timeout_override(handle)

    def test_longer_explicit_timeout_is_preserved(self):
        module = load_module()
        executor = make_executor()
        handle = module.install_group_image_tool_timeout_override(300, executor)
        try:
            self.assertEqual(
                asyncio.run(
                    collect_timeout(
                        executor,
                        "gcp_step_image_generate",
                        explicit_timeout=420,
                    )
                ),
                [420],
            )
        finally:
            module.remove_group_image_tool_timeout_override(handle)

    def test_multiple_registrations_use_largest_timeout_and_restore(self):
        module = load_module()
        executor = make_executor()
        original = executor.__dict__["_execute_local"]
        first = module.install_group_image_tool_timeout_override(240, executor)
        second = module.install_group_image_tool_timeout_override(300, executor)
        self.assertEqual(
            asyncio.run(collect_timeout(executor, "gcp_step_image_generate")),
            [300],
        )
        module.remove_group_image_tool_timeout_override(second)
        self.assertEqual(
            asyncio.run(collect_timeout(executor, "gcp_step_image_generate")),
            [240],
        )
        module.remove_group_image_tool_timeout_override(first)
        self.assertIs(executor.__dict__["_execute_local"], original)

    def test_removal_restores_preexisting_wrapper(self):
        module = load_module()
        executor = make_executor()
        original = executor.__dict__["_execute_local"]

        async def existing_wrapper(
            cls,
            tool,
            run_context,
            *,
            tool_call_timeout=None,
            **tool_args,
        ):
            original_method = original.__get__(None, cls)
            async for value in original_method(
                tool,
                run_context,
                tool_call_timeout=tool_call_timeout,
                **tool_args,
            ):
                yield value

        existing_descriptor = classmethod(existing_wrapper)
        setattr(executor, "_execute_local", existing_descriptor)
        handle = module.install_group_image_tool_timeout_override(300, executor)
        module.remove_group_image_tool_timeout_override(handle)
        self.assertIs(executor.__dict__["_execute_local"], existing_descriptor)

    def test_later_wrapper_can_be_removed_before_or_after_override(self):
        module = load_module()
        executor = make_executor()
        original = executor.__dict__["_execute_local"]
        handle = module.install_group_image_tool_timeout_override(300, executor)
        timeout_descriptor = executor.__dict__["_execute_local"]

        async def later_wrapper(
            cls,
            tool,
            run_context,
            *,
            tool_call_timeout=None,
            **tool_args,
        ):
            wrapped_method = timeout_descriptor.__get__(None, cls)
            async for value in wrapped_method(
                tool,
                run_context,
                tool_call_timeout=tool_call_timeout,
                **tool_args,
            ):
                yield value

        later_descriptor = classmethod(later_wrapper)
        setattr(executor, "_execute_local", later_descriptor)
        module.remove_group_image_tool_timeout_override(handle)
        self.assertIs(executor.__dict__["_execute_local"], later_descriptor)
        self.assertEqual(
            asyncio.run(collect_timeout(executor, "gcp_step_image_generate")),
            [180],
        )

        setattr(executor, "_execute_local", timeout_descriptor)
        self.assertEqual(
            asyncio.run(collect_timeout(executor, "gcp_step_image_generate")),
            [180],
        )
        self.assertIs(executor.__dict__["_execute_local"], original)

        second_handle = module.install_group_image_tool_timeout_override(
            300, executor
        )
        second_timeout_descriptor = executor.__dict__["_execute_local"]
        setattr(executor, "_execute_local", later_descriptor)
        setattr(executor, "_execute_local", second_timeout_descriptor)
        module.remove_group_image_tool_timeout_override(second_handle)
        self.assertIs(executor.__dict__["_execute_local"], original)

    def test_gcp_inner_matoi_outer_restore_eagerly(self):
        for implementation, matoi_module in iter_matoi_modules():
            with self.subTest(implementation=implementation):
                module = load_module()
                executor = make_executor()
                original = executor.__dict__["_execute_local"]

                gcp_handle = module.install_group_image_tool_timeout_override(
                    300, executor
                )
                matoi_handle = (
                    matoi_module.install_image_tool_timeout_override(
                        300, executor
                    )
                )
                module.remove_group_image_tool_timeout_override(gcp_handle)
                matoi_module.remove_image_tool_timeout_override(matoi_handle)

                self.assert_timeout_override_state_removed(executor, original)

    def test_matoi_inner_gcp_outer_restore_eagerly(self):
        for implementation, matoi_module in iter_matoi_modules():
            with self.subTest(implementation=implementation):
                module = load_module()
                executor = make_executor()
                original = executor.__dict__["_execute_local"]

                matoi_handle = (
                    matoi_module.install_image_tool_timeout_override(
                        300, executor
                    )
                )
                gcp_handle = module.install_group_image_tool_timeout_override(
                    300, executor
                )
                matoi_module.remove_image_tool_timeout_override(matoi_handle)
                module.remove_group_image_tool_timeout_override(gcp_handle)

                self.assert_timeout_override_state_removed(executor, original)

    def test_separate_module_instances_share_hot_reload_state(self):
        first_module = load_module("gcp_timeout_module_before_reload")
        second_module = load_module("gcp_timeout_module_after_reload")
        executor = make_executor()
        original = executor.__dict__["_execute_local"]
        first = first_module.install_group_image_tool_timeout_override(
            240, executor
        )
        second = second_module.install_group_image_tool_timeout_override(
            300, executor
        )
        self.assertEqual(
            asyncio.run(collect_timeout(executor, "gcp_step_image_generate")),
            [300],
        )
        first_module.remove_group_image_tool_timeout_override(first)
        self.assertEqual(
            asyncio.run(collect_timeout(executor, "gcp_step_image_generate")),
            [300],
        )
        second_module.remove_group_image_tool_timeout_override(second)
        self.assertIs(executor.__dict__["_execute_local"], original)

    def test_backend_timeout_resolution(self):
        module = load_module()
        self.assertEqual(
            module.resolve_group_image_tool_timeout(
                {
                    "image_tool_backend": "codex_oauth",
                    "codex_oauth_image_timeout": 300,
                }
            ),
            300,
        )
        self.assertEqual(
            module.resolve_group_image_tool_timeout(
                {
                    "image_tool_backend": "stepfun",
                    "step_image_timeout": 60,
                }
            ),
            60,
        )
        for config in (
            {"image_tool_backend": "unknown"},
            {
                "image_tool_backend": "codex_oauth",
                "codex_oauth_image_timeout": 0,
            },
            {
                "image_tool_backend": "codex_oauth",
                "codex_oauth_image_timeout": float("nan"),
            },
        ):
            with self.subTest(config=config):
                with self.assertRaises(ValueError):
                    module.resolve_group_image_tool_timeout(config)

    def test_default_executor_resolution_prefers_current_core_class(self):
        module = load_module()
        executor = make_executor()
        imported_module = SimpleNamespace(FunctionToolExecutor=executor)
        with patch.object(
            module.importlib,
            "import_module",
            return_value=imported_module,
        ):
            self.assertIs(module._default_executor_cls(), executor)


if __name__ == "__main__":
    unittest.main()
