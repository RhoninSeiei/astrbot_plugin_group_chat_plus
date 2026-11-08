import ast
import asyncio
import copy
import importlib.util
from pathlib import Path
from types import SimpleNamespace as NS
import sys
import unittest
from unittest.mock import AsyncMock

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('snapshot_queue',ROOT/'utils/native_queue.py')
q=importlib.util.module_from_spec(spec);sys.modules[spec.name]=q;spec.loader.exec_module(q)
TREE=ast.parse((ROOT/'main.py').read_text(encoding='utf8'))
CLS=next(n for n in TREE.body if isinstance(n,ast.ClassDef) and n.name=='ChatPlus')


def method(name,**namespace):
    node=copy.deepcopy(next(n for n in CLS.body if getattr(n,'name',None)==name))
    node.decorator_list=[]
    exec(compile(ast.fix_missing_locations(ast.Module(body=[node],type_ignores=[])),'main.py','exec'),namespace)
    return namespace[name]


class Event:
    unified_msg_origin='test:GroupMessage:g'
    def __init__(self):self.extras={}
    def get_extra(self,k,default=None):return self.extras.get(k,default)
    def set_extra(self,k,v):self.extras[k]=v
    def get_messages(self):return [type('Plain',(),{})()]
    def get_group_id(self):return 'g'
    def get_platform_id(self):return 'test'
    def get_platform_name(self):return 'qq'


class SnapshotAdmissionTests(unittest.TestCase):
    def prepare(self):
        return method('_prepare_native_queue_recipe',
            native_text_qualification=q.native_text_qualification,RequestHistoryRecipe=q.RequestHistoryRecipe,
            pin_native_history_snapshot=q.pin_native_history_snapshot,
            ContextManager=NS(format_context_for_ai=AsyncMock(),get_history_cutoff=lambda group:0))

    def test_uncopyable_cache_snapshot_does_not_enter_native_or_lossy_capture(self):
        class Uncopyable:
            def __deepcopy__(self,memo):raise RuntimeError('uncopyable state')
        event=Event();cached=NS(message_id='cached_1',opaque=Uncopyable())
        q.bind_native_cached_message(cached,{'message_id':'qq_1','sender_id':'user'},event)
        p=NS(context=NS(),_native_queue_capability=object(),_native_capture_guard=NS(supported=True),
            include_timestamp=False,include_sender_info=True)
        self.prepare()(p,event,[cached],'current','bot','formatted')
        self.assertIsNone(event.get_extra('_gcp_native_history_recipe'))
        self.assertEqual(event.get_extra('_gcp_native_qualification'),'snapshot_unavailable')

    def test_missing_guard_retains_enriched_compatibility(self):
        event=Event();cached=NS(message_id='cached_1')
        q.bind_native_cached_message(cached,{'message_id':'qq_1','sender_id':'user'},event)
        p=NS(context=NS(),_native_queue_capability=object(),include_timestamp=False,include_sender_info=True)
        self.prepare()(p,event,[cached],'current','bot','formatted')
        self.assertIsNone(event.get_extra('_gcp_native_history_recipe'))
        self.assertEqual(event.get_extra('_gcp_native_qualification'),'capture_guard_unavailable')

    def test_initialize_failure_precedes_bridge_and_success_installs_before_ready(self):
        for fail in [True,False]:
            order=[]
            def metadata():
                order.append('metadata')
                if fail:raise RuntimeError('metadata')
            def install(**kwargs):
                order.append('guard');return NS(supported=True,close=lambda:None)
            init=method('initialize',aiohttp=NS(ClientSession=lambda:NS()),
                GroupImageService=NS(is_enabled=lambda config:False),
                install_native_capture_guard=install)
            p=NS(enable_group_chat=True,flow_control=None,proactive_enabled=False,step_image_config={},
                _compute_session_integrity=lambda *a:'sig',_emit_session_metadata=metadata,
                _owns_enriched_native_request=lambda event:False,
                request_lifecycle=NS(mark_ready=lambda:order.append('ready')))
            if fail:
                with self.assertRaisesRegex(RuntimeError,'metadata'):asyncio.run(init(p))
                self.assertEqual(order,['metadata'])
            else:
                asyncio.run(init(p));self.assertEqual(order,['metadata','guard','ready'])

    def test_guard_closes_after_drain_even_if_later_identity_cleanup_fails(self):
        order=[]
        terminate=method('terminate',asyncio=asyncio)
        p=NS(_termination_complete=False,request_lifecycle=None,
            _native_capture_guard=NS(close=lambda:order.append('guard_closed')),
            member_identity_bridge=NS(aclose=AsyncMock(side_effect=RuntimeError('identity close'))))
        with self.assertRaisesRegex(RuntimeError,'identity close'):asyncio.run(terminate(p))
        self.assertEqual(order,['guard_closed']);self.assertIsNone(p._native_capture_guard)


if __name__=='__main__':unittest.main()
