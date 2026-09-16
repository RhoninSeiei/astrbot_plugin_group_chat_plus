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
    async def test_active_legacy_handles_survive_upgrade_and_retained_filter(self):
        module = load_module()
        class Runner:
            def _func_tool_for_provider(self):
                return ["grok", "gpt", "search"]
            async def _handle_function_tools(self, req, response):
                yield 1
        class LegacyHandle:
            def __init__(self, state):
                self.active = True
                state["handles"].add(self)
        first = module.install_image_planner_context(Runner)
        state = first.state
        legacy = LegacyHandle(state)
        # Match the already-deployed unsafe wrapper, including an outer hook.
        original_filter = Runner._func_tool_for_provider
        def old_filter(runner):
            selected = original_filter(runner)
            for handle in tuple(state["handles"]):
                if handle.active and handle.tool_filter is not None:
                    selected = handle.tool_filter(None, runner.provider, selected)
            return selected
        state["original_filter"] = original_filter
        state["filter_wrapper"] = old_filter
        Runner._func_tool_for_provider = lambda runner: old_filter(runner)
        first.close()
        replacement = load_module().install_image_planner_context(
            Runner, tool_filter=lambda e, p, t: [n for n in t if n in {p, "search"}]
        )
        runner = Runner()
        runner.provider = "grok"
        self.assertEqual(runner._func_tool_for_provider(), ["grok", "search"])
        self.assertIn(legacy, state["handles"])
        self.assertTrue(legacy.active)
        late_legacy = LegacyHandle(state)
        runner.provider = "gpt"
        self.assertEqual(runner._func_tool_for_provider(), ["gpt", "search"])
        replacement.close()
        self.assertEqual(runner._func_tool_for_provider(), ["grok", "gpt", "search"])
        legacy.active = late_legacy.active = False

    async def test_upgrade_retained_legacy_wrapper_recognizes_new_names(self):
        legacy = load_module()
        legacy._TOOLS = frozenset({"gcp_step_image_generate", "gcp_step_image_edit"})
        event = object()
        seen = []
        class Runner:
            def _func_tool_for_provider(self):
                return ["gcp_grok_image", "gcp_gpt_image"]
            async def _handle_function_tools(self, req, response):
                seen.append(new_handle.provider_for(event))
                yield 1
        old_handle = legacy.install_image_planner_context(Runner)
        previous = Runner._handle_function_tools
        async def outer(self, req, response):
            async for item in previous(self, req, response):
                yield item
        Runner._handle_function_tools = outer
        old_handle.close()
        replacement = load_module()
        new_handle = replacement.install_image_planner_context(Runner, tool_filter=lambda e, p, t: t)
        runner = Runner()
        runner.provider = object()
        runner.run_context = SimpleNamespace(context=SimpleNamespace(event=event))
        async for _ in runner._handle_function_tools(None, SimpleNamespace(tools_call_name=["gcp_grok_image"])):
            pass
        self.assertEqual(seen, [runner.provider])
        self.assertIsNone(old_handle.provider_for(event))
        new_handle.close()

    async def test_reload_preserves_visibility_with_outer_wrapper_on_either_hook(self):
        for outer_hook in ("tools", "filter"):
            module = load_module()
            class Runner:
                def _func_tool_for_provider(self):
                    return ["grok", "gpt"]
                async def _handle_function_tools(self, req, response):
                    yield 1
            handle = module.install_image_planner_context(Runner, tool_filter=lambda e, p, t: [p])
            if outer_hook == "tools":
                previous = Runner._handle_function_tools
                async def outer(self, req, response):
                    async for item in previous(self, req, response):
                        yield item
                Runner._handle_function_tools = outer
            else:
                previous_filter = Runner._func_tool_for_provider
                def outer_filter(self):
                    return previous_filter(self)
                Runner._func_tool_for_provider = outer_filter
            runner = Runner()
            runner.provider = "grok"
            handle.close()
            self.assertEqual(runner._func_tool_for_provider(), ["grok", "gpt"])
            replacement = module.install_image_planner_context(Runner, tool_filter=lambda e, p, t: [p])
            self.assertEqual(runner._func_tool_for_provider(), ["grok"])
            replacement.close()

    async def test_provider_fallback_recomputes_exposure_without_mutating_request(self):
        module = load_module()
        class Runner:
            def _func_tool_for_provider(self):
                return self.req.func_tool
            async def _handle_function_tools(self, req, response):
                yield 1
        original = Runner._func_tool_for_provider
        event = object()
        def filter_tools(ev, provider, tools):
            self.assertIs(ev, event)
            wanted = "gcp_grok_image" if provider == "grok" else "gcp_gpt_image"
            return [name for name in tools if name == wanted or name == "search"]
        handle = module.install_image_planner_context(Runner, tool_filter=filter_tools)
        runner = Runner()
        runner.req = SimpleNamespace(func_tool=["gcp_grok_image", "gcp_gpt_image", "search"])
        runner.run_context = SimpleNamespace(context=SimpleNamespace(event=event))
        runner.provider = "grok"
        self.assertEqual(runner._func_tool_for_provider(), ["gcp_grok_image", "search"])
        runner.provider = "codex"
        self.assertEqual(runner._func_tool_for_provider(), ["gcp_gpt_image", "search"])
        self.assertEqual(len(runner.req.func_tool), 3)
        handle.close()
        self.assertIs(Runner._func_tool_for_provider, original)

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
