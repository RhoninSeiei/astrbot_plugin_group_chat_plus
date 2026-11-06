import importlib.util
from pathlib import Path
import sys
import unittest

spec = importlib.util.spec_from_file_location('tool_progress_guard', Path(__file__).parents[1] / 'utils/tool_progress_guard.py')
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


class ToolProgressTests(unittest.TestCase):
    def test_same_call_arguments_alone_do_not_prove_stalled_progress(self):
        guard=module.RequestToolProgress('request')
        guard.begin('1','poll',{'job':'one'})
        guard.finish('1','success')
        signal=guard.begin('2','poll',{'job':'one'})
        self.assertEqual(signal.reason,'duplicate_success')
        self.assertFalse(signal.stalled)

    def test_result_fingerprints_distinguish_poll_progress_from_repeated_result(self):
        guard=module.RequestToolProgress('request')
        guard.begin('1','poll',{'job':'one'})
        guard.finish('1','success',result={'progress':10})
        guard.begin('2','poll',{'job':'one'})
        self.assertFalse(guard.finish('2','success',result={'progress':50}).stalled)
        guard.begin('3','poll',{'job':'one'})
        signal=guard.finish('3','success',result={'progress':50})
        self.assertEqual(signal.reason,'repeated_result')
        self.assertTrue(signal.stalled)
        self.assertNotIn('progress',repr(guard._records))

    def test_identical_success_and_failed_retries_are_detected(self):
        for outcome, reason in [('success','duplicate_success'),('failed','retry_after_failure'),('unknown','uncertain_repeat')]:
            guard = module.RequestToolProgress('request')
            self.assertFalse(guard.begin('1','search',{'query':'quota'}).stalled)
            self.assertEqual(guard.finish('1',outcome).reason,outcome)
            signal = guard.begin('2','search',{'query':'quota'})
            self.assertEqual((signal.reason,signal.attempts,signal.stalled),(reason,2,False))

    def test_new_request_and_corrected_arguments_are_independent(self):
        guard = module.RequestToolProgress('one')
        guard.begin('1','search',{'query':'quota','page':1})
        self.assertFalse(guard.begin('2','search',{'page':2,'query':'quota'}).stalled)
        other = module.RequestToolProgress('two')
        self.assertFalse(other.begin('1','search',{'query':'quota','page':1}).stalled)
        self.assertFalse(guard.begin('3','different',{'query':'quota','page':1}).stalled)

    def test_stale_pending_idempotent_start_and_lifecycle_clear(self):
        now=[0]
        guard=module.RequestToolProgress('request',stale_seconds=10,clock=lambda:now[0])
        guard.begin('1','write',{'value':'secret'})
        self.assertEqual(guard.begin('1','write',{'value':'secret'}).attempts,1)
        now[0]=10
        self.assertTrue(guard.check('1').stalled)
        self.assertEqual(guard.check()[0].reason,'stale_pending')
        guard.finish('1','success')
        self.assertFalse(guard.check('1').stalled)
        self.assertNotIn('secret',repr(guard._records))
        guard.clear();self.assertEqual(guard.check(),[])

    def test_bounded_storage_and_unknown_arguments_fail_open(self):
        guard=module.RequestToolProgress('request',max_records=2)
        for i in range(5):guard.begin(str(i),'read',{'id':i})
        self.assertLessEqual(len(guard._records),2)
        self.assertLessEqual(len(guard._calls),2)
        self.assertEqual(guard.begin('opaque','tool',object()).reason,'unrecognized')

    def test_older_completion_cannot_hide_newer_pending_duplicate(self):
        now=[0]
        guard=module.RequestToolProgress('request',stale_seconds=10,clock=lambda:now[0])
        guard.begin('1','read',{'id':1})
        guard.begin('2','read',{'id':1})
        self.assertEqual(guard.finish('1','success').reason,'superseded_result')
        now[0]=10
        self.assertTrue(guard.check('2').stalled)
        guard.finish('2','success')
        self.assertFalse(guard.check('2').stalled)


if __name__ == '__main__': unittest.main()
