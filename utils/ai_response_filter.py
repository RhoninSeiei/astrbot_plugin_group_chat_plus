"""
AI响应过滤器 - 处理带思考链的AI返回
过滤掉AI输出中的思考过程标记，避免影响决策判断

作者: Him666233
版本: v1.2.1
"""

import re
from typing import Optional, Dict, Any, Tuple
from astrbot.api import logger

# 详细日志开关
DEBUG_MODE: bool = False


class AIResponseFilter:
    """
    AI响应过滤器

    主要功能：
    1. 移除常见的思考链标记（XML格式）
    2. 移除中文思考过程前缀
    3. 提取纯净的AI回复内容

    支持的思考链格式：
    - <thinking>...</thinking>
    - <think>...</think>
    - <thought>...</thought>
    - <reasoning>...</reasoning>
    - 中文前缀：思考：、分析：、判断：等
    """

    # XML风格的思考标签正则列表
    THINKING_TAG_PATTERNS = [
        r"<thinking>.*?</thinking>",
        r"<think>.*?</think>",
        r"<thought>.*?</thought>",
        r"<reasoning>.*?</reasoning>",
        r"<analysis>.*?</analysis>",
        r"<考虑>.*?</考虑>",
        r"<思考>.*?</思考>",
        r"<分析>.*?</分析>",
    ]

    # 中文思考过程前缀模式
    CHINESE_THINKING_PREFIXES = [
        r"^思考[：:]\s*",
        r"^分析[：:]\s*",
        r"^判断[：:]\s*",
        r"^推理[：:]\s*",
        r"^考虑[：:]\s*",
        r"^评估[：:]\s*",
        r"^我的想法[：:]\s*",
        r"^让我想想[：:]\s*",
    ]

    ANSWER_PREFIXES = [
        r"^回答[：:]\s*",
        r"^答[：:]\s*",
        r"^结论[：:]\s*",
        r"^结果[：:]\s*",
    ]

    @staticmethod
    def filter_thinking_chain(response: str) -> str:
        """
        过滤AI响应中的思考链标记

        Args:
            response: 原始AI响应

        Returns:
            过滤后的响应文本
        """
        if not response or not isinstance(response, str):
            return response

        original_response = response

        # 第一步：移除XML风格的思考标签
        for pattern in AIResponseFilter.THINKING_TAG_PATTERNS:
            # 使用 DOTALL 标志，让 . 匹配包括换行符在内的所有字符
            response = re.sub(pattern, "", response, flags=re.DOTALL | re.IGNORECASE)

        # 第二步：移除中文思考过程前缀及其后的内容（更智能的处理）
        lines = response.split("\n")
        filtered_lines = []

        # 定义简单答案的集合（用于判断是否应该保留）
        # 包含决策判断和频率判断的所有可能答案
        simple_answers = {
            # 决策判断
            "yes",
            "y",
            "no",
            "n",
            "是",
            "否",
            "应该",
            "不应该",
            "回复",
            "不回复",
            # 频率判断
            "正常",
            "过于频繁",
            "过少",
            "太少",
            "太频繁",
            "频繁",
            "少",
            "合适",
            "适当",
        }

        for line in lines:
            line_stripped = line.strip()

            if not line_stripped:
                continue

            # 检查是否是思考前缀开头的行
            found_thinking_prefix = False
            extracted_answer = None

            for prefix_pattern in AIResponseFilter.CHINESE_THINKING_PREFIXES:
                match = re.match(prefix_pattern, line_stripped, flags=re.IGNORECASE)
                if match:
                    found_thinking_prefix = True
                    # 提取前缀后的内容
                    remaining = line_stripped[match.end() :].strip()
                    # 如果后面是简单答案，保留答案
                    if remaining.lower() in simple_answers:
                        extracted_answer = remaining
                    # 否则整行跳过（这是思考过程的描述）
                    break

            # 如果找到思考前缀
            if found_thinking_prefix:
                # 只保留提取到的简单答案（如果有）
                if extracted_answer:
                    filtered_lines.append(extracted_answer)
                # 否则跳过整行
            else:
                # 不是思考前缀行，保留
                filtered_lines.append(line)

        response = "\n".join(filtered_lines)

        # 第三步：清理多余的空白
        response = response.strip()

        # 第四步：移除可能存在的"回答："、"答："等前缀
        for prefix_pattern in AIResponseFilter.ANSWER_PREFIXES:
            response = re.sub(prefix_pattern, "", response, flags=re.IGNORECASE)

        response = response.strip()

        # 记录日志（如果内容发生了变化）
        if response != original_response and DEBUG_MODE:
            logger.info(f"[AI响应过滤] 检测到思考链内容并已过滤")
            logger.info(f"  原始响应前100字符: {original_response[:100]}...")
            logger.info(f"  过滤后响应: {response}")

        return response

    @staticmethod
    def _strip_answer_prefixes(response: str) -> str:
        cleaned = (response or "").strip()
        for prefix_pattern in AIResponseFilter.ANSWER_PREFIXES:
            cleaned = re.sub(prefix_pattern, "", cleaned, flags=re.IGNORECASE)
        return cleaned.strip()

    @staticmethod
    def _extract_custom_reasoning_block(
        response: str, start_marker: str = "", end_marker: str = ""
    ) -> Tuple[str, str]:
        if not response:
            return response, ""

        start_marker = (start_marker or "").strip()
        end_marker = (end_marker or "").strip()
        if not start_marker or not end_marker:
            return response, ""

        pattern = re.compile(
            re.escape(start_marker) + r"([\s\S]*?)" + re.escape(end_marker),
            re.IGNORECASE,
        )
        reasoning_blocks = [
            match.group(1).strip()
            for match in pattern.finditer(response)
            if match.group(1).strip()
        ]
        filtered = pattern.sub("", response).strip()
        reasoning_text = "\n\n".join(reasoning_blocks).strip()
        filtered = re.sub(r"\n{3,}", "\n\n", filtered).strip()
        return AIResponseFilter._strip_answer_prefixes(filtered), reasoning_text

    @staticmethod
    def _extract_last_non_empty_line(text: str) -> str:
        if not text:
            return ""
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        return lines[-1] if lines else ""

    @staticmethod
    def _normalize_tail_token(token: str) -> str:
        cleaned = AIResponseFilter._strip_answer_prefixes(token or "")
        cleaned = cleaned.strip().rstrip(".,!?。,!？；;:：")
        return cleaned.strip()

    @staticmethod
    def _decision_exact_map() -> Dict[str, str]:
        return {
            "yes": "yes",
            "y": "y",
            "no": "no",
            "n": "n",
            "是": "是",
            "否": "否",
            "应该": "应该",
            "不应该": "不应该",
            "回复": "回复",
            "不回复": "不回复",
            "适合": "适合",
            "合适": "适合",
            "不适合": "不适合",
            "不合适": "不适合",
            "跳过": "不适合",
            "跳过这次": "不适合",
            "跳过本次": "不适合",
        }

    @staticmethod
    def parse_decision_response(
        response: str, start_marker: str = "", end_marker: str = ""
    ) -> Dict[str, Any]:
        if not response:
            return {
                "filtered_text": "",
                "reasoning_text": "",
                "normalized_answer": None,
                "parse_success": False,
                "tail_line": "",
                "tail_candidate": "",
                "protocol_followed": False,
            }

        filtered = AIResponseFilter.filter_thinking_chain(response)
        final_text, reasoning_text = AIResponseFilter._extract_custom_reasoning_block(
            filtered, start_marker, end_marker
        )
        tail_line = AIResponseFilter._extract_last_non_empty_line(final_text)
        tail_cleaned = AIResponseFilter._normalize_tail_token(tail_line).lower()
        exact_map = AIResponseFilter._decision_exact_map()
        if tail_cleaned in exact_map:
            return {
                "filtered_text": final_text,
                "reasoning_text": reasoning_text,
                "normalized_answer": exact_map[tail_cleaned],
                "parse_success": True,
                "tail_line": tail_line,
                "tail_candidate": tail_cleaned,
                "protocol_followed": True,
            }

        cleaned = AIResponseFilter._strip_answer_prefixes(
            final_text.strip().lower().rstrip(".,!?。,!？；;:：")
        )
        negative_patterns = (
            r"不适合",
            r"不合适",
            r"不建议",
            r"跳过(?:这次|本次)?",
            r"不应该",
            r"不回复",
            r"\b(no|n)\b",
            r"否",
        )
        for pattern in negative_patterns:
            if re.search(pattern, cleaned, re.IGNORECASE):
                return {
                    "filtered_text": final_text,
                    "reasoning_text": reasoning_text,
                    "normalized_answer": "no",
                    "parse_success": True,
                    "tail_line": tail_line,
                    "tail_candidate": tail_cleaned,
                    "protocol_followed": False,
                }

        positive_patterns = (
            r"适合",
            r"合适",
            r"可以主动",
            r"可以发起",
            r"应该",
            r"回复",
            r"\b(yes|y)\b",
            r"(^|[\s，。])是($|[\s，。])",
        )
        for pattern in positive_patterns:
            if re.search(pattern, cleaned, re.IGNORECASE):
                return {
                    "filtered_text": final_text,
                    "reasoning_text": reasoning_text,
                    "normalized_answer": "yes",
                    "parse_success": True,
                    "tail_line": tail_line,
                    "tail_candidate": tail_cleaned,
                    "protocol_followed": False,
                }

        if DEBUG_MODE:
            logger.warning(f"[AI响应过滤] 无法从响应中提取决策判断: {final_text[:80]}")

        return {
            "filtered_text": final_text,
            "reasoning_text": reasoning_text,
            "normalized_answer": None,
            "parse_success": False,
            "tail_line": tail_line,
            "tail_candidate": tail_cleaned,
            "protocol_followed": False,
        }

    @staticmethod
    def extract_decision_answer(
        response: str, start_marker: str = "", end_marker: str = ""
    ) -> Optional[str]:
        return AIResponseFilter.parse_decision_response(
            response, start_marker, end_marker
        ).get("normalized_answer")

    @staticmethod
    def parse_frequency_response(
        response: str, start_marker: str = "", end_marker: str = ""
    ) -> Dict[str, Any]:
        """
        从可能包含思考链的响应中提取频率判断（正常/过于频繁/过少）

        这是一个增强版提取器，专门用于频率调整的场景

        Args:
            response: AI响应文本

        Returns:
            提取到的判断结果，如果无法提取则返回None
        """
        if not response:
            return {
                "filtered_text": "",
                "reasoning_text": "",
                "normalized_answer": None,
                "parse_success": False,
                "tail_line": "",
                "tail_candidate": "",
                "protocol_followed": False,
            }

        filtered = AIResponseFilter.filter_thinking_chain(response)
        final_text, reasoning_text = AIResponseFilter._extract_custom_reasoning_block(
            filtered, start_marker, end_marker
        )
        tail_line = AIResponseFilter._extract_last_non_empty_line(final_text)
        tail_cleaned = AIResponseFilter._normalize_tail_token(tail_line)
        exact_map = {
            "正常": "正常",
            "过于频繁": "过于频繁",
            "过少": "过少",
            "合适": "正常",
            "适当": "正常",
            "偏少": "过少",
            "太少": "过少",
            "偏频繁": "过于频繁",
            "太频繁": "过于频繁",
        }
        if tail_cleaned in exact_map:
            return {
                "filtered_text": final_text,
                "reasoning_text": reasoning_text,
                "normalized_answer": exact_map[tail_cleaned],
                "parse_success": True,
                "tail_line": tail_line,
                "tail_candidate": tail_cleaned,
                "protocol_followed": True,
            }

        cleaned = (
            final_text.strip().replace("。", "").replace("!", "").replace("！", "")
        )
        cleaned = cleaned.replace("?", "").replace("？", "").strip()
        cleaned = AIResponseFilter._strip_answer_prefixes(cleaned)

        # 检查完整匹配
        if cleaned in ["正常", "过于频繁", "过少"]:
            return {
                "filtered_text": final_text,
                "reasoning_text": reasoning_text,
                "normalized_answer": cleaned,
                "parse_success": True,
                "tail_line": tail_line,
                "tail_candidate": tail_cleaned,
                "protocol_followed": False,
            }

        # 扩展关键词匹配（更宽松的匹配，因为思考链过滤后可能只剩下简短的词）
        # 优先匹配"过于频繁"相关
        if "过于频繁" in cleaned or "过度频繁" in cleaned or "太频繁" in cleaned:
            return {
                "filtered_text": final_text,
                "reasoning_text": reasoning_text,
                "normalized_answer": "过于频繁",
                "parse_success": True,
                "tail_line": tail_line,
                "tail_candidate": tail_cleaned,
                "protocol_followed": False,
            }

        # 单独的"频繁"也算（但要排除"不频繁"等否定情况）
        if "频繁" in cleaned and "不" not in cleaned and "过" not in cleaned:
            return {
                "filtered_text": final_text,
                "reasoning_text": reasoning_text,
                "normalized_answer": "过于频繁",
                "parse_success": True,
                "tail_line": tail_line,
                "tail_candidate": tail_cleaned,
                "protocol_followed": False,
            }

        # 匹配"过少"相关（包括"太少"）
        if (
            "过少" in cleaned
            or "太少" in cleaned
            or "过于少" in cleaned
            or cleaned == "少"
        ):
            return {
                "filtered_text": final_text,
                "reasoning_text": reasoning_text,
                "normalized_answer": "过少",
                "parse_success": True,
                "tail_line": tail_line,
                "tail_candidate": tail_cleaned,
                "protocol_followed": False,
            }

        # 匹配"正常"相关
        if "正常" in cleaned or "合适" in cleaned or "适当" in cleaned:
            return {
                "filtered_text": final_text,
                "reasoning_text": reasoning_text,
                "normalized_answer": "正常",
                "parse_success": True,
                "tail_line": tail_line,
                "tail_candidate": tail_cleaned,
                "protocol_followed": False,
            }

        # 无法识别
        if DEBUG_MODE:
            logger.warning(f"[AI响应过滤] 无法从响应中提取频率判断: {cleaned[:50]}")

        return {
            "filtered_text": final_text,
            "reasoning_text": reasoning_text,
            "normalized_answer": None,
            "parse_success": False,
            "tail_line": tail_line,
            "tail_candidate": tail_cleaned,
            "protocol_followed": False,
        }

    @staticmethod
    def extract_frequency_decision(
        response: str, start_marker: str = "", end_marker: str = ""
    ) -> Optional[str]:
        return AIResponseFilter.parse_frequency_response(
            response, start_marker, end_marker
        ).get("normalized_answer")
