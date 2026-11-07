"""Controlled r4 native queue/reload integration; never load a live plugin.

Run inside an isolated AstrBot r4 image with --network none and no host mounts:
    PYTHONPATH=/AstrBot python3 -B tests/native_queue_reload_probe.py [output-dir]

Real paths: InternalAgentSubStage.process, SessionLockManager, run_agent,
ToolLoopAgentRunner, MainAgentHooks, call_handler/call_event_hook and registry.
AST-extracted ChatPlus methods are unmodified except removed decorators and
postponed annotations. Only provider, SDK, build selection and persistence are
controlled boundaries. Full plugin initialization is deliberately excluded.
"""
from __future__ import annotations

import ast
import asyncio
import copy
from contextlib import aclosing
from datetime import datetime, timezone
from dataclasses import replace
import hashlib
import importlib.util
import inspect
import json
import logging
from pathlib import Path
import sys
import time
import traceback
import types
from types import SimpleNamespace as NS
from unittest.mock import patch

from astrbot.core.agent.message import TextPart
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.runners.tool_loop_agent_runner import ToolLoopAgentRunner
from astrbot.core.agent.tool import FunctionTool, ToolSet
from astrbot.core.astr_agent_hooks import MainAgentHooks
from astrbot.core.astr_main_agent import MainAgentBuildConfig
from astrbot.core.message.components import Plain, Image, Reply
from astrbot.core.message.message_event_result import MessageChain, MessageEventResult
from astrbot.core.pipeline.context_utils import call_event_hook, call_handler
from astrbot.core.pipeline.process_stage.method.agent_sub_stages import internal
from astrbot.core.pipeline.process_stage import follow_up
from astrbot.core.provider.entities import LLMResponse, ProviderRequest
from astrbot.core.star.star import star_map
from astrbot.core.star.star_handler import EventType, StarHandlerMetadata, star_handlers_registry
from astrbot.core.utils.session_lock import SessionLockManager
from mcp.types import CallToolResult, TextContent


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "gcp_controlled_native"
LOG = logging.getLogger(PACKAGE)
TREE = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
CHATPLUS = next(n for n in TREE.body if isinstance(n, ast.ClassDef) and n.name == "ChatPlus")
SCOPE = "controlled:GroupMessage:queue-g1"
OTHER = "controlled:GroupMessage:queue-g2"
THIRD = "controlled:GroupMessage:queue-g3"


def serializable(value):
    if hasattr(value, "model_dump"):
        return serializable(value.model_dump(mode="json"))
    if isinstance(value, dict): return {key: serializable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)): return [serializable(item) for item in value]
    return value


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


package = types.ModuleType(PACKAGE)
package.__path__ = [str(ROOT)]
sys.modules[PACKAGE] = package
utilities = types.ModuleType(PACKAGE + ".utils")
utilities.__path__ = [str(ROOT / "utils")]
sys.modules[utilities.__name__] = utilities
queue = load(PACKAGE + ".utils.native_queue", "utils/native_queue.py")
lifecycle = load(PACKAGE + ".utils.request_lifecycle", "utils/request_lifecycle.py")
flow_module = load(PACKAGE + ".utils.flow_control", "utils/flow_control.py")
context_module = load(PACKAGE + ".utils.context_manager", "utils/context_manager.py")
cleaner = load(PACKAGE + ".utils.message_cleaner", "utils/message_cleaner.py")
processor = load(PACKAGE + ".utils.message_processor", "utils/message_processor.py")
rewriter = load(PACKAGE + ".utils.system_prompt_rewriter", "utils/system_prompt_rewriter.py")
compat = load(PACKAGE + ".utils.request_compatibility", "utils/request_compatibility.py")
policy = load(PACKAGE + ".utils.tool_policy", "utils/tool_policy.py")

# Import only literal real constants; importing reply_handler would broaden the
# harness into unrelated provider/tool integration. No tested method is edited.
reply = types.ModuleType(PACKAGE + ".utils.reply_handler")
reply_tree = ast.parse((ROOT / "utils/reply_handler.py").read_text(encoding="utf-8"))
for statement in reply_tree.body:
    if isinstance(statement, ast.Assign):
        for target in statement.targets:
            if isinstance(target, ast.Name) and target.id.startswith("PLUGIN_"):
                try:
                    setattr(reply, target.id, ast.literal_eval(statement.value))
                except ValueError:
                    pass
sys.modules[reply.__name__] = reply


def extract_class(name, namespace):
    names = {"on_group_message", "_claim_processing_slot", "_can_delegate_native_queue",
             "_read_native_committed_history",
             "_request_callback_owner", "on_llm_request", "_on_llm_request_impl",
             "on_llm_response", "after_message_sent", "terminate",
             "on_flow_tool_start", "on_flow_tool_end"}
    methods = []
    for original in CHATPLUS.body:
        if getattr(original, "name", "") in names:
            node = copy.deepcopy(original)
            node.decorator_list = []
            methods.append(node)
    assert {n.name for n in methods} == names
    cls = ast.ClassDef(name=name, bases=[], keywords=[], body=methods, decorator_list=[])
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), cls], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(ROOT / "main.py"), "exec"), namespace)
    return namespace[name]


