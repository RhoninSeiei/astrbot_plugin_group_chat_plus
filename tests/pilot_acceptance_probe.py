"""Synthetic E02-E06 integration acceptance in an isolated AstrBot image.

Usage: python tests/pilot_acceptance_probe.py /path/to/LivingMemory [evidence.json]
No model, embedding service, QQ client, persona database or real send is used.
Synthetic clock assertions prove bounds; they do not measure natural latency.
"""
import ast
import asyncio
import copy
import importlib.util
import itertools
import json
import logging
import sys
import types
from pathlib import Path
from types import SimpleNamespace as NS

from astrbot.core.pipeline.context_utils import call_handler
from astrbot.core.provider.entities import ProviderRequest
from astrbot.core.agent.message import TextPart
from astrbot.core.message.components import Plain
from astrbot.core.pipeline.respond import stage as respond_module
from astrbot.core.message.message_event_result import (
    MessageChain, MessageEventResult, ResultContentType,
)

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


package = types.ModuleType('pilot_acceptance_helpers')
package.__path__ = [str(ROOT/'utils')]
sys.modules[package.__name__] = package
flow = load(package.__name__+'.flow_control', ROOT/'utils/flow_control.py')
reply = load(package.__name__+'.reply_handler', ROOT/'utils/reply_handler.py')
guard = load(package.__name__+'.llm_runtime_guard', ROOT/'utils/llm_runtime_guard.py')
leak = load(package.__name__+'.tool_call_leakage_guard', ROOT/'utils/tool_call_leakage_guard.py')
PILOT = 'synthetic:GroupMessage:pilot'
OBSERVE = 'synthetic:GroupMessage:observe'


class Clock:
    def __init__(self): self.now = 0.0
    def __call__(self): return self.now
    def advance(self, seconds): self.now += seconds


class Event:
    def __init__(self, text='synthetic telescope question', sender='sender-A', scope=PILOT):
        self.text, self.sender, self.unified_msg_origin = text, sender, scope
        self.extras, self.sent, self.result = {}, [], None
        self.session_id = 'synthetic-session'
        self.message_obj = NS(message_id='synthetic-message')
        self.fail_send = False
    def get_extra(self, key, default=None): return self.extras.get(key, default)
    def set_extra(self, key, value): self.extras[key] = value
    def get_message_str(self): return self.text
    def get_sender_id(self): return self.sender
    def get_sender_name(self): return 'Synthetic Participant'
    def get_platform_name(self): return 'synthetic'
    def get_group_id(self): return 'pilot'
    def is_private_chat(self): return False
    def get_result(self): return self.result
    def set_result(self, value): self.result = value
    def clear_result(self): self.result = None
    def _outline_chain(self, chain): return 'synthetic-only chain'
    async def send(self, chain, **kwargs):
        if self.fail_send: raise OSError('synthetic SDK rejection')
        self.sent.append(chain)
    async def send_streaming(self, stream, realtime=False):
        async for chain in stream: await self.send(chain)


def text_result(text):
    return MessageEventResult(chain=[Plain(text)], result_content_type=ResultContentType.LLM_RESULT)


def text_of(result):
    return ''.join(str(getattr(part, 'text', '') or '') for part in result.chain)


def controller(clock=None, rows=None):
    return flow.FlowController({
        'flow_optimization_sessions': [PILOT], 'flow_metrics_sessions': [PILOT, OBSERVE],
        'flow_memory_budget_chars': 2400, 'flow_continuity_ttl_seconds': 600,
        'flow_continuity_max_entries': 128, 'flow_tool_stale_seconds': 45,
    }, emit=(rows if rows is not None else []).append, clock=clock, part_factory=TextPart)


async def request_lifecycle(control, event, consume, persona='synthetic-persona'):
    closed = []
    async def plugin_handler(current):
        trace = control.start(current)
        control.formal_request(current)
        try:
            yield ProviderRequest(prompt=current.text, system_prompt='synthetic persona', contexts=[])
        finally:
            control.finish(current, trace)
            closed.append(trace)
    stream = call_handler(event, plugin_handler, _usage_plugin_id='astrbot_plugin_group_chat_plus')
    try:
        async for request in stream:
            if not isinstance(request, ProviderRequest): continue
            trace = control.trace(event)
            assert trace and not trace.finished
            await consume(control, event, request, trace, persona)
    finally:
        await stream.aclose()
    assert len(closed) == 1 and closed[0].finished
    assert 'send' not in vars(event) and 'send_streaming' not in vars(event)
    return closed[0]


