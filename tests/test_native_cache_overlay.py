"""Source-bound cache overlays: no content or timestamp identity guessing."""
import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace as NS
import sys
import unittest
import gc
import weakref

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('native_cache_overlay', ROOT/'utils/native_queue.py')
q = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = q
spec.loader.exec_module(q)


class Event:
    unified_msg_origin = 'test:GroupMessage:g1'
    def get_platform_id(self): return 'test'
    def get_platform_name(self): return 'qq'


class CacheOverlayTests(unittest.TestCase):
    def setUp(self):
        self.event = Event()
        self.context = NS()

    def cache(self, key, text='same', sender='u1', timestamp=10):
        msg = NS(message_id='cached_10', message_str=text, timestamp=timestamp,
                 sender=NS(user_id=sender))
        q.bind_native_cached_message(msg, {'message_id':key, 'sender_id':sender,
            'image_urls':[], 'reference_image_urls':[]}, self.event)
        return msg

    def test_pending_cache_survives_fresh_official_history(self):
        cached = self.cache('qq_1', 'uncommitted')
        fresh = [NS(message_id='official_5', message_str='previous reply', timestamp=11)]
        got = q.merge_native_cached_history(self.event, [cached], fresh,
            context=self.context, excluded_ids=set(), cutoff=0, limit=20)
        self.assertEqual([m.message_str for m in got], ['uncommitted', 'previous reply'])

    def test_exact_promotion_receipt_replaces_overlay_only_when_row_is_present(self):
        cached = self.cache('qq_1')
        source = q.native_cached_source(cached, self.event)
        q.publish_native_history_row(self.context, source, 5)
        fresh = [NS(message_id='official_5', message_str='committed', timestamp=11)]
        got = q.merge_native_cached_history(self.event, [cached], fresh,
            context=self.context, excluded_ids=set(), cutoff=0, limit=20)
        self.assertEqual([m.message_str for m in got], ['committed'])
        got = q.merge_native_cached_history(self.event, [cached], [],
            context=self.context, excluded_ids=set(), cutoff=0, limit=20)
        self.assertEqual([m.message_str for m in got], ['same'])

    def test_same_text_and_timestamp_with_distinct_ids_or_senders_stays_distinct(self):
        original = [self.cache('qq_1'), self.cache('qq_2'), self.cache('qq_1',sender='u2')]
        self.assertEqual(len({q.native_cached_source(m,self.event) for m in original}), 3)
        got = q.merge_native_cached_history(self.event, original, [], context=self.context,
            excluded_ids=set(), cutoff=0, limit=20)
        self.assertEqual(len(got), 3)

    def test_cross_group_or_unknown_source_is_ineligible(self):
        cached = self.cache('qq_1')
        other = Event(); other.unified_msg_origin = 'test:GroupMessage:g2'
        self.assertIsNone(q.native_cached_source(cached,other))
        self.assertIsNone(q.native_cached_source(self.cache('fallback_1'),self.event))

    def test_media_cache_is_ineligible_and_duplicate_sources_are_rejected(self):
        cached = self.cache('qq_1')
        q.bind_native_cached_message(cached,{'message_id':'qq_1','sender_id':'u1',
            'image_urls':['private-image']},self.event)
        self.assertIsNone(q.native_cached_source(cached,self.event))
        with self.assertRaises(ValueError):
            q.merge_native_cached_history(self.event,[self.cache('qq_1'),self.cache('qq_1')],[],
                context=self.context,excluded_ids=set(),cutoff=0,limit=20)

    def test_pending_row_cutoff_and_limit_are_applied_after_merge(self):
        a,b = self.cache('qq_1',timestamp=1),self.cache('qq_2',timestamp=3)
        q.publish_native_history_row(self.context,q.native_cached_source(b,self.event),7)
        fresh=[NS(message_id='official_6',message_str='prior',timestamp=2)]
        got=q.merge_native_cached_history(self.event,[a,b],fresh,context=self.context,
            excluded_ids={7},cutoff=2,limit=1)
        self.assertEqual([m.message_str for m in got],['prior'])

    def test_capacity_pressure_cannot_evict_live_cache_or_invalidate_unrelated_groups(self):
        cache=self.cache('qq_1')
        class Snapshot: pass
        snapshot=Snapshot()
        self.assertTrue(q.pin_native_history_snapshot(self.context,snapshot,self.event,[cache]))
        q.publish_native_history_row(self.context,q.native_cached_source(cache,self.event),9000)
        for n in range(2050):q.publish_native_history_row(self.context,str(n).zfill(64),n+1)
        got=q.merge_native_cached_history(self.event,[cache],
            [NS(message_id='official_9000',message_str='committed',timestamp=11)],
            context=self.context,excluded_ids=set(),cutoff=0,limit=20)
        self.assertEqual([m.message_str for m in got],['committed'])

    def test_completed_snapshot_is_not_retained_by_context_receipts(self):
        class Snapshot: pass
        snapshot=Snapshot();reference=weakref.ref(snapshot)
        q.pin_native_history_snapshot(self.context,snapshot,self.event,[self.cache('qq_1')])
        del snapshot
        gc.collect()
        self.assertIsNone(reference())


if __name__=='__main__': unittest.main()