class Event:
    """Fake platform SDK; all sends remain local and are recorded."""
    def __init__(self, key, scope=SCOPE, sender=None, text=None, components=None):
        self.key = key
        self.unified_msg_origin = scope
        self.session_id = scope
        self.message_str = text or f"RAW_REQUEST_{key}"
        self.sender = sender or f"user-{key}"
        self.message_obj = NS(message_id=key, message=components or [Plain(self.message_str)])
        self.extras = {}
        self._result = None
        self.plugins_name = ["*"]
        self.call_llm = False
        self._has_send_oper = False
        self.platform_meta = NS(support_streaming_message=False)
        self.trace_rows = []
        self.trace = NS(record=lambda *a, **kw: self.trace_rows.append((a, kw)))
        self.sent = []
        self.typing = []

    def get_extra(self, key, default=None): return self.extras.get(key, default)
    def set_extra(self, key, value): self.extras[key] = value
    def is_private_chat(self): return False
    def is_stopped(self): return False
    def get_sender_id(self): return self.sender
    def get_sender_name(self): return self.sender
    def get_self_id(self): return "controlled-bot"
    def get_group_id(self): return self.unified_msg_origin.rsplit(":", 1)[1]
    def get_platform_id(self): return "controlled"
    def get_platform_name(self): return "controlled"
    def get_message_str(self): return self.message_str
    def get_message_outline(self): return self.message_str
    def get_messages(self): return self.message_obj.message
    def get_result(self): return self._result
    def set_result(self, value): self._result = value
    def clear_result(self): self._result = None
    def _outline_chain(self, chain): return "controlled local SDK chain"
    async def send_typing(self): self.typing.append("start")
    async def stop_typing(self): self.typing.append("stop")
    async def send(self, chain, **kwargs):
        self.sent.append("".join(getattr(c, "text", "") for c in chain.chain))
        self._has_send_oper = True


class HistorySink:
    """Persistence boundary shared by generations, with actual formatter."""
    format_context_for_ai = staticmethod(context_module.ContextManager.format_context_for_ai)

    def __init__(self):
        self.messages = {}
        self.saves = []
        self.custom_saves = []
        self.reads = []
        self.native_saves = []

    def get_history_cutoff(self, group): return 0
    _official_history_to_message = staticmethod(context_module.ContextManager._official_history_to_message)
    async def get(self, platform_id, user_id, page, page_size):
        self.reads.append({"group": user_id, "page_size": page_size})
        return copy.deepcopy(self.messages.get(user_id, [])[-page_size:])

    async def insert(self, platform_id, user_id, content, sender_id, sender_name):
        messages = self.messages.setdefault(user_id, [])
        record = NS(id=1 + sum(len(items) for items in self.messages.values()),
                    content=copy.deepcopy(content), sender_id=sender_id, sender_name=sender_name,
                    created_at=datetime.now(timezone.utc))
        messages.append(record)
        return record

    async def save_bot_message(self, event, text, context):
        self.custom_saves.append((event.key, text))
        await self.insert(event.get_platform_id(), event.get_group_id(),
                          [{"type": "text", "data": {"text": text}}], event.get_self_id(), "controlled-bot")

    async def save_to_official_conversation_with_cache(self, event, cached, user, bot, context, **kwargs):
        assert not cached, "Controlled tests have no pending cached inputs"
        assert user and bot
        assert event.sent and event.sent[-1] == bot, "History must contain the submitted SDK text"
        assert not any(row[0] == event.key for row in self.saves), "duplicate history save"
        self.saves.append((event.key, user, bot))
        return True


def native_recipe_block(plugin, event, *, images=None, windows=None, history=None):
    """Execute actual rich-payload exclusion gate from ChatPlus preparation."""
    block = next(n for n in ast.walk(CHATPLUS) if isinstance(n, ast.If)
                 and "getattr(self, '_native_queue_capability'" in ast.unparse(n.test)
                 and any(isinstance(c, ast.Call) and ast.unparse(c.func) == "RequestHistoryRecipe.capture"
                         for c in ast.walk(n)))
    fragment = "fixture-fragment"
    namespace = {"self": plugin, "event": event, "image_urls": images or [],
                 "window_buffered_msgs": windows or [], "history_messages": history or [],
                 "message_text_for_ai": event.message_str, "bot_id": event.get_self_id(),
                 "formatted_context": fragment, "chat_id": event.get_group_id(),
                 "RequestHistoryRecipe": queue.RequestHistoryRecipe, "ContextManager": plugin.sink}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[copy.deepcopy(block)], type_ignores=[])),
                 str(ROOT / "main.py"), "exec"), namespace)
    return event.get_extra("_gcp_native_history_recipe")


