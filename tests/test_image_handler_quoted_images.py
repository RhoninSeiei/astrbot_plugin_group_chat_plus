import asyncio
import importlib.util
import pathlib
import sys
import types
import unittest


REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
UTILS_DIR = REPO_ROOT / "utils"

quoted_results = {}
quoted_calls = []


class FakeLogger:
    def info(self, *_args, **_kwargs):
        pass

    def warning(self, *_args, **_kwargs):
        pass

    def error(self, *_args, **_kwargs):
        pass


class FakeImage:
    def __init__(self, value=None, error=None):
        self.value = value
        self.error = error

    async def convert_to_file_path(self):
        if self.error:
            raise self.error
        return self.value


class FakeReply:
    def __init__(self, reply_id="r1", chain=None, message_str="[quoted message]"):
        self.id = reply_id
        self.chain = list(chain or [])
        self.message_str = message_str


class FakePlain:
    def __init__(self, text=""):
        self.text = text


class FakeEvent:
    def __init__(self, chain):
        self.message_obj = types.SimpleNamespace(message=list(chain))


async def extract_quoted_message_images(event, reply_component):
    quoted_calls.append(reply_component.id)
    value = quoted_results.get(reply_component.id, [])
    if isinstance(value, Exception):
        raise value
    return list(value)


