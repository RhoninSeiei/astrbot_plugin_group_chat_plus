"""Synthetic cross-plugin contract probe in the deployed core image, no network."""
import ast,asyncio,importlib.util,json,sys,tempfile
from pathlib import Path
from types import SimpleNamespace as NS
from astrbot.core.agent.message import TextPart
from astrbot.core.provider.entities import ProviderRequest
from astrbot.core.pipeline.context_utils import call_handler
from astrbot.core.message.components import Plain
ROOT=Path(__file__).resolve().parents[1]
def module(name,path):
 spec=importlib.util.spec_from_file_location(name,path);obj=importlib.util.module_from_spec(spec);sys.modules[name]=obj;spec.loader.exec_module(obj);return obj
bridge_mod=module('native_member_bridge',ROOT/'utils/member_identity.py')
sl=module('native_identity_service',Path(sys.argv[1]))
tree=ast.parse(Path(sys.argv[2]).read_text())
node=next(n for n in ast.walk(tree) if isinstance(n,ast.FunctionDef) and n.name=='_augment_identity_query');node.decorator_list=[]
ns={'json':json};exec(compile(ast.Module(body=[node],type_ignores=[]),str(sys.argv[2]),'exec'),ns)
augment=ns['_augment_identity_query']
class Event:
 plugins_name=None
 def __init__(self,sender,text,key,group='90001'):
  self.sender=sender;self.text=text;self.group=group;self.extras={}
  self.unified_msg_origin='test:GroupMessage:'+group
  self.message_obj=NS(message_id=key)
 def get_platform_id(self):return 'test'
 def get_group_id(self):return self.group
 def get_sender_id(self):return self.sender
 def get_self_id(self):return '99999'
 def get_sender_name(self):return '测试成员'+self.sender
 def get_messages(self):return [Plain(self.text)]
 def get_message_str(self):return self.text
 def get_extra(self,k,d=None):return self.extras.get(k,d)
 def set_extra(self,k,v):self.extras[k]=v
async def main():
 with tempfile.TemporaryDirectory() as temp:
  scope='test:GroupMessage:90001'
  config=NS(identity_mode='active',identity_allowed_sessions=[scope],identity_relation_scopes=[],enable_message_capture=True)
  service=sl.IdentityService(temp,config);await service.start()
  await service.observe_event(Event('10001','请叫我“星河”','confirm'))
  context=NS(get_registered_star=lambda name:NS(activated=True,star_cls=NS(get_identity_context=service.get_identity_context)))
  bridge=bridge_mod.MemberIdentityBridge(context,{'member_identity_sessions':[scope]})
  event=Event('10002','星河之前喜欢什么','query')
  async def handler(current):
   token=bridge.begin(current)
   try:yield ProviderRequest(prompt=current.text,system_prompt='unchanged persona',contexts=[])
   finally:bridge.end(current,token)
  async for req in call_handler(event,handler,_usage_plugin_id='astrbot_plugin_group_chat_plus'):
   await bridge.prepare(event,req)
   assert req.system_prompt=='unchanged persona'
   assert len(req.extra_user_content_parts)==1 and isinstance(req.extra_user_content_parts[0],TextPart) and req.extra_user_content_parts[0]._no_save
   snapshot=event.get_extra(bridge_mod.HANDOFF)['snapshot']
   target=next(s for s in snapshot['subjects'] if s['user_id']=='10001')
   assert 'alias' in target['roles'] and '星河' in target['confirmed_aliases']
   enhanced=augment(event,req,req.prompt,scope,current_query=event.text)
   assert '10001' in enhanced and '星河' in enhanced
   # Simulate an intermediate hook stopping propagation: low-priority hook omitted.
  assert not bridge.entries and event.get_extra(bridge_mod.HANDOFF) is None
  await service.observe_event(Event('10001','别叫我“星河”','deny'))
  later=await service.get_identity_context(Event('10002','星河在哪里','later'))
  assert not any(s['user_id']=='10001' and 'alias' in s['roles'] for s in later['subjects'])
  wrong=await service.get_identity_context(Event('10002','星河在哪里','wrong','90002'))
  assert wrong['status']=='off' and not wrong['subjects']
  await bridge.aclose();await service.close()
 print('IDENTITY_NATIVE_CONTRACT_OK: real core request/temp-part/lifetime, SQLite confirmation-to-query, denial, scope isolation')
asyncio.run(main())