class Harness:
    def __init__(self):
        self.sink = HistorySink()
        self.metrics = []
        self.plugins = []
        self.claims = []
        self.routes = []
        self.provider_calls = []
        self.active = {}
        self.maximum = {}
        self.entered = {}
        self.release = {}
        self.runners = {}
        self.no_terminal = set()
        self.follow_tool = set()
        self.tool_entered = {}
        self.tool_release = {}
        self.native_hooks = MainAgentHooks()
        self.core_saves = []
        self.save_entered, self.save_release = {}, {}
        self.stage = internal.InternalAgentSubStage()
        self.stage.streaming_response = False
        self.stage.show_reasoning = False
        self.stage.unsupported_streaming_strategy = "turn_off"
        self.stage.max_step = 4
        self.stage.show_tool_use = False
        self.stage.show_tool_call_result = False
        self.stage.buffer_intermediate_messages = False
        self.stage.main_agent_cfg = MainAgentBuildConfig(tool_call_timeout=5, provider_settings={})
        self.context = NS(message_history_manager=self.sink,
                          get_config=lambda **kw: {"provider_settings": {"agent_runner_type": "local", "enable": True}})
        self.stage.ctx = NS(plugin_manager=NS(context=self.context))
        self.stage._save_to_history = self.core_save

    async def core_save(self, event, req, response, messages, stats, **kw):
        self.core_saves.append(event.key)
        self.save_entered.setdefault(event.key, asyncio.Event()).set()
        if event.key in self.save_release: await self.save_release[event.key].wait()

    def plugin(self, generation, ready=True):
        # Each generation loads independent lifecycle code, as a real reload does.
        lease_module = load(PACKAGE + ".lease_" + generation, "utils/request_lifecycle.py")
        namespace = {"__name__": PACKAGE + ".main_" + generation, "__package__": PACKAGE,
                     "asyncio": asyncio, "time": time, "logger": LOG,
                     "LEASE_KEY": lease_module.LEASE_KEY,
                     "resolve_request_owner": lease_module.resolve_request_owner,
                     "can_delegate_native": queue.can_delegate_native,
                     "ContextManager": self.sink, "MessageCleaner": cleaner.MessageCleaner,
                     "MessageProcessor": processor.MessageProcessor,
                     "SystemPromptRewriter": rewriter.SystemPromptRewriter,
                     "ToolPolicy": policy.ToolPolicy,
                     "enabled_for": compat.enabled_for, "merge_request": compat.merge_request,
                     "split_static_instructions": compat.split_static_instructions,
                     "REQUEST_BASELINE": compat.BASELINE, "STATIC_INSTRUCTIONS": compat.STATIC_INSTRUCTIONS,
                     "PROGRESS_INSTRUCTION": flow_module.PROGRESS_INSTRUCTION,
                     **{key: value for key, value in vars(reply).items() if key.startswith("PLUGIN_")},
                     "PLUGIN_PENDING_MAIN_MODEL_DECISION": "_group_chat_plus_pending_main_model_decision"}
        cls = extract_class("ControlledChatPlus_" + generation, namespace)
        p = cls()
        p.generation, p.sink = generation, self.sink
        p.context, p.config = self.context, {"flow_metrics_sessions": [SCOPE, OTHER, THIRD]}
        p.runtime_state = NS(generation=generation)
        p.request_lifecycle = lease_module.RequestLifecycle(p, p.runtime_state)
        p.request_lifecycle.attach(self.context)
        p.flow_control = flow_module.FlowController(p.config, self.metrics.append, part_factory=TextPart)
        p._termination_lock = asyncio.Lock()
        p._termination_complete = False
        p._native_queue_capability = queue.detect_native_queue_capability(internal)
        assert p._native_queue_capability.supported, p._native_queue_capability
        p.enable_group_chat = True
        p.debug_mode = False
        p.member_identity_bridge = None
        p.enable_idle_cache_flush = p.enable_welcome_message_parsing = p.enable_forward_message_parsing = False
        p._seen_message_ids, p.command_messages = {}, {}
        p._request_message_owners = {}
        p._native_history_locks = {}
        p.concurrent_lock = asyncio.Lock()
        p.concurrent_mode = "legacy"
        p.concurrent_wait_interval, p.concurrent_wait_max_loops = 1.0, 14
        p.processing_sessions = {}
        p._message_cache_snapshots, p._smart_batch_snapshots = {}, {}
        p._duplicate_blocked_messages, p._pending_bot_replies, p.raw_reply_cache = {}, {}, {}
        p._agent_done_flags, p._saved_messages = set(), {}
        p.pending_messages_cache, p.proactive_processing_sessions = {}, {}
        p.recent_replies_cache = {}
        p.duplicate_filter_check_count, p._DUPLICATE_CACHE_SIZE_LIMIT = 3, 100
        p.humanize_mode_enabled = False
        p.proactive_enabled = False
        p._idle_flush_tasks, p._idle_flush_meta = {}, {}
        p.include_timestamp, p.include_sender_info = False, True
        p.max_context_messages = 100
        p.enable_tools_reminder = False
        p._is_vacuum_message = lambda e: False
        p._check_compliance_status = lambda: None
        p._get_message_id = lambda e: e.key
        p._is_enabled = lambda e: True
        p._is_user_blacklisted = p._is_at_all_message = p._should_ignore_at_all = p._should_ignore_at_others = lambda e: False
        p._check_poke_message = lambda e: {}
        p._sanitize_llm_request_images = lambda *a, **kw: None
        p._capture_step_image_request_references = lambda *a, **kw: None
        p._filter_step_image_tools_for_request = lambda e, t: (t, [])
        p._log_step_image_tools_filtered = lambda *a, **kw: None
        p._infer_step_image_action = lambda e: None
        p._build_step_image_tool_directive = lambda *a, **kw: ""
        p._compute_session_integrity = lambda chat: None
        p._build_interleaved_tool_reply = lambda *a: None
        p._append_persistent_event_text = lambda text, extra: text
        p.content_filter = NS(process_for_save=lambda text: text)
        p.cache_manager = NS(prepare_cache_for_save=lambda **kw: [], clear_saved_cache=lambda **kw: None,
                             prepare_window_buffered_for_save=lambda **kw: [])
        async def successful_effects(*args): pass
        p._apply_successful_reply_effects = successful_effects
        async def process_message(event):
            records = self.sink.messages.get(event.get_group_id(), [])
            if p.max_context_messages > 0: records = records[-p.max_context_messages:]
            elif p.max_context_messages == 0: records = []
            history = [self.sink._official_history_to_message(record, event.get_platform_name(), False,
                        event.get_group_id(), event.get_self_id())
                       for record in records]
            fragment = await self.sink.format_context_for_ai(history, event.message_str, event.get_self_id(),
                                                           include_timestamp=False, include_sender_info=True)
            recipe = queue.RequestHistoryRecipe.capture(event, history_messages=history,
                current_message=event.message_str, bot_id=event.get_self_id(), rendered_fragment=fragment,
                formatter=self.sink.format_context_for_ai, reset_token=0,
                include_timestamp=False, include_sender_info=True)
            event.set_extra("_gcp_native_history_recipe", recipe)
            event.set_extra("_gcp_native_history_fragment", fragment)
            event.set_extra("_gcp_native_history_candidate", True)
            sleep_calls = []
            async def forbidden_sleep(delay):
                sleep_calls.append(delay)
                raise AssertionError("Native admission must not poll in plugin")
            with patch.object(asyncio, "sleep", forbidden_sleep):
                claimed = await p._claim_processing_slot(event, event.get_group_id(), event.key)
            assert claimed and not sleep_calls
            self.claims.append(event.key)
            assert await context_module.ContextManager.save_user_message(
                event, event.message_str, self.context, skip_custom_storage=True)
            marker = event.get_extra("_gcp_pending_official_user_row")
            assert marker and marker["event"] is event and type(marker["row_id"]) is int
            p._message_cache_snapshots[event.key] = {"message_id": event.key, "content": event.message_str,
                "sender_id": event.sender, "sender_name": event.sender, "timestamp": 100.0}
            p._smart_batch_snapshots[event.key] = ["snapshot-retained-until-owner-finally"]
            for key, value in [(reply.PLUGIN_REQUEST_MARKER, True), (reply.PLUGIN_CUSTOM_PROMPT, fragment),
                               (reply.PLUGIN_CUSTOM_CONTEXTS, []), (reply.PLUGIN_CUSTOM_SYSTEM_PROMPT, "controlled persona")]:
                event.set_extra(key, value)
            yield ProviderRequest(prompt=event.message_str, contexts=[], system_prompt="controlled persona")
        p._process_message = process_message
        if ready: p.request_lifecycle.mark_ready()
        self.plugins.append(p)
        return p

    def install_registry(self, plugin):
        star_handlers_registry.clear()
        star_map.clear()
        module = "controlled_registry_" + plugin.generation
        star_map[module] = NS(name="controlled-gcp", display_name="controlled-gcp", activated=True, reserved=False)
        for kind, name in [(EventType.OnLLMRequestEvent, "on_llm_request"),
                           (EventType.OnLLMResponseEvent, "on_llm_response"),
                           (EventType.OnUsingLLMToolEvent, "on_flow_tool_start"),
                           (EventType.OnLLMToolRespondEvent, "on_flow_tool_end"),
                           (EventType.OnAfterMessageSentEvent, "after_message_sent")]:
            async def handler(event, *args, name=name):
                owner = plugin._request_callback_owner(event)
                self.routes.append({"key": event.key, "hook": name, "registry": plugin.generation,
                                    "owner": getattr(owner, "generation", None)})
                await getattr(plugin, name)(event, *args)
            star_handlers_registry.append(StarHandlerMetadata(kind, module + "." + name, name, module, handler, []))

    async def build(self, *, event, **kw):
        req = event.get_extra("provider_request")
        assert isinstance(req, ProviderRequest)
        runner = ToolLoopAgentRunner()
        harness = self
        class Provider:
            provider_config = {"id": "controlled-no-network", "max_context_tokens": 0}
            def get_model(self): return "controlled-script"
            def meta(self): return NS(id="controlled-no-network", type="controlled", model="controlled-script")
            async def text_chat(self, **kwargs):
                harness.active[event.unified_msg_origin] = harness.active.get(event.unified_msg_origin, 0) + 1
                harness.maximum[event.unified_msg_origin] = max(harness.maximum.get(event.unified_msg_origin, 0), harness.active[event.unified_msg_origin])
                harness.provider_calls.append({"key": event.key, "prompt": kwargs.get("prompt"),
                    "contexts": serializable(copy.deepcopy(kwargs.get("contexts", [])))})
                harness.entered.setdefault(event.key, asyncio.Event()).set()
                try:
                    if event.key in harness.release: await harness.release[event.key].wait()
                    n = sum(c["key"] == event.key for c in harness.provider_calls)
                    if event.key in harness.follow_tool and n == 1:
                        return LLMResponse(role="assistant", completion_text="", tools_call_name=["controlled_wait"],
                            tools_call_args=[{}], tools_call_ids=["controlled-tool-1"])
                    return LLMResponse(role="assistant", completion_text=f"SDK_REPLY_{event.key}")
                finally:
                    harness.active[event.unified_msg_origin] -= 1
        provider = Provider()
        class Executor:
            async def execute(self, **kwargs):
                harness.tool_entered.setdefault(event.key, asyncio.Event()).set()
                await harness.tool_release[event.key].wait()
                yield CallToolResult(content=[TextContent(type="text", text="controlled tool completed")])
        if event.key in self.follow_tool:
            req.func_tool = ToolSet(tools=[FunctionTool(name="controlled_wait", description="local controlled wait",
                parameters={"type": "object", "properties": {}})])
        class Hooks(MainAgentHooks):
            async def on_agent_done(self, run_context, response):
                if event.key not in harness.no_terminal:
                    await super().on_agent_done(run_context, response)
        async def reset():
            await runner.reset(provider=provider, request=req, run_context=ContextWrapper(context=NS(event=event)),
                tool_executor=Executor(), agent_hooks=Hooks(), streaming=False, fallback_providers=[],
                request_max_retries=0, enforce_max_turns=-1)
        self.runners[event.key] = runner
        return NS(agent_runner=runner, provider_request=req, provider=provider, reset_coro=reset())

    async def invoke(self, plugin, event):
        async with aclosing(call_handler(event, plugin.on_group_message, _usage_plugin_id="controlled-gcp")) as handler:
            async for req in handler:
                if not isinstance(req, ProviderRequest): continue
                event.set_extra("provider_request", req)
                async with aclosing(self.stage.process(event, "")) as stages:
                    async for _ in stages:
                        result = event.get_result()
                        if result and result.chain:
                            await event.send(MessageChain(result.chain))
                            await call_event_hook(event, EventType.OnAfterMessageSentEvent)

    async def wait_entered(self, key, tools=False):
        table = self.tool_entered if tools else self.entered
        await asyncio.wait_for(table.setdefault(key, asyncio.Event()).wait(), 8)

    async def cleanup(self):
        for plugin in self.plugins: await plugin.terminate()


