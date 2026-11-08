import importlib.util
from pathlib import Path
from types import SimpleNamespace as NS
import sys
import unittest

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('native_qualification',ROOT/'utils/native_queue.py')
q=importlib.util.module_from_spec(spec);sys.modules[spec.name]=q;spec.loader.exec_module(q)
Plain=type('Plain',(),{})
Reply=type('Reply',(),{})
Image=type('Image',(),{})


class Event:
    unified_msg_origin='test:GroupMessage:g'
    def __init__(self,parts):self.parts=parts
    def get_messages(self):return self.parts
    def get_platform_id(self):return 'test'
    def get_platform_name(self):return 'qq'


class NativeQualificationTests(unittest.TestCase):
    def test_plain_and_stable_official_history(self):
        self.assertEqual(q.native_text_qualification(Event([Plain()]),[NS(message_id='official_1')]),'plain')

    def test_text_reply_is_enriched(self):
        reply=Reply();reply.chain=[Plain()]
        self.assertEqual(q.native_text_qualification(Event([reply,Plain()]),[]),'enriched')

    def test_unresolved_reply_media_and_nested_media_fall_back(self):
        unresolved=Reply();unresolved.chain=[]
        media=Reply();media.chain=[Image()]
        nested=Reply();nested.chain=[media]
        for reply in [unresolved,media,nested]:
            self.assertNotIn(q.native_text_qualification(Event([reply]),[]),{'plain','enriched'})

    def test_images_windows_custom_history_remain_compatible(self):
        event=Event([Plain()])
        for history,images,windows in [([],['image'],[]),([],[],[{'content':'window'}]),([NS(message_id='official_0_123')],[],[])]:
            self.assertNotIn(q.native_text_qualification(event,history,image_urls=images,window_messages=windows),{'plain','enriched'})

    def test_source_bound_cache_is_enriched_duplicate_and_foreign_are_not(self):
        event=Event([Plain()]);cache=NS(message_id='cached_1')
        q.bind_native_cached_message(cache,{'message_id':'qq_1','sender_id':'user'},event)
        self.assertEqual(q.native_text_qualification(event,[cache]),'enriched')
        self.assertEqual(q.native_text_qualification(event,[cache,cache]),'unbound_or_duplicate_cache')
        other=Event([Plain()]);other.unified_msg_origin='test:GroupMessage:other'
        self.assertEqual(q.native_text_qualification(other,[cache]),'unbound_or_duplicate_cache')

    def test_cyclic_quotes_have_bounded_fallback(self):
        reply=Reply();reply.chain=[reply]
        self.assertEqual(q.native_text_qualification(Event([reply]),[]),'complex_quote')


if __name__=='__main__':unittest.main()
