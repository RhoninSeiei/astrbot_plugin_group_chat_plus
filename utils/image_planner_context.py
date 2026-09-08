"""Read the actual image-tool caller without changing provider configuration.

AstrBot's public tool event does not carry the runner's current provider. This
plugin-owned bridge reads it at the tool boundary, after provider fallback. It
only exposes the identity to GCP image handlers for that exact event. Context is
reset before each yield because AstrBot may resume generators in another task.
"""

from contextvars import ContextVar
import importlib
import inspect


_STATE_ATTR = "_gcp_image_planner_context_state"
_TOOLS = frozenset({"gcp_step_image_generate", "gcp_step_image_edit"})


class ImagePlannerContextHandle:
    def __init__(self, runner_cls, state):
        self.runner_cls = runner_cls
        self.state = state
        self.active = True
        state["handles"].add(self)

    def provider_for(self, event):
        if not self.active:
            return None
        current = self.state["current"].get()
        if current is None or current[0] is not event or self not in current[2]:
            return None
        return current[1]

    def close(self):
        if not self.active:
            return
        self.active = False
        self.state["handles"].discard(self)
        if self.state["handles"]:
            return
        # Preserve a wrapper another plugin installed after this one. Our
        # remaining wrapper is inert and can be reused on the next initialize.
        if self.runner_cls.__dict__.get("_handle_function_tools") is self.state["wrapper"]:
            setattr(self.runner_cls, "_handle_function_tools", self.state["original"])
            if self.runner_cls.__dict__.get(_STATE_ATTR) is self.state:
                delattr(self.runner_cls, _STATE_ATTR)


def install_image_planner_context(runner_cls=None):
    if runner_cls is None:
        module = importlib.import_module("astrbot.core.agent.runners.tool_loop_agent_runner")
        runner_cls = module.ToolLoopAgentRunner
    state = runner_cls.__dict__.get(_STATE_ATTR)
    if state is None:
        original = runner_cls.__dict__.get("_handle_function_tools")
        if not inspect.isasyncgenfunction(original):
            raise TypeError("AstrBot tool runner does not expose the supported async generator")
        state = {
            "original": original,
            "handles": set(),
            "current": ContextVar("gcp_image_planner", default=None),
        }

        async def with_image_planner(runner, req, llm_response):
            names = getattr(llm_response, "tools_call_name", ()) or ()
            binding = None
            if state["handles"] and _TOOLS.intersection(names):
                context = getattr(getattr(runner, "run_context", None), "context", None)
                event = getattr(context, "event", None)
                provider = getattr(runner, "provider", None)
                if event is not None and provider is not None:
                    binding = (event, provider, frozenset(state["handles"]))
            stream = state["original"](runner, req, llm_response)
            try:
                while True:
                    token = state["current"].set(binding if state["handles"] else None)
                    try:
                        item = await anext(stream)
                    except StopAsyncIteration:
                        return
                    finally:
                        state["current"].reset(token)
                    yield item
            finally:
                token = state["current"].set(binding if state["handles"] else None)
                try:
                    await stream.aclose()
                finally:
                    state["current"].reset(token)

        state["wrapper"] = with_image_planner
        setattr(runner_cls, _STATE_ATTR, state)
        setattr(runner_cls, "_handle_function_tools", with_image_planner)
    return ImagePlannerContextHandle(runner_cls, state)
