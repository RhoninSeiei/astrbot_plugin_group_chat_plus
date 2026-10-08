import ast
from pathlib import Path
from types import SimpleNamespace
import unittest


def load_handler():
    tree = ast.parse((Path(__file__).parents[1] / 'utils/reply_handler.py').read_text(encoding='utf-8'))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'ReplyHandler')
    methods = {'_collapse_reply_text', '_is_explicit_long_form_request',
               '_scan_reply_breaks', '_collect_reply_sentences', '_take_leading_sentences',
               '_guess_trimmed_reply_suffix', '_polish_trimmed_reply',
               '_trim_reply_by_clause', '_apply_group_chat_brevity_limit'}
    cls.body = [n for n in cls.body if
                isinstance(n, ast.FunctionDef) and n.name in methods or
                isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id.startswith('BRIEF_REPLY_') for t in n.targets)]
    ns = {'AstrMessageEvent': object, 'PLUGIN_REPLY_EFFECT_CONTEXT': 'effect'}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[cls], type_ignores=[])), 'reply_handler.py', 'exec'), ns)
    return ns['ReplyHandler']


Handler = load_handler()


class ReplyBrevityBudgetTest(unittest.TestCase):
    def event(self, *, direct=False, private=False, message='你好'):
        return SimpleNamespace(is_private_chat=lambda: private,
                               is_at_or_wake_command=direct,
                               get_extra=lambda key, default=None: default,
                               get_message_str=lambda: message)

    def test_short_explanation_is_retained(self):
        text = '无法完成。缺少所需的参考图片。'
        for direct in (False, True):
            self.assertEqual(Handler._apply_group_chat_brevity_limit(self.event(direct=direct), text), text)

    def test_all_sentences_at_each_character_budget_are_retained(self):
        for budget, event in [(30, self.event()), (42, self.event(direct=True)),
                              (78, self.event(direct=True, message='请详细解释这件事情的原因和步骤'))]:
            for length in (budget - 1, budget):
                text = '收到。可以。' + '甲' * (length - 7) + '。'
                self.assertEqual(len(text), length)
                self.assertEqual(Handler._apply_group_chat_brevity_limit(event, text), text)

    def test_over_budget_still_limits_sentences(self):
        text = '这是第一句。' + '这是后续说明。' * 15
        self.assertEqual(Handler._apply_group_chat_brevity_limit(self.event(), text), '这是第一句。')
        self.assertEqual(Handler._apply_group_chat_brevity_limit(
            self.event(direct=True, message='请详细解释这件事情的原因和步骤'), text),
            '这是第一句。 这是后续说明。')

    def test_long_single_sentence_uses_clause_boundary(self):
        text = '甲' * 20 + '，' + '乙' * 30 + '。'
        self.assertEqual(Handler._apply_group_chat_brevity_limit(self.event(), text), '甲' * 20)

    def test_empty_private_and_whitespace_behavior(self):
        self.assertEqual(Handler._apply_group_chat_brevity_limit(self.event(), ''), '')
        self.assertEqual(Handler._apply_group_chat_brevity_limit(self.event(), '收到。\n  可以。'), '收到。 可以。')
        text = '甲' * 100 + '。第二句。'
        self.assertEqual(Handler._apply_group_chat_brevity_limit(self.event(private=True), text), text)
