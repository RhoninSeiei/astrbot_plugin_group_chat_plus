"""Explicit integration probe in the isolated deployed AstrBot image only."""
import asyncio
import ast
import copy
import importlib.util
import sys
import types
from pathlib import Path
from types import SimpleNamespace

from astrbot.core.pipeline.context_utils import call_handler
from astrbot.core.provider.entities import ProviderRequest
from astrbot.core.pipeline.respond import stage as respond_module
from astrbot.core.message.message_event_result import MessageEventResult, MessageChain
from astrbot.core.message.components import Plain
from astrbot.core.agent.message import TextPart


ROOT = Path(__file__).resolve().parents[1]
package = types.ModuleType("gcp_flow_native_helpers")
package.__path__ = [str(ROOT / "utils")]
sys.modules[package.__name__] = package
spec = importlib.util.spec_from_file_location("gcp_flow_native_helpers.flow_control", ROOT / "utils/flow_control.py")
flow_module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = flow_module
spec.loader.exec_module(flow_module)


class Event:
    unified_msg_origin = "default:GroupMessage:905956314"
    session_id = "synthetic-session"
    message_obj = SimpleNamespace(message_id="synthetic-message")

    def __init__(self):
        self.extras = {}
        self.result = None
        self.submissions = []
        self.fail = False

    def get_extra(self, key, default=None):
        return self.extras.get(key, default)

    def set_extra(self, key, value):
        self.extras[key] = value

    def is_private_chat(self):
        return False

    def get_sender_name(self):
        return "synthetic"

    def get_sender_id(self):
        return "synthetic"

    def get_message_str(self):
        return "current telescope question"

    def _outline_chain(self, chain):
        return "synthetic chain"

    def get_result(self):
        return self.result

    def set_result(self, value):
        self.result = value

    def clear_result(self):
        self.result = None

    async def send(self, chain, **kwargs):
        if self.fail:
            raise OSError("synthetic SDK rejection")
        self.submissions.append(chain)

    async def send_streaming(self, stream, realtime=False):
        async for chain in stream:
            await self.send(chain)


def publish_native_memory_fixture(root, controller, event, req):
    """Use actual LM publisher body and formatter; no recall/network/providers."""
    root = Path(root)
    for name, directory in [
        ("lm_contract", root), ("lm_contract.core", root / "core"),
        ("lm_contract.core.base", root / "core/base"),
        ("lm_contract.core.utils", root / "core/utils"),
    ]:
        package = types.ModuleType(name)
        package.__path__ = [str(directory)]
        sys.modules[name] = package
    specification = importlib.util.spec_from_file_location("lm_contract.core.utils.formatting", root / "core/utils/formatting.py")
    formatting = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = formatting
    specification.loader.exec_module(formatting)
    tree = ast.parse((root / "core/event_handler_modules/memory_recall.py").read_text())
    publisher = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "_publish_request_handoff")
    publisher.decorator_list = []
    namespace = {"copy": copy, "format_memories_for_injection": formatting.format_memories_for_injection}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[publisher], type_ignores=[])), "memory_recall.py", "exec"), namespace)
    memories = [{"content": word * 120, "importance": .5, "metadata": {"topics": [word.strip()]}}
                for word in ["camera ", "telescope ", "cooking "]]
    original_text = formatting.format_memories_for_injection(memories)
    original = TextPart(text=original_text)
    original.mark_as_temp()
    other = TextPart(text="another plugin's user context")
    req.extra_user_content_parts = [other, original]
    req.system_prompt = "synthetic unchanged persona"
    req.contexts = [{"role": "tool", "content": "synthetic unchanged tool protocol"}]
    controller.request_memory_handoff(event, req)
    assert namespace[publisher.name](event, req, original, memories)
    controller.prepare_request(event, req, "synthetic-persona")
    assert req.extra_user_content_parts[0] is other
    assert req.extra_user_content_parts[1] is not original
    assert req.extra_user_content_parts[1]._no_save and original.text == original_text
    assert len(req.extra_user_content_parts[1].text) < len(original_text)
    assert "telescope" in req.extra_user_content_parts[1].text
    assert req.system_prompt == "synthetic unchanged persona"
    assert req.contexts == [{"role": "tool", "content": "synthetic unchanged tool protocol"}]
    assert event.get_extra(flow_module.HANDOFF_REQUESTED) is None
    assert event.get_extra(flow_module.HANDOFF_RESULT) is None
    print("NATIVE_MEMORY_HANDOFF_OK: actual LM publisher/formatter, native temporary part, structured selection, identity and cleanup")


async def main():
    event = Event()
    rows = []
    controller = flow_module.FlowController({"flow_optimization_sessions": [event.unified_msg_origin]}, rows.append)
    finalized = []

    async def handler(current):
        trace = controller.start(current)
        controller.formal_request(current)
        try:
            yield ProviderRequest(prompt="synthetic")
            assert controller.trace(current) is trace
            finalized.append(trace)
        finally:
            controller.finish(current, trace)

    async for request in call_handler(event, handler, _usage_plugin_id="astrbot_plugin_group_chat_plus"):
        if not isinstance(request, ProviderRequest):
            continue
        assert controller.trace(event) is not None, "usage scope reset another ContextVar"
        if len(sys.argv) > 1:
            publish_native_memory_fixture(sys.argv[1], controller, event, request)
        tool = object()
        # A core streaming feeder may consume the agent in a child task.
        async def consume():
            controller.tool_start(event, tool)
            controller.tool_end(event, tool, SimpleNamespace(isError=False))
            controller.agent_done(event)
            await event.send(MessageChain([Plain("synthetic final")]))
        await asyncio.create_task(consume())
        assert any(row["stage"] == "final_sdk_submit" for row in rows)
        assert not finalized, "handler finalized before the downstream send"
    assert len(finalized) == 1 and finalized[0].finished
    assert controller.active == set() and "send" not in vars(event)

    # The real RespondStage invokes after_message_sent even when its send raises.
    # The observation must distinguish that hook from successful SDK submission.
    trace = controller.start(event)
    controller.formal_request(event)
    event.fail = True
    event.set_result(MessageEventResult(chain=[Plain("synthetic failure")]))
    stage = respond_module.RespondStage()
    stage.platform_settings = {}
    stage.enable_seg = False
    stage.only_llm_result = True
    hook_calls = []
    async def hook(current, event_type, *args):
        hook_calls.append(event_type.name)
        controller.send_hook(current)
    original = respond_module.call_event_hook
    respond_module.call_event_hook = hook
    try:
        await stage.process(event)
    finally:
        respond_module.call_event_hook = original
        controller.finish(event, trace)
    assert hook_calls == ["OnAfterMessageSentEvent"]
    assert trace.send_count == 0 and trace.send_failed
    assert "send" not in vars(event)
    print("NATIVE_FLOW_PROBE_OK: real call_handler/iter_with_usage, ProviderRequest lifetime, child task tools/send, failed-send hook distinction")


asyncio.run(main())