def _load_image_handler_module():
    package_name = "group_chat_plus_image_handler_test"
    package_module = types.ModuleType(package_name)
    package_module.__path__ = [str(UTILS_DIR)]
    sys.modules[package_name] = package_module

    logger = FakeLogger()
    astrbot_module = types.ModuleType("astrbot")
    astrbot_api_module = types.ModuleType("astrbot.api")
    astrbot_api_all_module = types.ModuleType("astrbot.api.all")
    components_module = types.ModuleType("astrbot.api.message_components")
    core_module = types.ModuleType("astrbot.core")
    core_utils_module = types.ModuleType("astrbot.core.utils")
    parser_module = types.ModuleType("astrbot.core.utils.quoted_message_parser")

    astrbot_api_module.logger = logger
    astrbot_api_all_module.logger = logger
    astrbot_api_all_module.Image = FakeImage
    astrbot_api_all_module.Plain = FakePlain
    astrbot_api_all_module.AstrMessageEvent = type("AstrMessageEvent", (), {})
    astrbot_api_all_module.Context = type("Context", (), {})
    astrbot_api_all_module.BaseMessageComponent = type(
        "BaseMessageComponent", (), {}
    )
    components_module.Face = type("Face", (), {})
    components_module.At = type("At", (), {})
    components_module.Reply = FakeReply
    parser_module.extract_quoted_message_images = extract_quoted_message_images

    sys.modules["astrbot"] = astrbot_module
    sys.modules["astrbot.api"] = astrbot_api_module
    sys.modules["astrbot.api.all"] = astrbot_api_all_module
    sys.modules["astrbot.api.message_components"] = components_module
    sys.modules["astrbot.core"] = core_module
    sys.modules["astrbot.core.utils"] = core_utils_module
    sys.modules["astrbot.core.utils.quoted_message_parser"] = parser_module

    cache_module = types.ModuleType(f"{package_name}.image_description_cache")
    cache_module.ImageDescriptionCache = type("ImageDescriptionCache", (), {})
    sys.modules[cache_module.__name__] = cache_module

    spec = importlib.util.spec_from_file_location(
        f"{package_name}.image_handler",
        UTILS_DIR / "image_handler.py",
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


image_handler_module = _load_image_handler_module()
ImageHandler = image_handler_module.ImageHandler
ResolvedMessageImage = image_handler_module.ResolvedMessageImage


class ImageHandlerQuotedImagesTest(unittest.TestCase):
    def setUp(self):
        quoted_results.clear()
        quoted_calls.clear()

    def test_embedded_reply_image_avoids_remote_fetch(self):
        chain = [FakeReply("r1", [FakeImage("quoted-a")])]
        result = asyncio.run(
            ImageHandler.collect_message_images(FakeEvent(chain), chain, 10)
        )
        self.assertEqual([item.url for item in result], ["quoted-a"])
        self.assertEqual([item.source for item in result], ["quoted_embedded"])
        self.assertEqual(quoted_calls, [])

    def test_empty_reply_chain_uses_public_parser(self):
        quoted_results["r1"] = ["quoted-a"]
        chain = [FakeReply("r1")]
        result = asyncio.run(
            ImageHandler.collect_message_images(FakeEvent(chain), chain, 10)
        )
        self.assertEqual([item.url for item in result], ["quoted-a"])
        self.assertEqual([item.source for item in result], ["quoted_fetched"])
        self.assertEqual(quoted_calls, ["r1"])

    def test_partial_embedded_failure_prefers_complete_parser_order(self):
        quoted_results["r1"] = ["quoted-a", "quoted-b"]
        chain = [
            FakeReply(
                "r1",
                [FakeImage("quoted-a"), FakeImage(error=ValueError("expired"))],
            )
        ]
        result = asyncio.run(
            ImageHandler.collect_message_images(FakeEvent(chain), chain, 10)
        )
        self.assertEqual([item.url for item in result], ["quoted-a", "quoted-b"])
        self.assertEqual([item.source for item in result], ["quoted_fetched"] * 2)

    def test_top_level_image_is_resolved(self):
        chain = [FakeImage("top-a")]
        result = asyncio.run(
            ImageHandler.collect_message_images(FakeEvent(chain), chain, 10)
        )
        self.assertEqual(result, [ResolvedMessageImage("top-a", "top_level", 0)])

    def test_mixed_top_level_and_replies_preserve_component_order(self):
        chain = [
            FakeImage("top-a"),
            FakeReply("r1", [FakeImage("quoted-a")]),
            FakeImage("top-b"),
            FakeReply("r2", [FakeImage("quoted-b")]),
        ]
        result = asyncio.run(
            ImageHandler.collect_message_images(FakeEvent(chain), chain, 10)
        )
        self.assertEqual(
            [(item.url, item.component_index) for item in result],
            [("top-a", 0), ("quoted-a", 1), ("top-b", 2), ("quoted-b", 3)],
        )

    def test_duplicate_urls_are_removed_in_first_seen_order(self):
        quoted_results["r1"] = ["top-a", "quoted-a", "quoted-a"]
        chain = [FakeImage("top-a"), FakeReply("r1"), FakeImage("quoted-a")]
        result = asyncio.run(
            ImageHandler.collect_message_images(FakeEvent(chain), chain, 10)
        )
        self.assertEqual([item.url for item in result], ["top-a", "quoted-a"])
        self.assertEqual([item.component_index for item in result], [0, 1])

    def test_limit_is_shared_across_top_level_and_reply_images(self):
        chain = [
            FakeImage("top-a"),
            FakeReply("r1", [FakeImage("quoted-a"), FakeImage("quoted-b")]),
            FakeImage("top-b"),
        ]
        result = asyncio.run(
            ImageHandler.collect_message_images(FakeEvent(chain), chain, 3)
        )
        self.assertEqual(
            [(item.url, item.component_index) for item in result],
            [("top-a", 0), ("quoted-a", 1), ("quoted-b", 1)],
        )

    def test_parser_failure_retains_successful_embedded_images(self):
        quoted_results["r1"] = RuntimeError("unavailable")
        chain = [
            FakeReply(
                "r1",
                [FakeImage("quoted-a"), FakeImage(error=ValueError("expired"))],
            )
        ]
        result = asyncio.run(
            ImageHandler.collect_message_images(FakeEvent(chain), chain, 10)
        )
        self.assertEqual(
            result,
            [ResolvedMessageImage("quoted-a", "quoted_embedded", 0)],
        )
        self.assertEqual(quoted_calls, ["r1"])


if __name__ == "__main__":
    unittest.main()