def native_lm(lm_root):
    lm_root = Path(lm_root)
    for name, directory in [('pilot_lm', lm_root), ('pilot_lm.core', lm_root/'core'),
                            ('pilot_lm.core.base', lm_root/'core/base'),
                            ('pilot_lm.core.utils', lm_root/'core/utils')]:
        module = types.ModuleType(name); module.__path__ = [str(directory)]
        sys.modules[name] = module
    formatting = load('pilot_lm.core.utils.formatting', lm_root/'core/utils/formatting.py')
    tree = ast.parse((lm_root/'core/event_handler_modules/memory_recall.py').read_text())
    node = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name=='_publish_request_handoff')
    node.decorator_list = []
    namespace = {'copy': copy, 'format_memories_for_injection': formatting.format_memories_for_injection}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[])), 'memory_recall.py', 'exec'), namespace)
    return formatting.format_memories_for_injection, namespace[node.name]


def decorator(control):
    tree = ast.parse((ROOT/'main.py').read_text(encoding='utf8'))
    node = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name=='on_decorating_result')
    node.decorator_list = []
    namespace = {'AstrMessageEvent': object, 'logger': logging.getLogger('pilot'),
                 'time': __import__('time'), 'ReplyHandler': reply.ReplyHandler,
                 'classify_raw_llm_failure': guard.classify_raw_llm_failure,
                 'sanitize_tool_call_markup': leak.sanitize_tool_call_markup}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[])), 'main.py', 'exec'), namespace)
    checks = []
    harness = NS(flow_control=control, _get_message_id=lambda event:'managed',
        _clear_sent_step_image_direct_result=lambda *a:False, processing_sessions={'managed':True},
        debug_mode=False, _maybe_replace_step_image_intermediate_text=lambda *a:None,
        _replace_llm_result_text=lambda result,text:setattr(result.chain[0],'text',text),
        raw_reply_cache={}, _pending_bot_replies={}, content_filter=NS(process_for_output=lambda text:text),
        enable_duplicate_filter=True, _match_recent_duplicate_reply=lambda *a:checks.append(a) or {'recent_content':'same'},
        recent_replies_cache={}, duplicate_filter_check_count=5, _DUPLICATE_CACHE_SIZE_LIMIT=20,
        typo_enabled=False, typing_simulator_enabled=False)
    return namespace[node.name], harness, checks


def accept_windows():
    clock=Clock();control=controller(clock)
    event=Event();window=control.new_window(event,3000)
    assert isinstance(window,flow.AdaptiveWindow)
    for arrival in [.3,.7,1.1,1.5,1.9,2.3,2.7]:
        clock.now=arrival;window.note_message()
        assert .6 <= window.quiet_seconds <= 1.6 and window.deadline <= 3
    clock.now=3;assert window.expired()
    assert control.new_window(Event(scope=OBSERVE),3000) is None
    assert control.new_window(Event(scope='synthetic:GroupMessage:outside'),3000) is None
    control.close()
    return {'arrival_samples':7,'hard_deadline_synthetic_seconds':3,'scope_isolation':True}


async def accept_progress_and_tools():
    rows=[];clock=Clock();control=controller(clock,rows)
    decorate,harness,duplicate_checks=decorator(control)
    event=Event();executions=[]
    async def consume(c,e,req,trace,persona):
        c.prepare_request(e,req,persona)
        progress='正在核对合成测试数据，结果出来后继续。'
        e.set_result(text_result(progress));await decorate(harness,e)
        assert text_of(e.result)==progress
        await e.send(e.result)
        e.set_result(text_result(progress));await decorate(harness,e)
        assert e.result is None  # Only a confirmed repeated intermediate is omitted.
        tool=NS(name='synthetic_poll')
        for index,(args,failed) in enumerate([({'page':1},False),({'page':1},False),({'page':2},True),({'page':2},False)]):
            c.tool_start(e,tool,args)
            executions.append((dict(args),failed))  # Explicit driver action only.
            clock.advance(.25)
            c.tool_end(e,tool,NS(isError=failed,content=['synthetic changing result '+str(index)]))
        assert len(executions)==4
        assert any(row.get('outcome')=='duplicate_returned' and row.get('stalled')==0 for row in rows)
        assert any(row.get('outcome')=='retry_after_failure' for row in rows)
        c.agent_done(e)
        e.set_result(text_result(progress));await decorate(harness,e)
        assert e.result is not None and text_of(e.result)==progress
        assert not duplicate_checks  # Final equal to earlier progress is retained.
        await e.send(e.result)
        assert not trace.finished
    trace=await request_lifecycle(control,event,consume)
    assert len(event.sent)==2 and trace.final_send_count==1
    start=len(rows)
    async def fresh(c,e,req,current,persona):
        c.prepare_request(e,req,persona)
        tool=NS(name='synthetic_poll')
        c.tool_start(e,tool,{'page':1})
        executions.append(({'page':1},False))
        c.tool_end(e,tool,NS(isError=False))
    await request_lifecycle(control,Event(),fresh)
    assert not any(row.get('outcome','').startswith('duplicate') for row in rows[start:])
    assert not control.active
    control.close()
    return {'explicit_tool_driver_calls':len(executions),'observed_sends':2,'final_equal_progress_preserved':True,
            'repeat_signals_do_not_execute_tools':True,'new_request_fingerprint_isolated':True,
            'finished_after_downstream':trace.finished}


