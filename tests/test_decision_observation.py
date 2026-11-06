import asyncio,importlib,unittest
from test_reply_classification import DecisionAI,Context,Provider,Event,PACKAGE
obs=importlib.import_module(PACKAGE+'.decision_observation')
class DecisionObservationTests(unittest.IsolatedAsyncioTestCase):
 async def call(self,value,*,legacy=False,event=None,timeout=1):
  event=event or Event();provider=Provider([value])
  result=await DecisionAI.should_reply(Context(provider),event,'synthetic','','',select_reply_mode=not legacy,timeout=timeout)
  return result,event,provider
 async def test_all_modes_preserve_boolean_and_one_call(self):
  for legacy,values in [(False,[('skip',False),('brief',True),('full',True)]),(True,[('yes',True),('no',False)])]:
   for value,expected in values:
    result,event,p=await self.call(value,legacy=legacy)
    self.assertIs(result,expected);self.assertEqual(len(p.calls),1)
    self.assertEqual(obs.resolve(event,result),'allow' if expected else 'decline')
 async def test_failure_empty_protocol_and_timeout_are_distinct(self):
  async def slow():await asyncio.sleep(1)
  for response,expected in [('', 'empty_response'),(None,'empty_response'),('   ','empty_response'),('not a valid classification','protocol_failed'),(ValueError('private details'),'failed'),(slow,'timeout')]:
   result,event,p=await self.call(response,timeout=.01)
   self.assertFalse(result);self.assertEqual(len(p.calls),1);self.assertEqual(obs.resolve(event,result),expected)
   class Trace:
    def end(self,*args):self.args=args
   class Flow:
    def exit(self,*args):self.args=args
   trace=Trace();flow=Flow();obs.finish(event,trace,flow,result)
   self.assertNotEqual(flow.args[1],'no_reply');self.assertEqual(trace.args,('decision',expected))
   # Assert the real controller retains the reason through its final emission.
   flow_module=importlib.import_module(PACKAGE+'.flow_control')
   event.unified_msg_origin='test:GroupMessage:90001';event.is_private_chat=lambda:False;rows=[]
   controller=flow_module.FlowController({'flow_metrics_enabled':True,'flow_metrics_sessions':[event.unified_msg_origin]},rows.append)
   real_trace=controller.start(event);real_trace.begin('decision')
   obs.finish(event,real_trace,controller,result);controller.finish(event,real_trace)
   final=[r for r in rows if r['stage']=='complete']
   self.assertEqual(len(final),1);self.assertEqual(final[0]['outcome'],flow.args[1])

 async def test_legacy_errors_and_cancellation_are_not_decline(self):
  for value,expected in [('', 'empty_response'),(ValueError('private'),'failed')]:
   result,event,p=await self.call(value,legacy=True)
   self.assertFalse(result);self.assertEqual(obs.resolve(event,result),expected)
  event=Event();provider=Provider([asyncio.CancelledError()])
  with self.assertRaises(asyncio.CancelledError):await DecisionAI.should_reply(Context(provider),event,'synthetic','','',select_reply_mode=True)
  self.assertEqual(obs.resolve(event,False),'cancelled');self.assertEqual(len(provider.calls),1)
 async def test_missing_provider_and_event_reuse(self):
  event=Event();result=await DecisionAI.should_reply(Context(None),event,'synthetic','','',select_reply_mode=False)
  self.assertFalse(result);self.assertEqual(obs.resolve(event,False),'provider_unavailable')
  result,event,p=await self.call('skip',event=event)
  self.assertFalse(result);self.assertEqual(obs.resolve(event,False),'decline')
  obs.reset(event,source='rule');self.assertEqual(obs.resolve(event,True),'rule_allow');self.assertFalse(getattr(event,'_decision_ai_error',False))
 def test_unknown_cannot_be_reported_as_successful_skip(self):
  self.assertEqual(obs.resolve(Event(),False),'unknown')
 async def test_actual_main_decision_stage_reports_model_failure_not_no_reply(self):
  import ast,textwrap
  from pathlib import Path
  from types import SimpleNamespace
  source=(Path(__file__).parents[1]/'main.py').read_text(encoding='utf-8')
  start=source.index('        decision_trace = flow.begin(event, "decision")')
  end=source.index('        if not should_reply:',start)
  body=textwrap.dedent(source[start:end])
  namespace={'asyncio':asyncio,'reset_decision_observation':obs.reset,'record_decision_observation':obs.record,'finish_decision_observation':obs.finish}
  scaffold='async def stage(self,event,flow):\n'
  scaffold+='    _welcome_skip_all=_at_all_skip_all=False\n    formatted_context="test"\n    is_at_message=has_trigger_keyword=False\n    merged_image_urls=[]\n    matched_trigger_keyword=original_message_text=""\n    pending_cooldown_context={}\n'
  scaffold+=textwrap.indent(body,'    ')+'\n    return should_reply,decision_outcome\n'
  exec(compile(scaffold,'actual-main-decision-stage','exec'),namespace)
  for response,expected in [('skip','decline'),('brief','allow'),('', 'empty_response'),(RuntimeError('private'),'failed'),(asyncio.CancelledError(),'cancelled')]:
   event=Event();provider=Provider([response]);ends=[];exits=[]
   async def decide(event,prompt,*args,**kwargs):return await DecisionAI.should_reply(Context(provider),event,prompt,'','',select_reply_mode=True)
   plugin=SimpleNamespace(_check_ai_decision=decide,debug_mode=False)
   flow=SimpleNamespace(begin=lambda *args:SimpleNamespace(end=lambda *args:ends.append(args)),exit=lambda *args:exits.append(args[-1]))
   if expected=='cancelled':
    with self.assertRaises(asyncio.CancelledError):await namespace['stage'](plugin,event,flow)
   else:
    result,status=await namespace['stage'](plugin,event,flow)
    self.assertEqual(status,expected);self.assertEqual(result,expected=='allow')
   self.assertEqual(ends,[('decision',expected)]);self.assertEqual(len(provider.calls),1)
   if expected not in ('allow','decline'):self.assertNotIn('no_reply',exits)
 async def test_legacy_empty_skips_negative_learning_but_no_preserves_it(self):
  from test_reply_decision_flow import ReplyDecisionFlowTest
  for response,expected_updates in [('no',1),('',0)]:
   method,plugin,calls,records,decays,cooldown=ReplyDecisionFlowTest().harness()
   method.__globals__['DecisionAI']=DecisionAI
   provider=Provider([response]);plugin.context=Context(provider);plugin.enable_main_model_final_decision=False
   event=__import__('test_reply_decision_flow',fromlist=['Event']).Event()
   result=await method(plugin,event,'synthetic',False,False,pending_cooldown_context={'pending_before':True,'chat_key':'chat'})
   self.assertFalse(result);self.assertEqual(len(provider.calls),1)
   self.assertEqual(len(records),expected_updates)
   self.assertEqual(len(decays),expected_updates)
   self.assertEqual(obs.resolve(event,False),'decline' if response else 'empty_response')
if __name__=='__main__':unittest.main()