async def settled():
    # Event-loop scheduling only; no production-scale wait or timing threshold.
    for _ in range(5): await asyncio.sleep(0)


def counts(harness, key):
    # Request IDs are generated; route per event is independently counted.
    return {"key": key, "provider_calls": sum(c["key"] == key for c in harness.provider_calls),
            "official_saves": sum(row[0] == key for row in harness.sink.saves),
            "core_saves": harness.core_saves.count(key),
            "terminal_hooks": sum(row["key"] == key and row["hook"] == "on_llm_response" and row["owner"] is not None for row in harness.routes),
            "after_send_hooks": sum(row["key"] == key and row["hook"] == "after_message_sent" and row["owner"] is not None for row in harness.routes)}


async def queue_cases(size):
    h = Harness()
    p = h.plugin("queue")
    h.install_registry(p)
    for _ in range(2):
        await h.sink.insert("controlled", "queue-g1", [{"type": "text", "data": {"text": "SAME_TEXT_DISTINCT_ROW"}}],
                            "historical-user", "historical-user")
    keys = [f"QUEUE{size}_{i}" for i in range(size)]
    events = [Event(key) for key in keys]
    h.release[keys[0]] = asyncio.Event()
    tasks = [asyncio.create_task(h.invoke(p, events[0]))]
    await h.wait_entered(keys[0])
    tasks.extend(asyncio.create_task(h.invoke(p, event)) for event in events[1:])
    await settled()
    assert h.claims == keys, h.claims
    assert len(h.provider_calls) == 1, h.provider_calls
    # A separate UMO must finish while the first UMO is held.
    outsider = Event(f"OUTSIDE{size}", OTHER)
    await asyncio.wait_for(h.invoke(p, outsider), 8)
    assert events[0].sent == [] and outsider.sent == [f"SDK_REPLY_OUTSIDE{size}"]
    assert all(event.get_extra("_gcp_native_history_lock") is events[0].get_extra("_gcp_native_history_lock") for event in events)
    assert outsider.get_extra("_gcp_native_history_lock") is not events[0].get_extra("_gcp_native_history_lock")
    h.release[keys[0]].set()
    await asyncio.wait_for(asyncio.gather(*tasks), 15)
    assert h.maximum[SCOPE] == 1
    for index, key in enumerate(keys):
        value = counts(h, key)
        assert value == {"key": key, "provider_calls": 1, "official_saves": 1, "core_saves": 1,
                         "terminal_hooks": 1, "after_send_hooks": 1}, value
        provider_text = json.dumps(next(c for c in h.provider_calls if c["key"] == key), ensure_ascii=False)
        for earlier in keys[:index]:
            assert provider_text.count(f"RAW_REQUEST_{earlier}") == 1, provider_text
            assert provider_text.count(f"SDK_REPLY_{earlier}") == 1, provider_text
        assert provider_text.count(f"RAW_REQUEST_{key}") == 1
        for later in keys[index + 1:]:
            assert f"RAW_REQUEST_{later}" not in provider_text, provider_text
        assert provider_text.count("SAME_TEXT_DISTINCT_ROW") == 2, provider_text
    assert not p.processing_sessions and not p.request_lifecycle.active
    await h.cleanup()
    return {"case": f"same_group_{size}", "max_formal_concurrency": h.maximum[SCOPE],
            "plugin_admission_sleeps": 0, "cross_group_completed_while_blocked": True,
            "future_pending_users_excluded": True, "same_text_distinct_rows_retained": 2,
            "requests": [counts(h, key) for key in keys], "provider_inputs": h.provider_calls}


