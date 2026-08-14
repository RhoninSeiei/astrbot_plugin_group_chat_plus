import ast
from pathlib import Path
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]


class ImageUrlOwnershipTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tree = ast.parse((REPO_ROOT / "main.py").read_text(encoding="utf-8"))
        chat_plus = next(
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "ChatPlus"
        )
        cls.process_content = next(
            node
            for node in chat_plus.body
            if isinstance(node, ast.AsyncFunctionDef)
            and node.name == "_process_message_content"
        )
        cls.process_message = next(
            node
            for node in chat_plus.body
            if isinstance(node, ast.AsyncFunctionDef)
            and any(
                isinstance(child, ast.Name)
                and child.id == "merged_image_urls"
                for child in ast.walk(node)
            )
        )

    @staticmethod
    def _evaluate(expression, **namespace):
        wrapped = ast.Expression(body=expression)
        ast.fix_missing_locations(wrapped)
        return eval(compile(wrapped, "main.py", "eval"), namespace)

    def test_cached_message_owns_its_image_url_list(self):
        cached_assignment = next(
            node
            for node in ast.walk(self.process_content)
            if isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Dict)
            and any(
                isinstance(key, ast.Constant) and key.value == "image_urls"
                for key in node.value.keys
            )
        )
        image_value = next(
            value
            for key, value in zip(
                cached_assignment.value.keys,
                cached_assignment.value.values,
            )
            if isinstance(key, ast.Constant) and key.value == "image_urls"
        )
        current_images = ["current-a"]

        cached_images = self._evaluate(image_value, image_urls=current_images)
        current_images.append("wait-a")

        self.assertEqual(cached_images, ["current-a"])
        self.assertIsNot(cached_images, current_images)

    def test_formal_request_merge_does_not_mutate_current_message_images(self):
        merged_assignment = next(
            node
            for node in ast.walk(self.process_message)
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "merged_image_urls"
                for target in node.targets
            )
        )
        current_images = ["current-a"]

        merged_images = self._evaluate(
            merged_assignment.value,
            image_urls=current_images,
        )
        merged_images.extend(["wait-a", "smart-a"])

        self.assertEqual(current_images, ["current-a"])
        self.assertEqual(merged_images, ["current-a", "wait-a", "smart-a"])
        self.assertIsNot(merged_images, current_images)


if __name__ == "__main__":
    unittest.main()
