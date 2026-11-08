import ast
import asyncio
import copy
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

ROOT=Path(__file__).resolve().parents[1]
import importlib.util, sys
_spec=importlib.util.spec_from_file_location('claim_native_queue',ROOT/'utils/native_queue.py')
_queue=importlib.util.module_from_spec(_spec);sys.modules[_spec.name]=_queue;_spec.loader.exec_module(_queue)

def method(name, namespace):
    tree=ast.parse((ROOT/'main.py').read_text(encoding='utf-8'))
    cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='ChatPlus')
    node=next((copy.deepcopy(n) for n in cls.body if isinstance(n,ast.AsyncFunctionDef) and n.name==name),None)
    if node is None and name=='_claim_processing_slot':
        # Execute the actual pre-fix admission block to observe the 14 polls.
        source=(ROOT/'main.py').read_text(encoding='utf-8')
        a=source.index('        max_wait_loops = self.concurrent_wait_max_loops  # 最大等待循环次数',source.index('decision_outcome = finish_decision_observation'))
        b=source.index('        # 🔧 成功回复相关',a)
        import textwrap
        body=ast.parse(textwrap.dedent(source[a:b])).body
        node=ast.AsyncFunctionDef(name=name,args=ast.arguments(posonlyargs=[],args=[ast.arg(arg=x) for x in ['self','event','chat_id','message_id']],kwonlyargs=[],kw_defaults=[],defaults=[]),body=body+[ast.Return(value=ast.Constant(True))],decorator_list=[])
    if node is None:raise AssertionError(name)
    node.decorator_list=[]
    ns={'AstrMessageEvent':object,'asyncio':asyncio,'time':time,'flow':None,
        'merge_native_cached_history':_queue.merge_native_cached_history,
        'native_history_lock':_queue.native_history_lock,
        'logger':SimpleNamespace(info=lambda *a,**k:None,warning=lambda *a,**k:None,error=lambda *a,**k:None),**namespace}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[node],type_ignores=[])),'main.py','exec'),ns)
    return ns[name]

class Event:
    unified_msg_origin='default:GroupMessage:905956314'
    def __init__(self):self.extras={};self.call_llm=False
    def get_extra(self,key,default=None):return self.extras.get(key,default)
    def set_extra(self,key,value):self.extras[key]=value
    def is_private_chat(self):return False

class ProcessingClaimTest(unittest.IsolatedAsyncioTestCase):
    def plugin(self,native=True):
        return SimpleNamespace(processing_sessions={'first':'group'},concurrent_lock=asyncio.Lock(),concurrent_wait_max_loops=14,concurrent_wait_interval=1.0,debug_mode=False,flow_control=None,_can_delegate_native_queue=lambda event:native)

    async def test_native_admission_has_no_plugin_polling_or_forced_wait(self):
        plugin=self.plugin();event=Event();claim=method('_claim_processing_slot',{})
        with patch.object(asyncio,'sleep',new_callable=AsyncMock) as sleep:
            self.assertTrue(await claim(plugin,event,'group','second'))
        self.assertEqual(sleep.await_count,0)
        self.assertEqual(plugin.processing_sessions,{'first':'group','second':'group'})

    async def test_duplicate_admission_does_not_remove_original(self):
        plugin=self.plugin();event=Event();claim=method('_claim_processing_slot',{})
        with patch.object(asyncio,'sleep',new_callable=AsyncMock) as sleep:
            result=await claim(plugin,event,'group','first')
        self.assertFalse(result)
        self.assertEqual(sleep.await_count,0)
        self.assertEqual(plugin.processing_sessions,{'first':'group'})

    async def test_unsupported_path_retains_existing_bounded_wait(self):
        plugin=self.plugin(False);event=Event();claim=method('_claim_processing_slot',{})
        with patch.object(asyncio,'sleep',new_callable=AsyncMock) as sleep:
            self.assertTrue(await claim(plugin,event,'group','second'))
        self.assertEqual(sleep.await_count,14)

    async def test_duplicate_group_handler_finally_preserves_inflight_snapshots(self):
        event=Event();state={'first':'group'};snap={'first':{'image_urls':['synthetic-image']}};smart={'first':[{'content':'synthetic follower'}]}
        plugin=SimpleNamespace(enable_group_chat=True,_is_vacuum_message=lambda e:False,_check_compliance_status=lambda:None,_get_message_id=lambda e:'first',_seen_message_ids={'first':time.time()},command_messages={},debug_mode=False,flow_control=None,member_identity_bridge=None,processing_sessions=state,_message_cache_snapshots=snap,_smart_batch_snapshots=smart,concurrent_mode='legacy',_duplicate_blocked_messages={})
        handler=method('on_group_message',{})
        async for _ in handler(plugin,event):pass
        self.assertEqual(state,{'first':'group'})
        self.assertIn('first',snap)
        self.assertIn('first',smart)

    async def test_expired_seen_duplicate_preserves_original_processing_and_images(self):
        event=Event();snap={'first':{'image_urls':['synthetic-image']}}
        seen={str(i):time.time()-120 for i in range(102)};seen['first']=time.time()-120
        plugin=SimpleNamespace(enable_group_chat=True,_is_vacuum_message=lambda e:False,_check_compliance_status=lambda:None,_get_message_id=lambda e:'first',_seen_message_ids=seen,command_messages={},debug_mode=True,flow_control=None,member_identity_bridge=None,processing_sessions={'first':'group'},_message_cache_snapshots=snap,_smart_batch_snapshots={'first':['follower']},concurrent_mode='legacy',_duplicate_blocked_messages={})
        async for _ in method('on_group_message',{})(plugin,event):pass
        self.assertEqual(plugin.processing_sessions,{'first':'group'})
        self.assertIn('first',snap)
        self.assertTrue(event.call_llm)

    async def test_expired_seen_duplicate_during_decision_does_not_delete_original_snapshot(self):
        event=Event();original=Event();snap={'first':{'content':'pending decision'}}
        seen={str(i):time.time()-120 for i in range(102)}
        owners={(event.unified_msg_origin,'first'):original}
        plugin=SimpleNamespace(enable_group_chat=True,_is_vacuum_message=lambda e:False,_check_compliance_status=lambda:None,_get_message_id=lambda e:'first',_seen_message_ids=seen,command_messages={},debug_mode=False,flow_control=None,member_identity_bridge=None,processing_sessions={},_request_message_owners=owners,_message_cache_snapshots=snap,_smart_batch_snapshots={},concurrent_mode='legacy',_duplicate_blocked_messages={})
        async for _ in method('on_group_message',{})(plugin,event):pass
        self.assertIs(owners[(event.unified_msg_origin,'first')],original)
        self.assertIn('first',snap)

if __name__=='__main__':unittest.main()