async def reload_case():
    h = Harness()
    old, new, newest = [h.plugin(name) for name in ["generation1", "generation2", "generation3"]]
    h.install_registry(old)
    event = Event("RELOAD_OLD")
    h.release[event.key] = asyncio.Event()
    request = asyncio.create_task(h.invoke(old, event))
    await h.wait_entered(event.key)
    queued = Event("RELOAD_QUEUED")
    queued_request = asyncio.create_task(h.invoke(old, queued))
    await settled()
    assert queued.key in h.claims and queued.key not in h.runners
    # A duplicate delivery and a stale same-event task must preserve snapshots.
    snapshot = copy.deepcopy(old._message_cache_snapshots)
    smart = copy.deepcopy(old._smart_batch_snapshots)
    old._seen_message_ids.clear()  # Expired dedup TTL must not defeat live owner protection.
    duplicate = Event(event.key)
    await h.invoke(old, duplicate)
    await h.invoke(old, event)
    assert old._message_cache_snapshots == snapshot and old._smart_batch_snapshots == smart
    assert event.key in old.processing_sessions
    drain = asyncio.create_task(old.terminate())
    await settled()
    assert not old.request_lifecycle.accepting and not drain.done()
    assert not old.flow_control.closed and not old._termination_complete
    assert not awaitable_closed(old)
    assert len(old.request_lifecycle.active) == 2
    # Register two subsequent generations while the old lease is still active.
    h.install_registry(new)
    replacement = Event("RELOAD_NEW", OTHER)
    h.release[replacement.key] = asyncio.Event()
    replacement_request = asyncio.create_task(h.invoke(new, replacement))
    await h.wait_entered(replacement.key)
    drain_new = asyncio.create_task(new.terminate())
    await settled()
    assert not drain_new.done() and not new.flow_control.closed
    h.install_registry(newest)
    fresh = Event("RELOAD_NEWEST", THIRD)
    await h.invoke(newest, fresh)
    # This task was created outside the request context. A copied event or a
    # matching event/user ID must never grant access to the old request owner.
    lease = event.get_extra(lifecycle.LEASE_KEY)
    assert not old.request_lifecycle.release(event, lease)
    await call_event_hook(event, EventType.OnLLMResponseEvent,
                          LLMResponse(role="assistant", completion_text="unrelated callback"))
    assert event.key not in old._agent_done_flags
    assert h.routes[-1]["owner"] is None
    h.release[event.key].set()
    h.release[replacement.key].set()
    await asyncio.wait_for(request, 12)
    await asyncio.wait_for(queued_request, 12)
    await asyncio.wait_for(replacement_request, 12)
    await asyncio.wait_for(drain, 8)
    await asyncio.wait_for(drain_new, 8)
    assert old.flow_control.closed and old._termination_complete
    assert counts(h, event.key)["official_saves"] == 1
    late = [r for r in h.routes if r["key"] == event.key and r["hook"] in {"on_llm_response", "after_message_sent"} and r["owner"] is not None]
    assert len(late) == 2 and all(r["registry"] == "generation3" and r["owner"] == "generation1" for r in late), late
    assert event.sent == ["SDK_REPLY_RELOAD_OLD"]
    queued_routes = [r for r in h.routes if r["key"] == queued.key]
    assert len(queued_routes) == 3 and all(r["registry"] == "generation3" and r["owner"] == "generation1" for r in queued_routes), queued_routes
    generation2_late = [r for r in h.routes if r["key"] == replacement.key and r["hook"] in {"on_llm_response", "after_message_sent"}]
    assert len(generation2_late) == 2 and all(r["registry"] == "generation3" and r["owner"] == "generation2" for r in generation2_late)
    assert all(counts(h, e.key)["official_saves"] == 1 for e in [event, queued, replacement, fresh])
    assert not old.request_lifecycle.active and event.get_extra(lifecycle.LEASE_KEY) is None
    await h.cleanup()
    return {"case": "reload_two_generations", "drain_blocked_while_active": True,
            "resources_closed_after_request_finally": True, "duplicate_snapshots_preserved": True,
            "expired_dedup_active_owner_preserved": True, "unrelated_task_hook_rejected": True,
            "queued_request_hook_routes_to_original_owner": True,
            "requests": [counts(h, e.key) for e in [event, queued, replacement, fresh]], "routes": h.routes}


