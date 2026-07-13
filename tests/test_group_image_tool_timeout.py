import asyncio
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "utils" / "tool_timeout_override.py"


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
