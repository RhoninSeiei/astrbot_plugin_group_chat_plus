import ast
import copy
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import unittest


spec = importlib.util.spec_from_file_location('image_subject_context', Path(__file__).parents[1] / 'utils/image_subject_context.py')
subject = importlib.util.module_from_spec(spec)
spec.loader.exec_module(subject)


class ImageSubjectContextTest(unittest.TestCase):
    def setUp(self):
        self.umo = 'default:GroupMessage:905956314'
        self.definition = '本群对可爱娇小二次元女孩的泛称，不是固定外观。'
        self.config = {'image_subject_contexts': json.dumps({self.umo: {'tn': self.definition, 'tntn': self.definition}})}

    def event(self, umo=None):
        return SimpleNamespace(unified_msg_origin=umo or self.umo)

    def test_other_groups_platforms_and_private_chats_are_unchanged(self):
        for umo in ['default:GroupMessage:764968756', 'qq2:GroupMessage:905956314', 'default:FriendMessage:905956314', '905956314']:
            raw = '画一张tn，无文字'
            self.assertEqual(subject.append_subject_definitions(self.event(umo), self.config, raw, raw), raw)
            self.assertEqual(subject.subject_tool_hint(self.event(umo), self.config, raw), '')

    def test_no_alias_or_empty_configuration_is_unchanged(self):
        for raw in ['画一张塞尔达', '画一只猫', '画一张button', '画一张tn123', '画图']:
            self.assertEqual(subject.append_subject_definitions(self.event(), self.config, raw, raw), raw)
            self.assertEqual(subject.subject_tool_hint(self.event(), self.config, raw), '')
        for invalid in [None, '', 'invalid', '[]', '{"x":1}', json.dumps({self.umo: {'tn': []}})]:
            cfg = {'image_subject_contexts': invalid}
            self.assertEqual(subject.append_subject_definitions(self.event(), cfg, '画tn', '画tn'), '画tn')

    def test_alias_case_and_current_request_are_preserved(self):
        for raw in ['画一张TN，无文字', '换成tn试试', '机器人画一张呆呆tntn的meme', '来张tn', '给我一张tn']:
            result = subject.append_subject_definitions(self.event(), self.config, raw, raw)
            self.assertTrue(result.startswith(raw + '\n\n'))
            self.assertIn(self.definition, result)
            self.assertIn(self.definition, subject.subject_tool_hint(self.event(), self.config, raw))
        # A model-invented alias in its prompt cannot select a definition.
        self.assertEqual(subject.append_subject_definitions(self.event(), self.config, '画一只猫', '画tn'), '画tn')

    def test_non_image_chat_does_not_receive_hint(self):
        self.assertEqual(subject.subject_tool_hint(self.event(), self.config, 'tn很可爱'), '')

    def test_hint_keeps_content_and_permission_boundaries(self):
        hint = subject.subject_tool_hint(self.event(), self.config, '画tn')
        self.assertIn('具体请求仍须遵守适用的内容限制', hint)
        self.assertIn('不改变年龄', hint)
        self.assertIn('不赋予额外权限', hint)
        self.assertEqual(subject.subject_definitions(self.event(), self.config, 'TNTN'), {'tntn': self.definition})

    def test_tool_descriptions_do_not_leak_to_another_request(self):
        from tests.test_step_image_tool_integration import ToolPolicy, FakeToolContainer
        from tests.test_image_tool_routing import routing, provider
        tree = ast.parse((Path(__file__).parents[1] / 'main.py').read_text(encoding='utf-8'))
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'ChatPlus')
        node = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == '_filter_image_tools_for_provider')
        ns = dict(copy=copy, ToolPolicy=ToolPolicy, subject_tool_hint=subject.subject_tool_hint,
                  visible_image_tools=routing.visible_image_tools, default_image_backend=routing.default_image_backend,
                  ALL_IMAGE_TOOLS=routing.ALL_IMAGE_TOOLS, PUBLIC_IMAGE_TOOLS=routing.PUBLIC_IMAGE_TOOLS)
        exec(compile(ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[])), 'main.py', 'exec'), ns)
        plugin = SimpleNamespace(step_image_config=self.config, _can_expose_step_image_tools=lambda e: True)
        tools = FakeToolContainer(['gcp_grok_image', 'gcp_gpt_image', 'search'])
        for tool in tools.tools:
            tool.description = 'original description'
        event = self.event()
        event.is_admin = lambda: True
        event.get_message_str = lambda: '来张tn'
        selected = ns['_filter_image_tools_for_provider'](plugin, event, provider('grok_oauth'), tools)
        self.assertTrue(all(self.definition in t.description for t in selected.tools if t.name.startswith('gcp_')))
        self.assertEqual(selected.tools[-1].description, 'original description')
        self.assertTrue(all(t.description == 'original description' for t in tools.tools))
        event.unified_msg_origin = 'default:GroupMessage:764968756'
        other = ns['_filter_image_tools_for_provider'](plugin, event, provider('grok_oauth'), tools)
        self.assertTrue(all(self.definition not in t.description for t in other.tools))
        event.unified_msg_origin = self.umo
        event.get_message_str = lambda: '画一只猫'
        cat = ns['_filter_image_tools_for_provider'](plugin, event, provider('grok_oauth'), tools)
        self.assertTrue(all(self.definition not in t.description for t in cat.tools))

    def test_subject_guidance_is_in_request_prompt_only_for_eligible_requests(self):
        from tests.test_image_tool_routing import routing
        tree = ast.parse((Path(__file__).parents[1] / 'main.py').read_text(encoding='utf-8'))
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'ChatPlus')
        node = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == '_build_step_image_tool_directive')
        ns = dict(PUBLIC_IMAGE_TOOLS=routing.PUBLIC_IMAGE_TOOLS, subject_tool_hint=subject.subject_tool_hint,
                  GroupImageService=SimpleNamespace(is_enabled=lambda cfg: True))
        exec(compile(ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[])), 'main.py', 'exec'), ns)
        build = ns['_build_step_image_tool_directive']
        plugin = SimpleNamespace(step_image_config=self.config, _can_expose_step_image_tools=lambda e: True)
        event = self.event()
        event.get_message_str = lambda: '机器人画一张死库水tn'
        baseline = build(plugin, {'gcp_grok_image'})
        prompt = build(plugin, {'gcp_grok_image'}, event=event)
        self.assertIn(self.definition, prompt)
        self.assertIn('区分非性化的游泳服饰描绘与性化呈现', prompt)
        self.assertIn('不能仅凭服饰名称或别名断言违法', prompt)
        self.assertIn('具体请求仍须遵守适用的内容限制', prompt)
        self.assertEqual(build(plugin, {'search'}, event=event), '')
        event.unified_msg_origin = 'default:GroupMessage:764968756'
        self.assertEqual(build(plugin, {'gcp_grok_image'}, event=event), baseline)
        event.unified_msg_origin = self.umo
        event.get_message_str = lambda: '画一只猫'
        self.assertEqual(build(plugin, {'gcp_grok_image'}, event=event), baseline)
        event.get_message_str = lambda: '画tn'
        plugin._can_expose_step_image_tools = lambda e: False
        self.assertEqual(build(plugin, {'gcp_grok_image'}, event=event), baseline)
