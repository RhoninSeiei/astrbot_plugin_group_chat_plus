import ast
import asyncio
import copy
import json
from pathlib import Path
from types import SimpleNamespace
import unittest

from tests.test_image_tool_routing import routing, provider


class Config(dict):
    saved = None
    fail = False

    def save_config(self):
        if self.fail:
            raise OSError('disk unavailable')
        self.saved = json.dumps(self)


class ImageBackendCommandTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        source = ast.parse((Path(__file__).parents[1] / 'main.py').read_text(encoding='utf-8'))
        cls = next(n for n in source.body if isinstance(n, ast.ClassDef) and n.name == 'ChatPlus')
        node = copy.deepcopy(next(n for n in cls.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'image_default'))
        node.decorator_list = []
        namespace = dict(asyncio=asyncio, json=json, AstrMessageEvent=object,
                         is_image_admin=routing.is_image_admin,
                         group_image_backends=routing.group_image_backends,
                         planner_backend=routing.planner_backend)
        exec(compile(ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[])), 'main.py', 'exec'), namespace)
        self.command = namespace['image_default']
        self.plugin = SimpleNamespace(config=Config(), step_image_config={},
                                      context=SimpleNamespace(get_using_provider=lambda **kw: provider('grok_oauth')))
        self.event = SimpleNamespace(is_admin=lambda: True, is_private_chat=lambda: False,
                                     unified_msg_origin='bot:GroupMessage:42', plain_result=lambda s: s)

    async def run_command(self, value=''):
        return [s async for s in self.command(self.plugin, self.event, value)]

    async def test_query_set_reload_other_group_and_auto(self):
        self.assertIn('当前：Grok', (await self.run_command())[0])
        self.assertIsNone(self.plugin.config.saved)
        self.assertIn('：GPT。', (await self.run_command('gpt'))[0])
        persisted = json.loads(self.plugin.config.saved)
        self.plugin.step_image_config = persisted.copy()
        self.assertIn('：GPT。', (await self.run_command())[0])
        self.event.unified_msg_origin = 'bot:GroupMessage:43'
        await self.run_command('grok')
        self.event.unified_msg_origin = 'bot:GroupMessage:42'
        await self.run_command('auto')
        self.assertEqual(routing.group_image_backends(self.plugin.config), {'bot:GroupMessage:43': 'grok_oauth'})

    async def test_non_admin_private_and_invalid_input_do_not_save(self):
        self.event.is_admin = lambda: False
        self.assertIn('仅 AstrBot 管理员', (await self.run_command('gpt'))[0])
        self.event.is_admin = lambda: True
        self.event.is_private_chat = lambda: True
        self.assertIn('群内', (await self.run_command('gpt'))[0])
        self.event.is_private_chat = lambda: False
        self.assertIn('用法', (await self.run_command('other'))[0])
        self.assertIsNone(self.plugin.config.saved)

    async def test_failed_persistence_keeps_effective_default(self):
        await self.run_command('grok')
        self.plugin.config.fail = True
        self.assertIn('保存失败', (await self.run_command('gpt'))[0])
        self.assertEqual(routing.group_image_backends(self.plugin.config), routing.group_image_backends(self.plugin.step_image_config))
        self.assertIn('：Grok。', (await self.run_command())[0])

    async def test_concurrent_group_settings_are_not_lost(self):
        other = copy.copy(self.event)
        other.unified_msg_origin = 'bot:GroupMessage:43'
        async def run(event, value):
            return [s async for s in self.command(self.plugin, event, value)]
        await asyncio.gather(run(self.event, 'gpt'), run(other, 'grok'))
        self.assertEqual(routing.group_image_backends(json.loads(self.plugin.config.saved)),
                         {'bot:GroupMessage:42': 'codex_oauth', 'bot:GroupMessage:43': 'grok_oauth'})

    async def test_cancellation_requested_during_save_keeps_disk_and_runtime_aligned(self):
        original = self.plugin.config.save_config
        def save():
            asyncio.get_running_loop().call_soon(task.cancel)
            original()
        self.plugin.config.save_config = save
        task = asyncio.create_task(self.run_command('gpt'))
        try:
            await task
        except asyncio.CancelledError:
            pass
        self.assertEqual(routing.group_image_backends(json.loads(self.plugin.config.saved)),
                         routing.group_image_backends(self.plugin.step_image_config))
        self.assertIn('：GPT。', (await self.run_command())[0])