async def missing_row_identity_case():
    h = Harness(); p = h.plugin("missingrow"); h.install_registry(p)
    first, second = Event("IDENTITY_FIRST"), Event("IDENTITY_SECOND")
    h.release[first.key] = asyncio.Event()
    a = asyncio.create_task(h.invoke(p, first)); await h.wait_entered(first.key)
    b = asyncio.create_task(h.invoke(p, second)); await settled()
    original_recipe = second.get_extra("_gcp_native_history_recipe")
    assert original_recipe is not None
    assert second.get_extra("_gcp_pending_official_user_row")
    second.set_extra("_gcp_pending_official_user_row", None)
    h.release[first.key].set()
    await asyncio.wait_for(asyncio.gather(a, b), 12)
    sent = next(c for c in h.provider_calls if c["key"] == second.key)
    text = json.dumps(sent, ensure_ascii=False)
    assert text.count("RAW_REQUEST_IDENTITY_SECOND") == 1
    assert "SDK_REPLY_IDENTITY_FIRST" not in text, "Missing exact storage identity must preserve original snapshot"
    assert counts(h, second.key)["official_saves"] == 1
    await h.cleanup()
    return {"case": "missing_current_row_identity_fallback", "original_payload_preserved": True,
            "current_input_count": 1, "provider_input": sent}


async def one_message_history_window_case():
    h = Harness(); p = h.plugin("limitone"); h.install_registry(p)
    p.max_context_messages = 1
    first, second, third = [Event("LIMIT1_" + key) for key in ["A", "B", "C"]]
    # Hold the REAL core session lock after A's SDK/official save. B and C then
    # insert their pending user rows after A's committed bot row. A read(limit=1)
    # before exclusion would therefore select only C and lose A's sent reply.
    h.save_release[first.key] = asyncio.Event()
    a = asyncio.create_task(h.invoke(p, first))
    await asyncio.wait_for(h.save_entered.setdefault(first.key, asyncio.Event()).wait(), 8)
    assert first.sent == ["SDK_REPLY_LIMIT1_A"]
    b = asyncio.create_task(h.invoke(p, second)); c = asyncio.create_task(h.invoke(p, third))
    await settled()
    assert len(h.provider_calls) == 1 and h.claims == [e.key for e in [first, second, third]]
    h.save_release[first.key].set()
    await asyncio.wait_for(asyncio.gather(a, b, c), 15)
    for event, earlier in [(second, first), (third, second)]:
        sent = next(row for row in h.provider_calls if row["key"] == event.key)
        text = json.dumps(sent, ensure_ascii=False)
        assert text.count("SDK_REPLY_" + earlier.key) == 1, text
        assert text.count("RAW_REQUEST_" + event.key) == 1, text
        assert "RAW_REQUEST_" + earlier.key not in text, "limit=1 must retain only the newest committed row"
    b_input = json.dumps(next(row for row in h.provider_calls if row["key"] == second.key))
    assert "RAW_REQUEST_LIMIT1_C" not in b_input
    assert h.sink.reads[1]["page_size"] == 3, h.sink.reads
    assert all(counts(h, e.key)["official_saves"] == 1 for e in [first, second, third])
    await h.cleanup()
    return {"case": "limit_one_with_multiple_pending_rows", "history_limit": 1,
            "pending_rows_cannot_evict_committed_reply": True, "history_reads": h.sink.reads,
            "provider_inputs": h.provider_calls}


