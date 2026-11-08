import asyncio
import unittest
from types import SimpleNamespace as NS

try:
    from tests.test_native_processing_claim import method, Event, _queue as queue
except ModuleNotFoundError:
    from test_native_processing_claim import method, Event, _queue as queue


class HistoryEvent(Event):
    def __init__(self,key):
        super().__init__();self.key=key
        self.set_extra('_gcp_native_history_lock',asyncio.Lock())
    def get_group_id(self):return '905956314'
    def get_platform_id(self):return 'default'
    def get_platform_name(self):return 'aiocqhttp'
    def get_self_id(self):return 'synthetic-bot'


class NativeHistoryReaderTests(unittest.IsolatedAsyncioTestCase):
    def setup_reader(self,limit,records,pending):
        class Store:
            def __init__(self):self.calls=[]
            async def get(self,**kw):
                self.calls.append(kw)
                return records[-kw['page_size']:]
        store=Store()
        cm=NS(get_history_cutoff=lambda group:0,
              _official_history_to_message=lambda row,*args:NS(message_id='official_'+str(row.id),message_str=row.text,timestamp=row.id))
        read=method('_read_native_committed_history',{'ContextManager':cm})
        plugin=NS(max_context_messages=limit,context=NS(message_history_manager=store),request_lifecycle=NS(active=[NS(event=e) for e in pending]),processing_sessions={e.key:'group' for e in pending},_get_message_id=lambda e:e.key)
        return plugin,store,read

    def pending(self,key,row_id):
        e=HistoryEvent(key)
        e.set_extra('_gcp_pending_official_user_row',{'event':e,'umo':e.unified_msg_origin,'platform_id':e.get_platform_id(),'group_id':e.get_group_id(),'row_id':row_id})
        return e

    async def test_limit_one_preserves_prior_reply_when_current_and_next_are_pending(self):
        current=self.pending('current',3);next_event=self.pending('next',4)
        records=[NS(id=1,text='prior-user'),NS(id=2,text='prior-reply'),NS(id=3,text='current'),NS(id=4,text='next')]
        p,store,read=self.setup_reader(1,records,[current,next_event])
        got=await read(p,current,current.unified_msg_origin,[])
        self.assertEqual([r.message_str for r in got],['prior-reply'])
        self.assertEqual(store.calls[0]['page_size'],3)

    async def test_identical_text_with_different_storage_ids_is_retained(self):
        current=self.pending('current',3)
        p,store,read=self.setup_reader(2,[NS(id=1,text='same'),NS(id=2,text='same'),NS(id=3,text='current')],[current])
        got=await read(p,current,current.unified_msg_origin,[])
        self.assertEqual([r.message_id for r in got],['official_1','official_2'])

    async def test_invalid_marker_partition_fails_without_reading_another_group(self):
        current=self.pending('current',3);current.get_extra('_gcp_pending_official_user_row')['umo']='other:GroupMessage:wrong'
        p,store,read=self.setup_reader(1,[NS(id=2,text='prior')],[current])
        with self.assertRaises(ValueError):await read(p,current,current.unified_msg_origin,[])
        self.assertEqual(store.calls,[])

    async def test_zero_history_never_reads_storage(self):
        current=HistoryEvent('current');p,store,read=self.setup_reader(0,[],[current])
        self.assertEqual(await read(p,current,current.unified_msg_origin,[]),[])
        self.assertEqual(store.calls,[])

    def cache(self,event,key='aiocqhttp_10',text='pending-cache'):
        message=NS(message_id='cached_1',message_str=text,timestamp=1,sender=NS(user_id='u'))
        queue.bind_native_cached_message(message,{'message_id':key,'sender_id':'u'},event)
        return message

    async def test_uncommitted_cache_is_preserved_with_new_reply(self):
        current=self.pending('current',3)
        p,store,read=self.setup_reader(20,[NS(id=2,text='prior-reply'),NS(id=3,text='current')],[current])
        got=await read(p,current,current.unified_msg_origin,[self.cache(current)])
        self.assertEqual([m.message_str for m in got],['pending-cache','prior-reply'])

    async def test_exact_receipt_deduplicates_cache_without_dropping_distinct_text_ids(self):
        current=self.pending('current',4)
        p,store,read=self.setup_reader(20,[NS(id=2,text='committed-cache'),NS(id=3,text='same'),NS(id=4,text='current')],[current])
        cache=self.cache(current)
        queue.publish_native_history_row(p.context,queue.native_cached_source(cache,current),2)
        got=await read(p,current,current.unified_msg_origin,[cache,self.cache(current,'aiocqhttp_11','same')])
        self.assertEqual([m.message_str for m in got],['same','committed-cache','same'])

    async def test_conflicting_exact_receipt_retains_recipe_fallback(self):
        current=self.pending('current',3)
        p,store,read=self.setup_reader(20,[NS(id=2,text='prior')],[current])
        cached=self.cache(current);source=queue.native_cached_source(cached,current)
        queue.publish_native_history_row(p.context,source,1)
        queue.publish_native_history_row(p.context,source,2)
        with self.assertRaisesRegex(ValueError,'ambiguous'):
            await read(p,current,current.unified_msg_origin,[cached])

if __name__=='__main__':unittest.main()
