import ast
import asyncio
import copy
from pathlib import Path
from types import SimpleNamespace
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]


class RecordingLogger:
    level = 10

    def __init__(self):
        self.records = []

    def info(self, message, *args, **kwargs):
        self.records.append(("info", message, args, kwargs))

    def warning(self, message, *args, **kwargs):
        self.records.append(("warning", message, args, kwargs))

    def error(self, message, *args, **kwargs):
        self.records.append(("error", message, args, kwargs))


def _load_generate_reply(logger):
    tree = ast.parse(
        (REPO_ROOT / "utils" / "reply_handler.py").read_text(encoding="utf-8")
    )
    reply_handler = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "ReplyHandler"
    )
    method = next(
        copy.deepcopy(node)
        for node in reply_handler.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "generate_reply"
    )
    method.decorator_list = []
    module = ast.Module(body=[method], type_ignores=[])
    ast.fix_missing_locations(module)

    class FakeReplyHandler:
        SYSTEM_REPLY_PROMPT = "system prompt"
        SYSTEM_REPLY_PROMPT_ENDING = "system ending"

    async def resolve_session_persona(_context, event):
        return {"prompt": "persona", "_begin_dialogs_processed": []}

    namespace = {
        "AstrMessageEvent": object,
        "Context": object,
        "ProviderRequest": object,
        "ReplyHandler": FakeReplyHandler,
        "ToolPolicy": SimpleNamespace(clone_tool_container=lambda value: value),
        "resolve_session_persona": resolve_session_persona,
        "_expand_event_plugins_name_for_tool_access": lambda *_args: None,
        "format_ai_error": lambda exc, _stage: type(exc).__name__,
        "PLUGIN_MAIN_MODEL_FINAL_GATE_DECLINED": "gate_declined",
        "PLUGIN_REQUEST_MARKER": "request_marker",
        "PLUGIN_CUSTOM_CONTEXTS": "contexts",
        "PLUGIN_CUSTOM_SYSTEM_PROMPT": "system_prompt",
        "PLUGIN_CUSTOM_PROMPT": "prompt",
        "PLUGIN_IMAGE_URLS": "image_urls",
        "PLUGIN_FUNC_TOOL": "func_tool",
        "PLUGIN_FALLBACK_PAYLOAD": "fallback_payload",
        "PLUGIN_CURRENT_MESSAGE": "current_message",
        "PLUGIN_DIRECT_REPLY_MODE": "direct_reply",
        "DEBUG_MODE": True,
        "logger": logger,
    }
    exec(compile(module, "reply_handler.py", "exec"), namespace)
    FakeReplyHandler.generate_reply = staticmethod(namespace["generate_reply"])
    return FakeReplyHandler


class FakeEvent:
    def __init__(self):
        self.extras = {}
        self.session_id = "session-1"
        self.request = None

    def set_extra(self, key, value):
        self.extras[key] = value

    def get_sender_id(self):
        return "sender-1"

    def get_sender_name(self):
        return "sender"

    def get_message_str(self):
        return "引用正文"

    def request_llm(self, **kwargs):
        self.request = kwargs
        return kwargs

    def plain_result(self, value):
        return value


class FormalRequestLogPrivacyTest(unittest.TestCase):
    def test_formal_request_passes_images_without_logging_addresses(self):
        logger = RecordingLogger()
        reply_handler = _load_generate_reply(logger)
        secret_url = "https://private.example/image.png?token=SECRET-TOKEN"
        secret_base64 = "data:image/png;base64,SECRETBASE64"
        event = FakeEvent()
        context = SimpleNamespace(get_llm_tool_manager=lambda: SimpleNamespace(tools=[]))

        result = asyncio.run(
            reply_handler.generate_reply(
                event,
                context,
                "引用正文",
                "",
                image_urls=[secret_url, secret_base64],
                include_sender_info=False,
            )
        )

        self.assertEqual(result["image_urls"], [secret_url, secret_base64])
        self.assertEqual(event.extras["image_urls"], [secret_url, secret_base64])
        rendered = repr(logger.records)
        self.assertNotIn("private.example", rendered)
        self.assertNotIn("SECRET-TOKEN", rendered)
        self.assertNotIn("SECRETBASE64", rendered)


if __name__ == "__main__":
    unittest.main()
