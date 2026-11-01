import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch


ROOT = Path(__file__).parents[1]


class Plain:
    def __init__(self, text=''):
        self.text = text


class Image:
    pass


class At:
    def __init__(self, qq='123'):
        self.qq = qq


class Reply:
    def __init__(self, chain=None, message_str=''):
        self.chain = list(chain or [])
        self.message_str = message_str
        self.sender_id = '42'
        self.sender_nickname = 'Alice'


def load_modules():
    package = types.ModuleType('gcp_reply_path_test')
    package.__path__ = [str(ROOT / 'utils')]
    api = types.ModuleType('astrbot.api')
    api.logger = types.SimpleNamespace(info=lambda *a, **k: None, error=lambda *a, **k: None)
    all_api = types.ModuleType('astrbot.api.all')
    all_api.logger = api.logger
    all_api.AstrMessageEvent = object
    components = types.ModuleType('astrbot.api.message_components')
    for name, value in dict(Plain=Plain, Image=Image, At=At, Reply=Reply).items():
        setattr(components, name, value)
    core_components = types.ModuleType('astrbot.core.message.components')
    core_components.Forward = type('Forward', (), {})
    event = types.ModuleType('astrbot.api.event')
    event.AstrMessageEvent = object
    stubs = {'gcp_reply_path_test': package, 'astrbot': types.ModuleType('astrbot'),
             'astrbot.api': api, 'astrbot.api.all': all_api,
             'astrbot.api.message_components': components,
             'astrbot.core': types.ModuleType('astrbot.core'),
             'astrbot.core.message': types.ModuleType('astrbot.core.message'),
             'astrbot.core.message.components': core_components,
             'astrbot.api.event': event}
    loaded = []
    with patch.dict(sys.modules, stubs):
        for name in ['message_cleaner', 'platform_ltm_helper']:
            spec = importlib.util.spec_from_file_location('gcp_reply_path_test.' + name, ROOT / 'utils' / (name + '.py'))
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
            loaded.append(module)
        # Keep relative helper imports inside functions on the same component
        # fixtures while assertions execute, without changing other test suites.
        yield loaded


class ReplyChainPathsTest(unittest.TestCase):
    def setUp(self):
        self.loader = load_modules()
        self.cleaner, self.ltm = next(self.loader)

    def tearDown(self):
        try:
            next(self.loader)
        except StopIteration:
            pass

    def event(self, chain):
        return types.SimpleNamespace(message_obj=types.SimpleNamespace(message=chain))

    def test_pure_quoted_image_is_visible_to_cleaner_and_ltm(self):
        reply = Reply([Image()])
        self.assertIn('[图片]', self.cleaner.MessageCleaner._format_reply_component(reply))
        self.assertTrue(self.ltm.PlatformLTMHelper.has_image_in_message(self.event([reply])))
        self.assertFalse(self.ltm.PlatformLTMHelper.is_pure_image_message(self.event([reply, At()])))

    def test_mixed_quoted_text_and_image_preserve_both(self):
        reply = Reply([Plain('原文'), Image()], message_str='原文')
        rendered = self.cleaner.MessageCleaner._format_reply_component(reply)
        self.assertIn('原文', rendered)
        self.assertIn('[图片]', rendered)
        self.assertIn('Alice(ID:42)', rendered)

    def test_nested_quote_recognizes_image_but_plain_quote_does_not(self):
        self.assertTrue(self.ltm.PlatformLTMHelper.has_image_in_message(self.event([Reply([Reply([Image()])])])))
        self.assertFalse(self.ltm.PlatformLTMHelper.has_image_in_message(self.event([Reply([Plain('引用文字')])])))
        self.assertTrue(self.ltm.PlatformLTMHelper.is_pure_image_message(self.event([Image()])))
        self.assertFalse(self.ltm.PlatformLTMHelper.is_pure_image_message(self.event([Image(), Plain('改图')])))

    def test_recursive_quote_render_is_bounded_and_keeps_fallback_text(self):
        reply = Reply([Plain('引用正文')])
        reply.chain.append(reply)
        rendered = self.cleaner.MessageCleaner._format_reply_component(reply)
        self.assertIn('引用正文', rendered)
        self.assertLess(len(rendered), 200)
        invalid_chain = Reply(message_str='旧格式正文')
        invalid_chain.chain = object()
        self.assertIn('旧格式正文', self.cleaner.MessageCleaner._format_reply_component(invalid_chain))

    def test_legacy_text_and_missing_chain_keep_existing_output(self):
        self.assertEqual(self.cleaner.MessageCleaner._format_reply_component(Reply(message_str='引用文字')),
                         '[引用 Alice(ID:42): 引用文字]')
        self.assertEqual(self.cleaner.MessageCleaner._format_reply_component(Reply()), '[引用消息]')
