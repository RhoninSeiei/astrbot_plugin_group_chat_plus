import importlib.util
import pathlib
import unittest


REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _load_ai_error_formatter():
    module_path = REPO_ROOT / "utils" / "ai_error_formatter.py"
    spec = importlib.util.spec_from_file_location("ai_error_formatter_test", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class AIErrorHandlingTest(unittest.TestCase):
    def test_formats_http_provider_errors(self):
        formatter = _load_ai_error_formatter()

        message = formatter.format_ai_error(
            RuntimeError("HTTP 502: Bad Gateway"),
            context_label="读空气判断",
        )

        self.assertIn("[读空气判断]", message)
        self.assertIn("HTTP 502", message)
        self.assertIn("AI 服务商", message)

    def test_formats_html_gateway_errors_without_dumping_page(self):
        formatter = _load_ai_error_formatter()
        html = "<!DOCTYPE html><html><head><title>502 Bad Gateway</title></head>"
        html += "<body>" + ("gateway failure " * 80) + "</body></html>"

        message = formatter.format_ai_error(
            RuntimeError(html),
            context_label="主动对话生成",
        )

        self.assertIn("[主动对话生成]", message)
        self.assertIn("HTML", message)
        self.assertLess(len(message), 260)

    def test_formats_upstream_empty_output(self):
        formatter = _load_ai_error_formatter()

        message = formatter.format_ai_error(
            RuntimeError("upstream_empty_output: model returned no usable output"),
            context_label="最终回复判断",
        )

        self.assertIn("上游模型返回空输出", message)
        self.assertIn("最终回复判断", message)

    def test_formats_network_errors(self):
        formatter = _load_ai_error_formatter()

        message = formatter.format_ai_error(
            TimeoutError("connection timeout while reading socket"),
            context_label="图片转文字",
        )

        self.assertIn("网络问题", message)
        self.assertIn("图片转文字", message)

    def test_proactive_judge_failure_does_not_touch_last_reply_time(self):
        source = (REPO_ROOT / "utils" / "proactive_chat_manager.py").read_text(
            encoding="utf-8"
        )
        start = source.index("# ========== 步骤4.5")
        end = source.index("# 注入情绪状态", start)
        judge_block = source[start:end]

        for marker in (
            "except asyncio.TimeoutError:",
            "except Exception as e:",
        ):
            marker_index = judge_block.index(marker)
            next_return = judge_block.index("return", marker_index)
            error_block = judge_block[marker_index:next_return]
            self.assertNotIn('state["last_bot_reply_time"] = time.time()', error_block)


if __name__ == "__main__":
    unittest.main()
