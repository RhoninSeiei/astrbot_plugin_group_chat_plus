"""Request policy regressions; native runner lifecycle is separately integrated."""
import asyncio
import importlib.util
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace as NS

from mcp.types import CallToolResult, TextContent, ToolAnnotations
from astrbot.core.agent.tool import FunctionTool
from astrbot.core.provider.entities import LLMResponse

SPEC = importlib.util.spec_from_file_location('gcp_tool_loop_limiter_tests',
    Path(__file__).parents[1]/'utils/tool_loop_limiter.py')
limiter = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = limiter
SPEC.loader.exec_module(limiter)


def pending(progress=10, status='pending', error=False, **extra):
    return CallToolResult(isError=error, content=[TextContent(type='text',
        text=json.dumps({'status':status,'progress':progress,**extra}))])


def tool(name='controlled_poll', readonly=True, mcp=False):
    value=FunctionTool(name=name,description='synthetic only',parameters={'type':'object','properties':{}})
    annotations=ToolAnnotations(readOnlyHint=readonly)
    if mcp:value.mcp_tool=NS(annotations=annotations)
    else:value.annotations=annotations
    return value


class Executor:
    def __init__(self, results=None):
        self.results=results or [pending()]
        self.calls=[];self.entered=None;self.resume=None;self.failure=None
    async def execute(self,tool,run_context,**args):
        self.calls.append({'tool':tool,'args':args})
        if self.entered:self.entered.set()
        if self.resume:await self.resume.wait()
        if self.failure:raise self.failure
        yield self.results[min(len(self.calls)-1,len(self.results)-1)]


def runner_class():
    class Runner:
        EMPTY_OUTPUT_RETRY_ATTEMPTS=3
        def __init__(self,event=None,request=None,executor=None,tools=None):
            self.req=request or NS(func_tool=object())
            self.run_context=NS(context=NS(event=event or object()),messages=[])
            self.tool_executor=executor or Executor();self.tools=tools or {'controlled_poll':tool()}
            self.streaming=True;self.request_max_retries=4;self.fallback_providers=[object()]
            self._follow_up_seq=0;self.provider_calls=[];self.provider_result=LLMResponse(role='assistant',completion_text='仍在等待，未取得新的结果。')
            self.provider_error=None;self.on_provider=None;self.final=None;self.done_hooks=0
            self.stop_requested=False
        def _func_tool_for_provider(self):return self.req.func_tool
        def done(self):return self.final is not None
        def _is_stop_requested(self):return self.stop_requested
        async def _iter_llm_responses_with_fallback(self):
            if self.stop_requested:return
            self.provider_calls.append({'tools':self._func_tool_for_provider(),'streaming':self.streaming,
                'retries':self.request_max_retries,'empty_attempts':self.EMPTY_OUTPUT_RETRY_ATTEMPTS,
                'fallbacks':list(self.fallback_providers),'messages':list(self.run_context.messages)})
            if self.on_provider:await self.on_provider()
            if self.provider_error:raise self.provider_error
            yield self.provider_result
        async def _handle_function_tools(self,req,response):
            for name,args in zip(response.tools_call_name,response.tools_call_args):
                async for result in self.tool_executor.execute(tool=self.tools[name],run_context=self.run_context,**args):
                    yield result
        async def final_step(self):
            async for response in self._iter_llm_responses_with_fallback():
                if not response.tools_call_name:
                    self.final=response;self.done_hooks+=1
            return self.final
    return Runner


async def calls(runner,args=({'job_id':'C1'},),name='controlled_poll'):
    response=LLMResponse(role='assistant',tools_call_name=[name]*len(args),
        tools_call_args=list(args),tools_call_ids=[str(index) for index in range(len(args))])
    return [result async for result in runner._handle_function_tools(runner.req,response)]


class ToolLoopLimiterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.Runner=runner_class();self.event=object();self.req=NS(func_tool=object());self.token=object()
        self.signals=[]
        self.handle=limiter.install_tool_loop_limiter(runner_cls=self.Runner,
            authorize=lambda event,req:self.token if event is self.event and req is self.req else None,
            emit=lambda event,signal:self.signals.append(signal))
        self.runner=self.Runner(self.event,self.req)
    def tearDown(self):self.handle.close()
    async def arm(self):
        for _ in range(3):await calls(self.runner)
        self.assertIn('armed',self.signals)

    async def test_three_identical_pending_results_then_one_toolless_completion(self):
        await self.arm();original=self.runner.provider_result;original_tools=self.req.func_tool
        final=await self.runner.final_step()
        self.assertIs(final,original);self.assertEqual(self.runner.done_hooks,1)
        self.assertEqual(len(self.runner.tool_executor.calls),3)
        self.assertEqual(len(self.runner.provider_calls),1)
        seen=self.runner.provider_calls[0]
        self.assertIsNone(seen['tools']);self.assertFalse(seen['streaming'])
        self.assertEqual((seen['retries'],seen['empty_attempts'],seen['fallbacks']),(0,1,[]))
        self.assertTrue(seen['messages'][0]._no_save)
        self.assertEqual(self.runner.run_context.messages,[])
        self.assertIs(self.req.func_tool,original_tools)
        self.assertTrue(self.runner.streaming);self.assertEqual(self.runner.request_max_retries,4)
        self.assertNotIn('_func_tool_for_provider',vars(self.runner))
        await self.runner.final_step()
        self.assertEqual(len(self.runner.provider_calls),1)

    async def test_same_batch_cannot_execute_fourth_identical_call(self):
        results=await calls(self.runner,[{'job_id':'C1'}]*6)
        self.assertEqual(len(results),6)
        self.assertEqual(len(self.runner.tool_executor.calls),3)
        self.assertEqual(sum(result.isError for result in results),3)
        self.assertEqual(self.signals.count('blocked'),3)

    async def test_progress_changes_are_not_stagnation(self):
        self.runner.tool_executor=Executor([pending(index) for index in range(8)])
        await calls(self.runner,[{'job_id':'C1'}]*8)
        self.assertEqual(len(self.runner.tool_executor.calls),8)
        self.assertNotIn('armed',self.signals)

    async def test_parameter_change_resets_before_threshold(self):
        await calls(self.runner,[{'job_id':'C1'}]*2+[{'job_id':'C2'}]*2)
        self.assertEqual(len(self.runner.tool_executor.calls),4)
        self.assertNotIn('armed',self.signals)

    async def test_changed_parameters_in_same_batch_may_resolve_armed_query(self):
        await calls(self.runner,[{'job_id':'C1'}]*3+[{'job_id':'C2'}])
        self.assertEqual(len(self.runner.tool_executor.calls),4)
        await self.runner.final_step()
        self.assertIsNotNone(self.runner.provider_calls[0]['tools'])

    async def test_failed_result_resets_and_real_retry_is_not_blocked(self):
        self.runner.tool_executor=Executor([pending(),pending(),pending(error=True),pending(),pending()])
        await calls(self.runner,[{'job_id':'C1'}]*5)
        self.assertEqual(len(self.runner.tool_executor.calls),5)
        self.assertNotIn('armed',self.signals)

    async def test_error_payload_with_false_isError_does_not_arm(self):
        self.runner.tool_executor=Executor([pending(ok=False)])
        await calls(self.runner,[{'job_id':'C1'}]*5)
        self.assertNotIn('armed',self.signals)

    async def test_unfinished_statuses_are_explicit_and_terminal_states_never_arm(self):
        for status in ['pending','running','queued','in_progress','success','done','failed','mystery']:
            with self.subTest(status=status):
                self.handle.release(self.event,self.req);self.signals.clear()
                self.runner.tool_executor=Executor([pending(status=status)])
                await calls(self.runner,[{'job_id':'C1'}]*3)
                self.assertEqual('armed' in self.signals,status in {'pending','running','queued','in_progress'})

    async def test_unknown_metadata_and_explicit_false_remain_observational(self):
        for candidate in [NS(name='controlled_poll'),tool(readonly=False),
                          NS(name='controlled_poll',annotations={'readOnlyHint':1})]:
            self.handle.release(self.event,self.req);self.signals.clear()
            self.runner.tools={'controlled_poll':candidate};self.runner.tool_executor=Executor()
            await calls(self.runner,[{'job_id':'C1'}]*5)
            self.assertEqual(len(self.runner.tool_executor.calls),5)
            self.assertNotIn('armed',self.signals)

    async def test_mcp_annotations_and_permission_wrapper_preserve_outer_execution(self):
        original=tool(mcp=True);guard=NS(name=original.name,_wrapped=original)
        self.runner.tools={'controlled_poll':guard}
        await self.arm()
        self.assertTrue(all(call['tool'] is guard for call in self.runner.tool_executor.calls))

    async def test_text_unknown_json_images_and_oversized_results_are_not_fingerprinted(self):
        values=[CallToolResult(content=[TextContent(type='text',text='pending')]),
            CallToolResult(content=[TextContent(type='text',text='{"status":"pending","status":"done"}')]),
            CallToolResult(content=[TextContent(type='text',text=json.dumps({'status':'pending','body':'x'*70000}))])]
        for value in values:
            self.handle.release(self.event,self.req);self.signals.clear()
            self.runner.tool_executor=Executor([value])
            await calls(self.runner,[{'job_id':'C1'}]*4)
            self.assertNotIn('armed',self.signals)

    async def test_json_key_order_is_not_a_progress_change(self):
        one=CallToolResult(content=[TextContent(type='text',text='{"status":"pending","progress":10}')])
        two=CallToolResult(content=[TextContent(type='text',text='{"progress":10,"status":"pending"}')])
        self.runner.tool_executor=Executor([one,two,one]);await self.arm()
        self.assertIs((await calls(self.runner))[0].isError,True)

    async def test_real_timestamp_change_is_preserved_as_change(self):
        self.runner.tool_executor=Executor([pending(timestamp=index) for index in range(5)])
        await calls(self.runner,[{'job_id':'C1'}]*5)
        self.assertNotIn('armed',self.signals)

    async def test_changing_earlier_yield_with_same_last_snapshot_is_not_stagnation(self):
        class MultipleResults(Executor):
            async def execute(self,tool,run_context,**args):
                self.calls.append({'tool':tool,'args':args})
                yield pending(progress=len(self.calls))
                yield pending(progress=10)
        self.runner.tool_executor=MultipleResults()
        results=await calls(self.runner,[{'job_id':'C1'}]*5)
        self.assertEqual(len(results),10)
        self.assertEqual(len(self.runner.tool_executor.calls),5)
        self.assertNotIn('armed',self.signals)

    async def test_final_tool_empty_error_or_rejection_falls_back_without_execution(self):
        variants=[LLMResponse(role='assistant',completion_text=''),
            LLMResponse(role='assistant',tools_call_name=['controlled_poll'],tools_call_args=[{'job_id':'C1'}],tools_call_ids=['again']),
            LLMResponse(role='err',completion_text='synthetic provider failure')]
        for response in variants:
            with self.subTest(response=response):
                self.handle.release(self.event,self.req);self.signals.clear()
                self.runner=self.Runner(self.event,self.req);await self.arm()
                self.runner.provider_result=response
                final=await self.runner.final_step()
                self.assertEqual(final.completion_text,limiter.FALLBACK_TEXT)
                self.assertFalse(final.tools_call_name)
                self.assertEqual(len(self.runner.tool_executor.calls),3)
                self.assertEqual(len(self.runner.provider_calls),1)
                self.assertIn('fallback',self.signals)

    async def test_final_exception_becomes_honest_fallback(self):
        await self.arm();self.runner.provider_error=RuntimeError('synthetic')
        self.assertEqual((await self.runner.final_step()).completion_text,limiter.FALLBACK_TEXT)
        self.assertEqual(len(self.runner.provider_calls),1)

    async def test_cancel_is_not_converted_to_fallback_and_restores_context(self):
        await self.arm();self.runner.provider_error=asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):await self.runner.final_step()
        self.assertNotIn('fallback',self.signals)
        self.assertEqual(self.runner.run_context.messages,[])
        self.assertTrue(self.runner.streaming);self.assertNotIn('_func_tool_for_provider',vars(self.runner))

    async def test_native_abort_empty_return_does_not_construct_fallback(self):
        await self.arm();self.runner.stop_requested=True
        self.assertIsNone(await self.runner.final_step())
        self.assertNotIn('fallback',self.signals)
        self.assertNotIn('finalized',self.signals)
        self.assertEqual(len(self.runner.provider_calls),0)
        self.assertEqual(self.runner.run_context.messages,[])
        self.assertTrue(self.runner.streaming)

    async def test_new_request_and_follow_up_generation_start_independently(self):
        await self.arm();self.runner._follow_up_seq+=1
        await calls(self.runner)
        self.assertEqual(len(self.runner.tool_executor.calls),4)
        self.assertFalse(next(iter(self.handle.requests.values())).armed)
        other=self.Runner(self.event,NS(func_tool=object()))
        await calls(other,[{'job_id':'C1'}]*4)
        self.assertEqual(len(other.tool_executor.calls),4)

    async def test_other_event_and_wrong_release_do_not_touch_owner(self):
        await self.arm();self.handle.release(object(),self.req)
        self.handle.release(self.event,object())
        self.assertEqual(len(self.handle.requests),1)
        other=self.Runner(object(),self.req)
        await calls(other,[{'job_id':'C1'}]*4)
        self.assertEqual(len(other.tool_executor.calls),4)

    async def test_normal_response_and_tool_result_identity_are_unchanged(self):
        raw=self.runner.tool_executor.results[0]
        self.assertIs((await calls(self.runner))[0],raw)
        original=self.runner.provider_result
        self.assertIs((await self.runner.final_step()),original)
        self.assertIsNotNone(self.runner.provider_calls[0]['tools'])

    async def test_close_while_awaiting_tool_blocks_batch_and_finishes_without_model(self):
        entered=asyncio.Event();resume=asyncio.Event()
        executor=self.runner.tool_executor;executor.entered=entered;executor.resume=resume
        task=asyncio.create_task(calls(self.runner,[{'job_id':'C1'}]*4))
        await entered.wait();self.handle.close();resume.set()
        results=await task
        self.assertEqual(len(executor.calls),1)
        self.assertEqual(sum(result.isError for result in results),3)
        self.assertIn('执行上下文已结束',results[1].content[0].text)
        self.assertNotIn('连续返回相同',results[1].content[0].text)
        final=await self.runner.final_step()
        self.assertIn('上下文已结束',final.completion_text)
        self.assertEqual(len(self.runner.provider_calls),0)
        self.assertNotIn('_iter_llm_responses_with_fallback',vars(self.runner))

    async def test_close_during_provider_await_cannot_execute_returned_tools(self):
        entered=asyncio.Event();resume=asyncio.Event()
        async def pause():entered.set();await resume.wait()
        self.runner.on_provider=pause
        self.runner.provider_result=LLMResponse(role='assistant',tools_call_name=['controlled_poll'],tools_call_args=[{}],tools_call_ids=['late'])
        task=asyncio.create_task(self.runner.final_step())
        await entered.wait();self.handle.close();resume.set()
        final=await task
        self.assertFalse(final.tools_call_name);self.assertEqual(len(self.runner.tool_executor.calls),0)
        self.assertEqual(len(self.runner.provider_calls),1)

    async def test_close_between_rounds_finishes_without_a_new_provider_call(self):
        await self.arm()
        self.assertFalse(self.runner.done())
        self.assertEqual(next(iter(self.handle.requests.values())).inflight,0)
        self.handle.close()
        final=await self.runner.final_step()
        self.assertIn('上下文已结束',final.completion_text)
        self.assertEqual(len(self.runner.provider_calls),0)
        self.assertEqual(len(self.runner.tool_executor.calls),3)
        self.assertNotIn('_iter_llm_responses_with_fallback',vars(self.runner))

    async def test_done_release_does_not_install_a_completion_gate(self):
        await calls(self.runner)
        await self.runner.final_step()
        self.assertTrue(self.runner.done())
        self.handle.release(self.event,self.req)
        self.assertNotIn('_iter_llm_responses_with_fallback',vars(self.runner))
        self.assertEqual(len(self.handle.requests),0)

    async def test_outer_wrapper_is_never_overwritten_and_reload_reuses_inert_bridge(self):
        old=self.Runner._handle_function_tools
        async def foreign(runner,req,response):
            async for value in old(runner,req,response):yield value
        self.Runner._handle_function_tools=foreign
        self.handle.close();self.handle.close()
        self.assertIs(self.Runner._handle_function_tools,foreign)
        newer=limiter.install_tool_loop_limiter(runner_cls=self.Runner,
            authorize=lambda event,req:object() if False else self.token)
        try:
            runner=self.Runner(self.event,self.req)
            await calls(runner,[{'job_id':'C1'}]*4)
            self.assertEqual(len(runner.tool_executor.calls),3)
        finally:newer.close()
        self.assertIs(self.Runner._handle_function_tools,foreign)

    async def test_request_capacity_is_bounded(self):
        self.handle.max_requests=2
        runners=[self.Runner(self.event,self.req) for _ in range(5)]
        for runner in runners:self.handle.bind(runner)
        self.assertEqual(len(self.handle.requests),2)
        self.handle.close();self.assertEqual(len(self.handle.requests),0)

    def test_unsupported_core_is_rejected_without_partial_install(self):
        class Unsupported:
            def _handle_function_tools(self):pass
        with self.assertRaises(TypeError):limiter.install_tool_loop_limiter(authorize=lambda *args:None,runner_cls=Unsupported)
        self.assertFalse(hasattr(Unsupported,limiter._STATE_ATTR))


if __name__=='__main__':unittest.main()
