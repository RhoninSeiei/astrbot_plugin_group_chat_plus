import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest

spec=importlib.util.spec_from_file_location('native_memory_budget',Path(__file__).parents[1]/'utils/native_memory_budget.py')
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)


CONTENTS=['old fishing preferences '*8,'current telescope preference '*8,'old cooking preferences '*8]


def formatter(memories):
    entries=[f'记忆 #{i} / Memory #{i} (Importance: 0.50)\nTopics: topic\n{memory["content"]}' for i,memory in enumerate(memories,1)]
    text=module.MEMORY_HEADER+'\n'+module.DEFAULT_HEADER_BODY+'\n\n'+'\n\n'.join(entries)+'\n\n'+module.DEFAULT_FOOTER_BODY+'\n'+module.MEMORY_FOOTER
    return text


def memory_part():
    entries=[f'记忆 #{i} / Memory #{i} (Importance: 0.50)\nTopics: topic\n{text}' for i,text in enumerate(CONTENTS,1)]
    return SimpleNamespace(text=formatter([{'content':text} for text in CONTENTS]),_no_save=True),entries


def handoff(req,part):
    return {'version':1,'request':req,'part':part,'memories':[{'content':text} for text in CONTENTS], 'formatter':formatter}


class NativeMemoryBudgetTests(unittest.TestCase):
    def test_unescaped_body_headings_must_not_be_treated_as_entry_boundaries(self):
        fake='记忆 #2 / Memory #2 (Importance: 0.50)\nTopics: telescope\nembedded example telescope '+('data '*35)
        for separator in ['\n','\n\n','\n\n```text\n']:
            text=(module.MEMORY_HEADER+'\n'+module.DEFAULT_HEADER_BODY+'\n\n'
                  +'记忆 #1 / Memory #1 (Importance: 0.50)\nThis entire observation is one fact '+('history '*35)
                  +separator+fake+'\n\n'+module.DEFAULT_FOOTER_BODY+'\n'+module.MEMORY_FOOTER)
            part=SimpleNamespace(text=text,_no_save=True)
            req=SimpleNamespace(extra_user_content_parts=[part])
            result=module.select_native_memory_parts(req,1100,query='telescope')
            self.assertEqual(result['changed'],0)
            self.assertIs(req.extra_user_content_parts[0],part)
            self.assertEqual(part.text,text)

    def test_only_owned_temporary_parts_are_selected_as_complete_entries(self):
        owned,entries=memory_part()
        other=SimpleNamespace(text='other plugin memory',_no_save=True)
        req=SimpleNamespace(extra_user_content_parts=[other,owned],system_prompt='persona',contexts=[{'role':'tool','content':'protocol'}],audio_urls=['audio'])
        budget=len(owned.text)-len(entries[0])-len(entries[2])-2
        data=handoff(req,owned)
        result=module.select_native_memory_parts(req,budget,query='telescope',handoff=data)
        self.assertEqual(result['changed'],1)
        self.assertIs(req.extra_user_content_parts[0],other)
        selected=req.extra_user_content_parts[1]
        self.assertTrue(selected._no_save)
        self.assertIn(CONTENTS[1],selected.text)
        self.assertNotIn(entries[0],selected.text)
        self.assertNotIn(entries[2],selected.text)
        self.assertLessEqual(len(selected.text),budget)
        self.assertIn('fishing',owned.text)
        self.assertEqual(req.system_prompt,'persona')
        self.assertEqual(req.contexts,[{'role':'tool','content':'protocol'}])
        self.assertEqual(req.audio_urls,['audio'])
        # Reusing a consumed handoff cannot further modify the replacement part.
        again=module.select_native_memory_parts(req,budget,query='telescope',handoff=data)
        self.assertEqual(again['changed'],0)
        self.assertIs(req.extra_user_content_parts[1],selected)

    def test_unknown_custom_and_non_temporary_formats_are_preserved(self):
        original,_=memory_part()
        for text,temporary in [(original.text,False),('user prefix\n'+original.text,True),(original.text.replace('CRITICAL RULES:','CUSTOM RULES:'),True),(original.text.replace('Memory #2','Memory #9'),True)]:
            part=SimpleNamespace(text=text,_no_save=temporary)
            req=SimpleNamespace(extra_user_content_parts=[part])
            result=module.select_native_memory_parts(req,1000,query='telescope')
            self.assertEqual(result['changed'],0)
            self.assertIs(req.extra_user_content_parts[0],part)

    def test_too_small_budget_fails_open_and_sufficient_budget_is_noop(self):
        part,_=memory_part()
        for budget in [10,len(part.text)+1]:
            req=SimpleNamespace(extra_user_content_parts=[part])
            result=module.select_native_memory_parts(req,budget)
            self.assertEqual(result['changed'],0)
            self.assertIs(req.extra_user_content_parts[0],part)

    def test_unstructured_native_part_is_preserved_even_with_clear_looking_titles(self):
        part,_=memory_part();req=SimpleNamespace(extra_user_content_parts=[part])
        result=module.select_native_memory_parts(req,1100,query='telescope')
        self.assertEqual(result['reason'],'unstructured_preserved')
        self.assertEqual(result['before_chars'],len(part.text))
        self.assertEqual(result['after_chars'],len(part.text))
        self.assertIs(req.extra_user_content_parts[0],part)

    def test_wrong_request_part_or_formatter_output_never_rewrites(self):
        for mutation in ['request','part','text','formatter']:
            part,_=memory_part();req=SimpleNamespace(extra_user_content_parts=[part]);data=handoff(req,part)
            if mutation=='request':data['request']=object()
            if mutation=='part':data['part']=SimpleNamespace(text=part.text,_no_save=True)
            if mutation=='text':part.text+='unrecognized modification'
            if mutation=='formatter':data['formatter']=lambda rows:'different format'
            result=module.select_native_memory_parts(req,1100,query='telescope',handoff=data)
            self.assertEqual(result['changed'],0)
            self.assertIs(req.extra_user_content_parts[0],part)

    def test_structured_fact_with_fake_headings_stays_whole(self):
        content='完整事实前半段\n\n记忆 #2 / Memory #2 (Importance: 0.50)\nTopics: telescope\n这是同一事实后半段'
        memories=[{'content':content},{'content':'unrelated '+('data '*80)}]
        part=SimpleNamespace(text=formatter(memories),_no_save=True)
        req=SimpleNamespace(extra_user_content_parts=[part])
        data={'version':1,'request':req,'part':part,'memories':memories,'formatter':formatter}
        result=module.select_native_memory_parts(req,len(formatter([memories[0]])),query='telescope',handoff=data)
        self.assertEqual(result['changed'],1)
        self.assertIn(content,req.extra_user_content_parts[0].text)
        self.assertEqual(result['retained_entries'],1)

    def test_structured_budget_too_small_or_formatter_failure_preserves_original(self):
        for fail in [False,True]:
            part,_=memory_part();req=SimpleNamespace(extra_user_content_parts=[part]);data=handoff(req,part)
            if fail:
                calls=[0]
                def unreliable(memories):
                    calls[0]+=1
                    if calls[0]>1:raise ValueError('formatter unavailable')
                    return formatter(memories)
                data['formatter']=unreliable
            result=module.select_native_memory_parts(req,1000 if fail else 1,query='telescope',handoff=data)
            self.assertEqual(result['changed'],0)
            self.assertEqual(result['reason'],'formatter_failed_preserved' if fail else 'budget_unmet_preserved')
            self.assertIs(req.extra_user_content_parts[0],part)


if __name__ == '__main__': unittest.main()