async def incoming_during_reload_case():
    h = Harness(); old = h.plugin("intake_old"); h.install_registry(old)
    first, incoming = Event("INTAKE_ACTIVE"), Event("INTAKE_DURING_DRAIN")
    h.release[first.key] = asyncio.Event()
    a = asyncio.create_task(h.invoke(old, first)); await h.wait_entered(first.key)
    retiring = asyncio.create_task(old.terminate()); await settled()
    b = asyncio.create_task(h.invoke(old, incoming)); await settled()
    assert not b.done() and not retiring.done()
    assert len(old.request_lifecycle.active) == 1
    assert incoming.get_extra(lifecycle.LEASE_KEY) is None
    assert incoming.key not in old._seen_message_ids and incoming.key not in h.claims
    successor = h.plugin("intake_successor", ready=False)
    h.install_registry(successor)
    h.release[first.key].set()
    await asyncio.wait_for(a, 12)
    await asyncio.wait_for(retiring, 8)
    await settled()
    assert old.request_lifecycle._retired.is_set() and old.flow_control.closed
    assert not b.done() and incoming.key not in h.runners, "Uninitialized successor cannot accept work"
    successor.request_lifecycle.mark_ready()
    await asyncio.wait_for(b, 12)
    assert h.claims.count(incoming.key) == 1
    assert counts(h, incoming.key) == {"key": incoming.key, "provider_calls": 1,
        "official_saves": 1, "core_saves": 1, "terminal_hooks": 1, "after_send_hooks": 1}
    incoming_routes = [row for row in h.routes if row["key"] == incoming.key]
    assert all(row["owner"] == "intake_successor" for row in incoming_routes)
    assert incoming.get_extra(lifecycle.LEASE_KEY) is None
    assert not old.request_lifecycle.active and not successor.request_lifecycle.active
    await h.cleanup()
    return {"case": "incoming_during_reload_waits_for_ready_successor",
            "old_drain_does_not_wait_for_new_arrival": True, "unready_successor_not_called": True,
            "new_arrival_processed_once": True, "request": counts(h, incoming.key), "routes": h.routes}


async def cancelled_reload_case():
    h = Harness(); old = h.plugin("cancel_reload"); h.install_registry(old)
    first, incoming = Event("CANCEL_RELOAD_ACTIVE"), Event("CANCEL_RELOAD_INCOMING")
    h.release[first.key] = asyncio.Event()
    a = asyncio.create_task(h.invoke(old, first)); await h.wait_entered(first.key)
    retiring = asyncio.create_task(old.terminate()); await settled()
    b = asyncio.create_task(h.invoke(old, incoming)); await settled()
    assert not retiring.done() and not b.done() and len(old.request_lifecycle.active) == 1
    retiring.cancel()
    try: await retiring
    except asyncio.CancelledError: pass
    else: raise AssertionError("Reload cancellation must propagate to its caller")
    await settled()
    assert not a.done(), "Cancelling reload must not cancel active requests"
    assert old.request_lifecycle.accepting and not old.flow_control.closed
    assert not old._termination_complete and not awaitable_closed(old)
    assert incoming.key in h.claims, "Waiting incoming event must resume on recovered owner"
    assert len(old.request_lifecycle.active) == 2
    assert incoming.key not in h.runners, "Resumed input still respects the real core session lock"
    h.release[first.key].set()
    await asyncio.wait_for(asyncio.gather(a, b), 15)
    post_cancel = Event("CANCEL_RELOAD_STILL_USABLE")
    await asyncio.wait_for(h.invoke(old, post_cancel), 8)
    for event in [first, incoming, post_cancel]:
        assert counts(h, event.key) == {"key": event.key, "provider_calls": 1,
            "official_saves": 1, "core_saves": 1, "terminal_hooks": 1, "after_send_hooks": 1}
    assert all(row["owner"] == "cancel_reload" for row in h.routes)
    assert not old.flow_control.closed and not old.request_lifecycle.active
    await h.cleanup()
    return {"case": "cancelled_reload_restores_original_owner_intake",
            "active_request_not_cancelled": True, "resources_remain_open_until_later_termination": True,
            "waiting_input_resumed_without_successor": True, "original_owner_remains_usable": True,
            "requests": [counts(h, e.key) for e in [first, incoming, post_cancel]], "routes": h.routes}


def awaitable_closed(plugin):
    return bool(getattr(plugin, "_image_tools_terminated", False))


async def cancellation_case():
    h = Harness(); p = h.plugin("cancel"); h.install_registry(p)
    first, waiting = Event("CANCEL_RUNNING"), Event("CANCEL_WAITING")
    h.release[first.key] = asyncio.Event()
    a = asyncio.create_task(h.invoke(p, first)); await h.wait_entered(first.key)
    b = asyncio.create_task(h.invoke(p, waiting)); await settled()
    assert waiting.key in h.claims
    b.cancel()
    try: await b
    except asyncio.CancelledError: pass
    else: raise AssertionError("waiting cancellation lost")
    a.cancel()
    try: await a
    except asyncio.CancelledError: pass
    else: raise AssertionError("active cancellation lost")
    assert not p.request_lifecycle.active and not p.processing_sessions
    assert not p._message_cache_snapshots and not p._smart_batch_snapshots
    assert not h.sink.saves
    await h.cleanup()
    return {"case": "cancel_active_and_waiter", "lease_and_snapshot_cleanup": True,
            "requests": [counts(h, e.key) for e in [first, waiting]]}


async def incomplete_case():
    h = Harness(); p = h.plugin("incomplete"); h.install_registry(p)
    event = Event("NO_TERMINAL"); h.no_terminal.add(event.key)
    await h.invoke(p, event)
    await p.terminate()
    complete = [row["outcome"] for row in h.metrics if row["stage"] == "complete"]
    assert complete == ["incomplete_agent"], complete
    assert counts(h, event.key)["terminal_hooks"] == 0
    assert not h.sink.saves and event.sent == ["SDK_REPLY_NO_TERMINAL"]
    return {"case": "sdk_send_without_terminal", "outcomes": complete,
            "request": counts(h, event.key), "sdk_send_count": len(event.sent)}


