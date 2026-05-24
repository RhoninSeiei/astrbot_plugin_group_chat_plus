"""Conservative system_prompt rewrite helper."""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class SystemPromptRewriteResult:
    merged_system_prompt: str
    strategy: str
    confidence: str
    warnings: list[str] = field(default_factory=list)
    preserved_order: bool = False
    persona_detected: bool = False
    ltm_detected: bool = False
    prefix_detected: bool = False
    suffix_detected: bool = False
    duplicate_suspected: bool = False


class SystemPromptRewriter:
    """Rewrite system_prompt while preserving plugin persona and third-party additions."""

    _KNOWN_LTM_PATTERNS = [
        re.compile(
            (
                r"You are now in a chatroom\. The chat history is as follows:\s*\n?"
                r"(?:\[[^\]]+/\d{2}:\d{2}:\d{2}\]:.*(?:\n(?!---\n).*)*)"
                r"(?:\n---\n\[[^\]]+/\d{2}:\d{2}:\d{2}\]:.*(?:\n(?!---\n).*)*)*"
            ),
            re.IGNORECASE,
        ),
        re.compile(
            (
                r"You are now in a chatroom\. The chat history is as follows:\s*"
                r"[\s\S]*?Now, a new message is coming:\s*`[\s\S]*?`\."
                r"\s*Please react to it\."
            ),
            re.IGNORECASE,
        ),
    ]

    _PERSONA_HEADER_PATTERN = re.compile(
        r"(?:^|\n)# Persona Instructions\s*\n+",
        re.IGNORECASE,
    )

    @staticmethod
    def _normalize_light(text: str) -> str:
        if not text:
            return ""
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    @classmethod
    def _compress_duplicate_blocks(cls, text: str) -> tuple[str, bool]:
        normalized = cls._normalize_light(text)
        if not normalized:
            return "", False
        parts = [part.strip() for part in normalized.split("\n\n") if part.strip()]
        deduped_parts: list[str] = []
        seen = set()
        duplicate_suspected = False
        for part in parts:
            fingerprint = re.sub(r"\s+", " ", part).strip().lower()
            if fingerprint in seen:
                duplicate_suspected = True
                continue
            seen.add(fingerprint)
            deduped_parts.append(part)
        return "\n\n".join(deduped_parts).strip(), duplicate_suspected

    @classmethod
    def _join_parts_preserving_order(cls, parts: list[str]) -> tuple[str, bool]:
        combined = "\n".join(part for part in parts if part).strip()
        return cls._compress_duplicate_blocks(combined)

    @classmethod
    def _strip_known_ltm(cls, text: str) -> tuple[str, bool]:
        if not text:
            return "", False
        cleaned = text
        detected = False
        for pattern in cls._KNOWN_LTM_PATTERNS:
            cleaned, count = pattern.subn("", cleaned)
            detected = detected or count > 0
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
        return cleaned, detected

    @classmethod
    def _build_loose_persona_pattern(
        cls,
        plugin_system_prompt: str,
    ) -> re.Pattern | None:
        normalized_lines = [
            re.escape(line.strip())
            for line in plugin_system_prompt.replace("\r\n", "\n")
            .replace("\r", "\n")
            .split("\n")
            if line.strip()
        ]
        if not normalized_lines:
            return None
        pattern = r"\s*" + r"\s*\n+\s*".join(normalized_lines) + r"\s*"
        return re.compile(pattern, re.DOTALL)

    @classmethod
    def _extract_persona_segment_by_header(
        cls, current_system_prompt: str, plugin_system_prompt: str
    ) -> tuple[str, str, str, bool]:
        if not current_system_prompt or not plugin_system_prompt:
            return "", "", current_system_prompt or "", False
        loose_pattern = cls._build_loose_persona_pattern(plugin_system_prompt)
        if not loose_pattern:
            return "", "", current_system_prompt, False
        for match in cls._PERSONA_HEADER_PATTERN.finditer(current_system_prompt):
            tail = current_system_prompt[match.end() :]
            persona_match = loose_pattern.match(tail)
            if persona_match:
                start = match.start()
                end = match.end() + persona_match.end()
                return (
                    current_system_prompt[:start],
                    current_system_prompt[match.end() : end],
                    current_system_prompt[end:],
                    True,
                )
        return "", "", current_system_prompt, False

    @classmethod
    def _build_exact_match_result(
        cls, prefix: str, suffix: str, plugin_system_prompt: str
    ) -> SystemPromptRewriteResult:
        prefix_cleaned, prefix_ltm = cls._strip_known_ltm(prefix)
        suffix_cleaned, suffix_ltm = cls._strip_known_ltm(suffix)
        merged, duplicate_suspected = cls._join_parts_preserving_order(
            [prefix_cleaned, plugin_system_prompt, suffix_cleaned]
        )
        return SystemPromptRewriteResult(
            merged_system_prompt=merged,
            strategy="exact-match",
            confidence="high",
            preserved_order=True,
            persona_detected=True,
            ltm_detected=prefix_ltm or suffix_ltm,
            prefix_detected=bool(prefix_cleaned),
            suffix_detected=bool(suffix_cleaned),
            duplicate_suspected=duplicate_suspected,
        )

    @classmethod
    def _build_wrapped_match_result(
        cls,
        prefix: str,
        suffix: str,
        plugin_system_prompt: str,
        matched_persona: str,
    ) -> SystemPromptRewriteResult:
        prefix_cleaned, prefix_ltm = cls._strip_known_ltm(prefix)
        suffix_cleaned, suffix_ltm = cls._strip_known_ltm(suffix)
        merged, duplicate_suspected = cls._join_parts_preserving_order(
            [prefix_cleaned, plugin_system_prompt, suffix_cleaned]
        )
        warnings = []
        if matched_persona and matched_persona != plugin_system_prompt:
            warnings.append("人格提示通过包装块归一化识别，未使用精确字符串命中")
        return SystemPromptRewriteResult(
            merged_system_prompt=merged,
            strategy="wrapped-persona-match",
            confidence="medium",
            warnings=warnings,
            preserved_order=True,
            persona_detected=True,
            ltm_detected=prefix_ltm or suffix_ltm,
            prefix_detected=bool(prefix_cleaned),
            suffix_detected=bool(suffix_cleaned),
            duplicate_suspected=duplicate_suspected,
        )

    @staticmethod
    def _build_keep_current_result(
        stripped_current: str,
        current_system_prompt: str,
        ltm_detected: bool,
    ) -> SystemPromptRewriteResult:
        merged, duplicate_suspected = SystemPromptRewriter._compress_duplicate_blocks(
            stripped_current or current_system_prompt
        )
        warnings = [
            "未命中精确 persona 字符串，已保留当前 system_prompt 并仅移除已知平台 LTM 片段"
        ]
        return SystemPromptRewriteResult(
            merged_system_prompt=merged or stripped_current or current_system_prompt,
            strategy="conservative-keep-current",
            confidence="medium" if ltm_detected else "low",
            warnings=warnings,
            preserved_order=False,
            persona_detected=True,
            ltm_detected=ltm_detected,
            prefix_detected=True,
            suffix_detected=True,
            duplicate_suspected=duplicate_suspected,
        )

    @staticmethod
    def _build_prepend_plugin_result(
        plugin_system_prompt: str,
        stripped_current: str,
        ltm_detected: bool,
    ) -> SystemPromptRewriteResult:
        merged, duplicate_suspected = SystemPromptRewriter._join_parts_preserving_order(
            [plugin_system_prompt, stripped_current]
        )
        warnings = ["未能高置信度识别 persona 边界，已进入保守兼容模式"]
        if not ltm_detected:
            warnings.append("未识别到已知平台 LTM 片段，可能保留未来版本 AstrBot 的重复提示词")
        return SystemPromptRewriteResult(
            merged_system_prompt=merged,
            strategy="conservative-prepend-plugin",
            confidence="low",
            warnings=warnings,
            preserved_order=False,
            persona_detected=False,
            ltm_detected=ltm_detected,
            prefix_detected=bool(stripped_current),
            suffix_detected=False,
            duplicate_suspected=duplicate_suspected,
        )

    @staticmethod
    def _build_no_plugin_result(current_system_prompt: str) -> SystemPromptRewriteResult:
        merged, duplicate_suspected = SystemPromptRewriter._compress_duplicate_blocks(
            current_system_prompt
        )
        return SystemPromptRewriteResult(
            merged_system_prompt=merged,
            strategy="no-plugin-system-prompt",
            confidence="medium",
            warnings=["插件未提供 system_prompt，直接保留当前 system_prompt"],
            preserved_order=True,
            duplicate_suspected=duplicate_suspected,
        )

    @staticmethod
    def _build_empty_current_result(
        plugin_system_prompt: str,
    ) -> SystemPromptRewriteResult:
        merged, duplicate_suspected = SystemPromptRewriter._compress_duplicate_blocks(
            plugin_system_prompt
        )
        return SystemPromptRewriteResult(
            merged_system_prompt=merged,
            strategy="empty-current-system-prompt",
            confidence="high",
            preserved_order=True,
            persona_detected=True,
            duplicate_suspected=duplicate_suspected,
        )

    @classmethod
    def rewrite_preserving_plugin_base(
        cls, current_system_prompt: str, plugin_system_prompt: str
    ) -> SystemPromptRewriteResult:
        return cls.rewrite(current_system_prompt, plugin_system_prompt)

    @classmethod
    def rewrite(
        cls, current_system_prompt: str, plugin_system_prompt: str
    ) -> SystemPromptRewriteResult:
        current_system_prompt = current_system_prompt or ""
        plugin_system_prompt = plugin_system_prompt or ""

        if not plugin_system_prompt:
            return cls._build_no_plugin_result(current_system_prompt)
        if not current_system_prompt:
            return cls._build_empty_current_result(plugin_system_prompt)

        normalized_plugin = cls._normalize_light(plugin_system_prompt)
        exact_index = current_system_prompt.find(plugin_system_prompt)
        if exact_index >= 0:
            prefix = current_system_prompt[:exact_index]
            suffix = current_system_prompt[exact_index + len(plugin_system_prompt) :]
            return cls._build_exact_match_result(prefix, suffix, plugin_system_prompt)

        prefix, matched_persona, suffix, wrapped_detected = (
            cls._extract_persona_segment_by_header(
                current_system_prompt, plugin_system_prompt
            )
        )
        if wrapped_detected:
            return cls._build_wrapped_match_result(
                prefix, suffix, plugin_system_prompt, matched_persona
            )

        stripped_current, ltm_detected = cls._strip_known_ltm(current_system_prompt)
        normalized_stripped = cls._normalize_light(stripped_current)
        if normalized_plugin and normalized_plugin in normalized_stripped:
            return cls._build_keep_current_result(
                stripped_current, current_system_prompt, ltm_detected
            )

        return cls._build_prepend_plugin_result(
            plugin_system_prompt, stripped_current, ltm_detected
        )
