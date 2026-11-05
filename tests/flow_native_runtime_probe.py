"""Explicit integration probe in the isolated deployed AstrBot image only."""
import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace

from astrbot.core.pipeline.context_utils import call_handler
from astrbot.core.provider.entities import ProviderRequest
from astrbot.core.pipeline.respond import stage as respond_module
from astrbot.core.message.message_event_result import MessageEventResult, MessageChain
from astrbot.core.message.components import Plain


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("flow_native_probe", ROOT / "utils/flow_control.py")
flow_module = importlib.util.module_from_spec(spec)
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
        async for _ in stage.process(event):
            pass
    finally:
        respond_module.call_event_hook = original
        controller.finish(event, trace)
    assert hook_calls == ["OnAfterMessageSentEvent"]
    assert trace.send_count == 0 and trace.send_failed
    assert "send" not in vars(event)
    print("NATIVE_FLOW_PROBE_OK: real call_handler/iter_with_usage, ProviderRequest lifetime, child task tools/send, failed-send hook distinction")


asyncio.run(main())
