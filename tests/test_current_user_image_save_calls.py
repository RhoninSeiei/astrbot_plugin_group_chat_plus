import ast
import pathlib
import unittest


REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
TARGET_METHODS = (
    "after_message_sent",
    "_save_user_messages_on_duplicate_block",
    "_finalize_bot_reply_save",
)


def _is_official_save_call(node):
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "save_to_official_conversation_with_cache"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "ContextManager"
    )


class CurrentUserImageSaveCallsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tree = ast.parse((REPO_ROOT / "main.py").read_text(encoding="utf-8"))
        cls.methods = {
            node.name: node
            for node in ast.walk(cls.tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name in TARGET_METHODS
        }

    def test_phase_one_calls_pass_reference_image_snapshot(self):
        self.assertEqual(set(self.methods), set(TARGET_METHODS))

        for method_name, method in self.methods.items():
            calls = [node for node in ast.walk(method) if _is_official_save_call(node)]
            phase_one_calls = [
                call
                for call in calls
                if len(call.args) > 2
                and not (
                    isinstance(call.args[2], ast.Constant)
                    and call.args[2].value is None
                )
            ]
            self.assertEqual(len(phase_one_calls), 1, method_name)

            keyword = next(
                (
                    item
                    for item in phase_one_calls[0].keywords
                    if item.arg == "user_image_urls"
                ),
                None,
            )
            self.assertIsNotNone(keyword, method_name)
            self.assertIsInstance(keyword.value, ast.Name, method_name)
            self.assertEqual(keyword.value.id, "user_image_urls", method_name)

            reference_gets = [
                node
                for node in ast.walk(method)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "last_cached"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and node.args[0].value == "reference_image_urls"
            ]
            self.assertTrue(reference_gets, method_name)

    def test_phase_two_calls_do_not_reuse_current_user_images(self):
        for method_name, method in self.methods.items():
            calls = [node for node in ast.walk(method) if _is_official_save_call(node)]
            phase_two_calls = [
                call
                for call in calls
                if len(call.args) > 2
                and isinstance(call.args[2], ast.Constant)
                and call.args[2].value is None
            ]
            self.assertEqual(len(phase_two_calls), 1, method_name)

            for call in phase_two_calls:
                keyword = next(
                    (
                        item
                        for item in call.keywords
                        if item.arg == "user_image_urls"
                    ),
                    None,
                )
                if keyword is not None:
                    self.assertIsInstance(keyword.value, (ast.Constant, ast.List))
                    if isinstance(keyword.value, ast.Constant):
                        self.assertIsNone(keyword.value.value)
                    else:
                        self.assertEqual(keyword.value.elts, [])


if __name__ == "__main__":
    unittest.main()
