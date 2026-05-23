from pathlib import Path
import re
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]


class ReplyLeakageGuardTest(unittest.TestCase):
    def setUp(self):
        self.reply_source = (REPO_ROOT / "utils" / "reply_handler.py").read_text(
            encoding="utf-8"
        )
        self.proactive_source = (
            REPO_ROOT / "utils" / "proactive_chat_manager.py"
        ).read_text(encoding="utf-8")
        self.private_reply_source = (
            REPO_ROOT
            / "private_chat"
            / "private_chat_utils"
            / "private_chat_reply_handler.py"
        ).read_text(encoding="utf-8")
        self.prompt_data_source = (
            REPO_ROOT / "web" / "static" / "js" / "prompt-data.js"
        ).read_text(encoding="utf-8")

    def test_reply_prompt_is_action_oriented(self):
        self.assertIn("你的任务：直接生成回复内容", self.reply_source)
        self.assertIn("你只负责生成回复文本，不负责判断", self.reply_source)
        self.assertIn("不要输出“是否该回复”", self.reply_source)

    def test_final_gate_is_separate_from_reply_generation_prompt(self):
        self.assertIn("_run_final_decision_gate", self.reply_source)
        self.assertIn("_build_final_decision_gate_prompt", self.reply_source)
        self.assertNotIn("full_prompt += final_decision_gate_prompt", self.reply_source)
        self.assertNotIn("+ final_decision_gate_prompt", self.reply_source)

    def test_final_gate_uses_private_tokens_and_legacy_no_reply_is_compat_only(self):
        self.assertIn('MAIN_MODEL_FINAL_GATE_REPLY = "[[GCP_FINAL_REPLY]]"', self.reply_source)
        self.assertIn(
            'MAIN_MODEL_FINAL_GATE_NO_REPLY = "[[GCP_FINAL_SILENCE]]"',
            self.reply_source,
        )
        self.assertIn(
            'LEGACY_MAIN_MODEL_FINAL_GATE_NO_REPLY = "[[NO_REPLY]]"',
            self.reply_source,
        )

        gate_prompt = re.search(
            r"MAIN_MODEL_FINAL_GATE_PROMPT = f\"\"\"(?P<body>.*?)\"\"\"",
            self.reply_source,
            re.S,
        )
        self.assertIsNotNone(gate_prompt)
        self.assertNotIn("[[NO_REPLY]]", gate_prompt.group("body"))

    def test_proactive_generation_prompt_is_action_oriented(self):
        self.assertIn("你的任务：直接生成你要说的话", self.proactive_source)
        self.assertIn("系统已经完成了", self.proactive_source)
        self.assertIn("不要输出判断腔", self.proactive_source)

    def test_private_reply_prompt_is_action_oriented(self):
        self.assertIn("你的任务：直接生成回复内容", self.private_reply_source)
        self.assertIn("你只负责生成回复文本，不负责判断", self.private_reply_source)
        self.assertIn("不要输出“是否该回复”", self.private_reply_source)

    def test_web_prompt_preview_matches_action_oriented_prompts(self):
        self.assertIn("你的任务：直接生成回复内容", self.prompt_data_source)
        self.assertIn("你的任务：直接生成你要说的话", self.prompt_data_source)
        self.assertIn("不要输出判断腔", self.prompt_data_source)


if __name__ == "__main__":
    unittest.main()
