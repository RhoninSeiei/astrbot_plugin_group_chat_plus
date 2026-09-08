import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest


def load_module():
    path = Path(__file__).resolve().parents[1] / "utils" / "image_planner_context.py"
    spec = importlib.util.spec_from_file_location("gcp_planner_context_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ImagePlannerContextTest(unittest.IsolatedAsyncioTestCase):
    async def test_actual_provider_is_scoped_to_event_and_each_async_resume(self):
        module = load_module()
        event = object()
        provider = object()
        seen = []

        class Runner:
            async def _handle_function_tools(self, req, response):
                seen.append(handle.provider_for(event))
                yield 1
                await asyncio.sleep(0)
                seen.append(handle.provider_for(event))
                yield 2

        original = Runner._handle_function_tools
        handle = module.install_image_planner_context(Runner)
        runner = Runner()
        runner.provider = provider
        runner.run_context = SimpleNamespace(context=SimpleNamespace(event=event))
        response = SimpleNamespace(tools_call_name=["gcp_step_image_generate"])
        stream = runner._handle_function_tools(None, response)
        self.assertEqual(await asyncio.create_task(anext(stream)), 1)
        self.assertIsNone(handle.provider_for(event))
        self.assertEqual(await asyncio.create_task(anext(stream)), 2)
        await stream.aclose()
        self.assertEqual(seen, [provider, provider])
        handle.close()
        self.assertIs(Runner._handle_function_tools, original)

    async def test_parallel_models_and_fallback_use_actual_runner_provider(self):
        module = load_module()
        barrier = asyncio.Event()
        seen = []

        class Runner:
            async def _handle_function_tools(self, req, response):
                await barrier.wait()
                event = self.run_context.context.event
                seen.append((event, handle.provider_for(event)))
                self.assert_other_event_is_unbound(event)
                yield 1

        handle = module.install_image_planner_context(Runner)
        events, providers = [object(), object()], [object(), object()]
        async def run(index):
            runner = Runner()
            runner.provider = providers[1 - index]  # The selected fallback, not default.
            runner.run_context = SimpleNamespace(context=SimpleNamespace(event=events[index]))
            runner.assert_other_event_is_unbound = lambda event: self.assertIsNone(handle.provider_for(events[1 - index]))
            async for _ in runner._handle_function_tools(None, SimpleNamespace(tools_call_name=["gcp_step_image_edit"])):
                pass
        tasks = [asyncio.create_task(run(i)) for i in range(2)]
        barrier.set()
        await asyncio.gather(*tasks)
        self.assertCountEqual(seen, [(events[0], providers[1]), (events[1], providers[0])])
        handle.close()

    async def test_unrelated_tools_and_closed_instance_have_no_identity(self):
        module = load_module()
        event = object()
        seen = []
        class Runner:
            async def _handle_function_tools(self, req, response):
                seen.append(handle.provider_for(event))
                yield 1
        handle = module.install_image_planner_context(Runner)
        runner = Runner()
        runner.provider = object()
        runner.run_context = SimpleNamespace(context=SimpleNamespace(event=event))
        async for _ in runner._handle_function_tools(None, SimpleNamespace(tools_call_name=["other_plugin_tool"])):
            pass
        stream = runner._handle_function_tools(None, SimpleNamespace(tools_call_name=["gcp_step_image_generate"]))
        await anext(stream)
        handle.close()
        self.assertIsNone(handle.provider_for(event))
        await stream.aclose()
        self.assertEqual(seen[0], None)

    async def test_new_instance_does_not_inherit_old_inflight_identity(self):
        module = load_module()
        event = object()
        seen = []
        class Runner:
            async def _handle_function_tools(self, req, response):
                yield 1
                seen.append(new_handle.provider_for(event))
                yield 2
        old_handle = module.install_image_planner_context(Runner)
        runner = Runner()
        runner.provider = object()
        runner.run_context = SimpleNamespace(context=SimpleNamespace(event=event))
        stream = runner._handle_function_tools(None, SimpleNamespace(tools_call_name=["gcp_step_image_edit"]))
        await anext(stream)
        new_handle = module.install_image_planner_context(Runner)
        old_handle.close()
        await anext(stream)
        await stream.aclose()
        self.assertEqual(seen, [None])
        new_handle.close()

    async def test_error_restores_context_and_preserves_later_wrapper(self):
        module = load_module()
        event = object()
        class Runner:
            async def _handle_function_tools(self, req, response):
                self.assert_context()
                raise ValueError("planned failure")
                yield
        handle = module.install_image_planner_context(Runner)
        runner = Runner()
        runner.provider = object()
        runner.run_context = SimpleNamespace(context=SimpleNamespace(event=event))
        runner.assert_context = lambda: self.assertIs(handle.provider_for(event), runner.provider)
        stream = runner._handle_function_tools(None, SimpleNamespace(tools_call_name=["gcp_step_image_generate"]))
        with self.assertRaisesRegex(ValueError, "planned failure"):
            await anext(stream)
        self.assertIsNone(handle.provider_for(event))
        original_wrapper = Runner._handle_function_tools
        async def other_wrapper(self, req, response):
            async for item in original_wrapper(self, req, response):
                yield item
        Runner._handle_function_tools = other_wrapper
        handle.close()
        self.assertIs(Runner._handle_function_tools, other_wrapper)


if __name__ == "__main__":
    unittest.main()