async def follow_up_case():
    h = Harness(); p = h.plugin("followup"); h.install_registry(p)
    first = Event("FOLLOW_MAIN", sender="same-controlled-user")
    follow = Event("FOLLOW_RAW", sender="same-controlled-user", text="RAW_FOLLOWUP_SENTINEL")
    h.follow_tool.add(first.key); h.tool_release[first.key] = asyncio.Event()
    a = asyncio.create_task(h.invoke(p, first)); await h.wait_entered(first.key, tools=True)
    b = asyncio.create_task(h.invoke(p, follow)); await settled()
    assert not follow.get_extra("_follow_up_captured"), "core has not consumed ticket yet"
    assert h.runners[first.key]._pending_follow_ups, "real active runner did not receive follow-up"
    h.tool_release[first.key].set()
    await asyncio.wait_for(asyncio.gather(a, b), 15)
    assert follow.get_extra("_follow_up_captured"), "real internal capture branch did not run"
    assert follow.key not in h.runners and follow.sent == []
    second = [c for c in h.provider_calls if c["key"] == first.key][1]
    serialized = json.dumps(second, ensure_ascii=False)
    assert serialized.count("RAW_FOLLOWUP_SENTINEL") == 1, serialized
    assert "历史消息上下文" not in serialized and "controlled persona" not in serialized.replace("controlled persona", "", 1)
    complete = [row["outcome"] for row in h.metrics if row["stage"] == "complete"]
    assert sorted(complete) == ["agent_done", "merged"], complete
    assert counts(h, first.key)["official_saves"] == 1 and counts(h, follow.key)["official_saves"] == 0
    await h.cleanup()
    return {"case": "real_core_plain_follow_up", "captured_by_core": follow.get_extra("_follow_up_captured"),
            "raw_consumption_count": serialized.count("RAW_FOLLOWUP_SENTINEL"),
            "outcomes": complete, "provider_inputs": h.provider_calls, "routes": h.routes}


async def payload_case():
    h = Harness(); p = h.plugin("payload")
    rows = []
    for name, parts, images, windows, history in [
        ("plain", [Plain("plain")], [], [], []),
        ("image", [Image.fromURL("https://invalid.test/never-fetch")], ["https://invalid.test/never-fetch"], [], []),
        ("reply", [Reply(id="controlled-reference", chain=[Plain("quoted")])], [], [], []),
        ("window", [Plain("plain")], [], [{"content": "pending window"}], []),
        ("custom_history", [Plain("plain")], [], [], [NS(message_id="custom_storage_1")]),
    ]:
        event = Event("PAYLOAD_" + name, components=parts)
        recipe = native_recipe_block(p, event, images=images, windows=windows, history=history)
        assert bool(recipe) == (name == "plain"), name
        if recipe: event.set_extra("_gcp_native_history_candidate", True)
        assert p._can_delegate_native_queue(event) == (name == "plain"), name
        rows.append({"payload": name, "delegates": bool(recipe)})
    # Run actual legacy branch to establish its original bounded fallback.
    event = Event("LEGACY_RICH")
    p.processing_sessions["inflight"] = event.get_group_id()
    sleeps = []
    async def record_sleep(delay): sleeps.append(delay)
    with patch.object(asyncio, "sleep", record_sleep):
        assert await p._claim_processing_slot(event, event.get_group_id(), event.key)
    assert sleeps == [1.0] * 14 and not event.get_extra("_gcp_native_queue_delegated")
    p.processing_sessions.clear()
    await h.cleanup()
    return {"case": "rich_payload_legacy_fallback", "payloads": rows, "legacy_poll_count": len(sleeps)}


async def run(output):
    output.mkdir(parents=True, exist_ok=True)
    report = {"real_model": False, "real_sdk": False, "production_operations": False,
              "boundaries": ["scripted provider", "local SDK", "isolated HistorySink", "build_main_agent selection"],
              "source_sha256": {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                  for p in [ROOT / "main.py", ROOT / "utils/native_queue.py", ROOT / "utils/request_lifecycle.py",
                            ROOT / "utils/flow_control.py", ROOT / "utils/context_manager.py", Path(__file__)]},
              "core_source_sha256": {inspect.getsourcefile(cls): hashlib.sha256(Path(inspect.getsourcefile(cls)).read_bytes()).hexdigest()
                  for cls in [internal.InternalAgentSubStage, SessionLockManager, ToolLoopAgentRunner,
                              MainAgentHooks, call_handler, call_event_hook, StarHandlerMetadata]},
              "core_source": inspect.getsourcefile(internal.InternalAgentSubStage), "cases": [], "passed": False}
    saved_handlers = list(star_handlers_registry)
    saved_stars = dict(star_map)
    original_lock = internal.session_lock_manager
    async def enabled(event): return True
    async def no_images(*a, **kw): pass
    async def no_metrics(*a, **kw): pass
    try:
        with patch.object(internal, "session_lock_manager", SessionLockManager()), \
             patch.object(internal, "build_main_agent", lambda **kw: CURRENT[0].build(**kw)), \
             patch.object(internal.SessionServiceManager, "should_process_llm_request", enabled), \
             patch.object(internal, "prepare_request_images", no_images), \
             patch.object(internal, "_schedule_internal_agent_stats", lambda *a, **kw: True), \
             patch.object(internal.Metric, "upload", no_metrics):
            # Select the current harness at construction, never intercept locks/hooks.
            original_init = Harness.__init__
            def init(h): original_init(h); CURRENT[:] = [h]
            with patch.object(Harness, "__init__", init):
                for case in [lambda: queue_cases(2), lambda: queue_cases(3), reload_case,
                             cancellation_case, incomplete_case, follow_up_case, payload_case,
                             missing_row_identity_case, one_message_history_window_case,
                             incoming_during_reload_case, cancelled_reload_case]:
                    name = getattr(case, "__name__", "queue_case")
                    print("RUN", name, flush=True)
                    row = await case()
                    report["cases"].append(row)
                    (output / "results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
                    print("PASS", row["case"], flush=True)
            report["passed"] = True
    except BaseException:
        report["failure"] = traceback.format_exc()
        raise
    finally:
        star_handlers_registry.clear()
        for handler in saved_handlers: star_handlers_registry.append(handler)
        star_map.clear(); star_map.update(saved_stars)
        assert internal.session_lock_manager is original_lock
        (output / "results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps({"passed": report["passed"], "cases": len(report["cases"]), "evidence": str(output / "results.json")}))


CURRENT = []
if __name__ == "__main__":
    asyncio.run(run(Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/tmp/controlled/native-probe")))