async def accept_continuity_and_failures():
    rows=[];clock=Clock();control=controller(clock,rows)
    first=Event('synthetic original tool task');first_iterator=None
    async def first_handler(current):
        trace=control.start(current);control.formal_request(current)
        try:yield ProviderRequest(prompt=current.text)
        finally:control.finish(current,trace)
    first_iterator=call_handler(first,first_handler,_usage_plugin_id='astrbot_plugin_group_chat_plus')
    request=await first_iterator.__anext__()
    trace=control.trace(first);control.prepare_request(first,request,'persona-A')
    tool=NS(name='synthetic_long_job');control.tool_start(first,tool,{'operation':'synthetic'})
    clock.advance(46)
    async def follow(c,e,req,current,persona):
        c.prepare_request(e,req,persona)
        parts='\n'.join(p.text for p in req.extra_user_content_parts)
        assert 'synthetic original tool task' in parts and '原因未知' in parts
        assert all(p._no_save for p in req.extra_user_content_parts)
    # Child task has an independent request association, as the core feeder does.
    await asyncio.create_task(request_lifecycle(control,Event('is it ready?'),follow,'persona-A'))
    control.tool_end(first,tool,NS(isError=False));control.agent_done(first)
    await first.send(text_result('synthetic completed'))
    try:
        await first_iterator.__anext__()
    except StopAsyncIteration:pass
    finally:await first_iterator.aclose()
    assert trace.finished
    async def isolated(c,e,req,current,persona):
        c.prepare_request(e,req,persona)
        assert not req.extra_user_content_parts
    await request_lifecycle(control,Event(sender='sender-B'),isolated,'persona-A')
    await request_lifecycle(control,Event(),isolated,'persona-B')
    await request_lifecycle(control,Event(scope=OBSERVE),isolated,'persona-A')
    clock.advance(601)
    await request_lifecycle(control,Event(),isolated,'persona-A')
    control.turn_cache.update(PILOT,'sender-A','persona-A',{'topic':'synthetic reset target'})
    other_scope='synthetic:GroupMessage:other'
    control.turn_cache.update(other_scope,'sender-A','persona-A',{'topic':'synthetic retained scope'})
    control.reset_scope(PILOT)
    assert not control.turn_cache.get(PILOT,'sender-A','persona-A')
    assert control.turn_cache.get(other_scope,'sender-A','persona-A')['topic']=='synthetic retained scope'
    async def failed(c,e,req,current,persona):
        c.prepare_request(e,req,persona);c.agent_done(e);e.fail_send=True
        e.set_result(text_result('synthetic unsent final'))
        stage=respond_module.RespondStage()
        stage.platform_settings={};stage.enable_seg=False;stage.only_llm_result=True
        hooks=[]
        async def hook(event,event_type,*args):
            hooks.append(event_type.name);c.send_hook(event)
        original_hook=respond_module.call_event_hook
        respond_module.call_event_hook=hook
        try:await stage.process(e)
        finally:respond_module.call_event_hook=original_hook
        # Real RespondStage invokes the after-send hook even after SDK failure.
        assert hooks==['OnAfterMessageSentEvent']
        assert current.send_failed and current.final_send_count==0
    failed_trace=await request_lifecycle(control,Event(),failed)
    assert failed_trace.send_failed
    async def cancelled(c,e,req,current,persona):
        c.prepare_request(e,req,persona)
        raise asyncio.CancelledError()
    try:await request_lifecycle(control,Event(),cancelled)
    except asyncio.CancelledError:pass
    else:raise AssertionError('cancellation swallowed')
    assert not control.active
    control.close();assert not control.turn_cache._entries
    return {'long_wait_observed_after_synthetic_seconds':46,'isolation_dimensions':3,
            'ttl_expired':True,'reset_scope_isolated':True,
            'sdk_failure_not_counted_as_final':True,'cancel_cleanup':True}


