"""Request boundary behavior without loading the AstrBot runtime."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest

spec = importlib.util.spec_from_file_location('request_compatibility', Path(__file__).parents[1] / 'utils/request_compatibility.py')
compat = importlib.util.module_from_spec(spec)
spec.loader.exec_module(compat)


class Event:
    unified_msg_origin = 'default:GroupMessage:905956314'
    plugins_name = None
    def __init__(self): self.extras = {}
    def set_extra(self, key, value): self.extras[key] = value
    def get_extra(self, key, default=None): return self.extras.get(key, default)


class RequestCompatibilityTests(unittest.TestCase):
    def test_preserve_additions_without_reintroducing_platform_history(self):
        event = Event()
        platform_history = {'role': 'user', 'content': 'already in group history'}
        req = SimpleNamespace(prompt='current message', contexts=[platform_history], image_urls=['original'], extra_user_content_parts=['native memory'])
        compat.capture_baseline(event, req, [], ['original'])
        addition = {'role': 'user', 'content': 'learned expression'}
        req.contexts.extend([addition, addition])
        req.prompt = 'style context\ncurrent message\nknowledge context'
        req.image_urls.append('added')
        contexts, prompt, images = compat.merge_request(event, req, [], 'group history', ['original'])
        self.assertEqual(contexts, [addition])
        self.assertIn('style context', prompt)
        self.assertIn('knowledge context', prompt)
        self.assertIn('group history', prompt)
        self.assertEqual(images, ['original', 'added'])
        self.assertEqual(req.extra_user_content_parts, ['native memory'])

    def test_in_place_mutation_does_not_corrupt_saved_plugin_context(self):
        event = Event()
        own = [{'role': 'user', 'content': 'original'}]
        req = SimpleNamespace(prompt='question', contexts=own, image_urls=[])
        compat.capture_baseline(event, req, own, [])
        req.contexts.append({'role': 'assistant', 'content': 'third party'})
        contexts, _, _ = compat.merge_request(event, req, own, 'history', [])
        self.assertEqual(len(contexts), 2)
        req.contexts[0]['content'] = 'mutated'
        self.assertEqual(contexts[0]['content'], 'original')

    def test_missing_baseline_keeps_existing_behavior(self):
        self.assertEqual(compat.merge_request(Event(), SimpleNamespace(), [], 'full', []), ([], 'full', []))

    def test_scope_is_exact_and_default_off(self):
        event = Event()
        self.assertFalse(compat.enabled_for(event, {}))
        self.assertFalse(compat.enabled_for(event, {'compatibility_groups': ['905956314']}))
        self.assertTrue(compat.enabled_for(event, {'compatibility_groups': [event.unified_msg_origin]}))

    def test_static_split_keeps_examples_and_rules_once(self):
        static = 'persona examples\nreply rules'
        prompt, rules = compat.split_static_instructions(static + '\ncurrent sender and history', static)
        self.assertEqual(prompt, 'current sender and history')
        self.assertEqual(rules, static)
        self.assertEqual(compat.split_static_instructions('unexpected', static), ('unexpected', ''))

    def test_native_recall_is_scoped_and_falls_back_until_ready(self):
        event = Event()
        config = {'native_memory_groups': [event.unified_msg_origin], 'compatibility_groups': [event.unified_msg_origin]}
        state = [SimpleNamespace(event_handler=object()), True, object()]
        injector = SimpleNamespace(resolve_mode=lambda *a: ('livingmemory', 'v2'), _get_livingmemory_plugin_state=lambda *a: tuple(state))
        native = lambda cfg=config: compat.native_memory_available(None, event, cfg, injector, 'auto', 'auto')
        self.assertTrue(native())
        self.assertFalse(native({}))
        self.assertFalse(native({'native_memory_groups': [event.unified_msg_origin]}))
        state[1] = False
        self.assertFalse(native())
        state[1] = True
        state[0].event_handler = None
        self.assertFalse(native())
        state[0].event_handler = object()
        event.plugins_name = ['astrbot_plugin_group_chat_plus']
        self.assertFalse(native())
        event.plugins_name.append('astrbot_plugin_livingmemory')
        self.assertTrue(native())


if __name__ == '__main__': unittest.main()