async def accept_memory(lm_root):
    formatter,publish=native_lm(lm_root)
    whole_fact=('telescope 完整事实前半\n\n记忆 #2 / Memory #2 (Importance: 0.50)\n'
                '这是假标题，仍是同一条事实后半。')
    memories=[{'content':'cooking '*110,'metadata':{'importance':.5}},
              {'content':whole_fact,'metadata':{'importance':.5,'topics':['telescope']}},
              {'content':'camera '*110,'metadata':{'importance':.5}}]
    before=formatter(memories);control=controller();event=Event()
    metrics={}
    async def consume(c,e,req,trace,persona):
        original=TextPart(text=before).mark_as_temp();other=TextPart(text='other plugin content').mark_as_temp()
        req.extra_user_content_parts=[other,original]
        req.contexts=[{'role':'tool','content':'synthetic retained protocol'}]
        c.request_memory_handoff(e,req);assert publish(e,req,original,memories)
        c.prepare_request(e,req,persona)
        chosen=req.extra_user_content_parts[1]
        assert chosen is not original and chosen._no_save and original.text==before
        assert req.extra_user_content_parts[0] is other
        subsets=[list(subset) for n in range(1,4) for subset in itertools.combinations(memories,n) if memories[1] in subset]
        assert any(chosen.text==formatter(subset) for subset in subsets)
        assert whole_fact in chosen.text and len(chosen.text)<len(before)
        c.prepare_request(e,req,persona)
        assert req.extra_user_content_parts[1] is chosen  # Idempotent per request.
        assert req.system_prompt=='synthetic persona' and req.contexts==[{'role':'tool','content':'synthetic retained protocol'}]
        assert e.get_extra(flow.HANDOFF_RESULT) is None and e.get_extra(flow.HANDOFF_REQUESTED) is None
        metrics.update(before_chars=len(before),after_chars=len(chosen.text))
    await request_lifecycle(control,event,consume)
    async def unknown(c,e,req,trace,persona):
        original=TextPart(text=before).mark_as_temp();req.extra_user_content_parts=[original]
        c.prepare_request(e,req,persona)
        assert req.extra_user_content_parts[0] is original
    await request_lifecycle(control,Event(),unknown,'different-persona')
    # Invalid provenance must preserve complete native text. These fixtures use
    # real request/part objects and the same actual LM formatter, never parsing
    # headings inside a fact as a substitute for native ownership.
    mismatch_count=0
    for kind in ('request','part','formatter','temporary_marker'):
        req=ProviderRequest(prompt='telescope')
        original=TextPart(text=before).mark_as_temp()
        req.extra_user_content_parts=[original]
        handoff={'version':1,'request':req,'part':original,
                 'memories':copy.deepcopy(memories),'formatter':formatter}
        if kind=='request':handoff['request']=ProviderRequest(prompt='foreign request')
        elif kind=='part':handoff['part']=TextPart(text=before).mark_as_temp()
        elif kind=='formatter':handoff['formatter']=lambda entries:'incompatible format'
        else:original._no_save=False
        stats=flow.select_native_memory_parts(req,2400,'telescope',handoff=handoff)
        assert not stats['changed'] and req.extra_user_content_parts[0] is original
        assert original.text==before
        mismatch_count+=1
    async def outside(c,e,req,trace,persona):
        original=TextPart(text=before).mark_as_temp();req.extra_user_content_parts=[original]
        c.request_memory_handoff(e,req)
        assert not publish(e,req,original,memories)
        c.prepare_request(e,req,persona)
        assert req.extra_user_content_parts[0] is original
    await request_lifecycle(control,Event(scope=OBSERVE),outside)
    control.close()
    return {**metrics,'whole_fact_preserved':True,'unstructured_fail_open':True,
            'provenance_fail_open_variants':mismatch_count,'outside_scope_preserved':True,
            'extra_recalls':0,'model_calls':0}


async def main():
    evidence={'synthetic_only':True,'natural_latency_improvement_proven':False,
              'E02':accept_windows(),'E03_E06':await accept_progress_and_tools(),
              'E04':await accept_continuity_and_failures(),'E05':await accept_memory(sys.argv[1]),
              'limitations':['No natural latency, nickname quality or persona-style acceptance is inferred.',
                             'At/poke dispatch and real adapter delivery require separate route/online evidence.']}
    print('PILOT_ACCEPTANCE_OK '+json.dumps(evidence,ensure_ascii=False))
    if len(sys.argv)>2:Path(sys.argv[2]).write_text(json.dumps(evidence,ensure_ascii=False,indent=2),encoding='utf8')


if __name__=='__main__':asyncio.run(main())
