"""Internal tool visibility policy for formal reply generation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional


STEP_IMAGE_TOOL_NAMES = frozenset({"gcp_step_image_generate", "gcp_step_image_edit"})


@dataclass(frozen=True)
class ToolPolicy:
    """Filter visible tools before injecting tool reminders."""

    allowed_tool_names: frozenset[str] = frozenset()
    denied_tool_names: frozenset[str] = frozenset()
    allowed_plugin_names: frozenset[str] = frozenset()
    max_steps: int = 0
    tool_call_timeout: float = 0.0
    allow_step_image: bool = True

    @classmethod
    def from_allowed_tool_names(
        cls,
        tool_names: Optional[Iterable[str]] = None,
        *,
        allow_step_image: bool = True,
        denied_tool_names: Optional[Iterable[str]] = None,
        allowed_plugin_names: Optional[Iterable[str]] = None,
        max_steps: int = 0,
        tool_call_timeout: float = 0.0,
    ) -> "ToolPolicy":
        return cls(
            allowed_tool_names=_normalize_names(tool_names),
            denied_tool_names=_normalize_names(denied_tool_names),
            allowed_plugin_names=_normalize_names(allowed_plugin_names),
            max_steps=max(0, int(max_steps or 0)),
            tool_call_timeout=max(0.0, float(tool_call_timeout or 0.0)),
            allow_step_image=bool(allow_step_image),
        )

    def is_unrestricted(self) -> bool:
        return (
            not self.allowed_tool_names
            and not self.denied_tool_names
            and not self.allowed_plugin_names
            and self.allow_step_image
        )

    def allows_tool(self, tool: dict) -> bool:
        name = str(tool.get("name", "")).strip()
        if not name:
            return False
        if not self.allow_step_image and name in STEP_IMAGE_TOOL_NAMES:
            return False
        if self.denied_tool_names and name in self.denied_tool_names:
            return False
        if self.allowed_tool_names and name not in self.allowed_tool_names:
            return False
        if self.allowed_plugin_names:
            plugin_name = str(
                tool.get("plugin")
                or tool.get("plugin_name")
                or tool.get("plugin_id")
                or ""
            ).strip()
            if plugin_name not in self.allowed_plugin_names:
                return False
        return True

    def filter_tools(self, tools: Iterable[dict]) -> list[dict]:
        return [tool for tool in tools or [] if self.allows_tool(tool)]

    def allowed_names_for_prompt(
        self, visible_tools: Optional[Iterable[dict]] = None
    ) -> Optional[list[str]]:
        if self.is_unrestricted():
            return None
        if visible_tools is not None:
            return sorted(
                {
                    str(tool.get("name", "")).strip()
                    for tool in visible_tools
                    if str(tool.get("name", "")).strip()
                }
            )
        if not self.allowed_tool_names:
            return None
        names = set(self.allowed_tool_names)
        if not self.allow_step_image:
            names.difference_update(STEP_IMAGE_TOOL_NAMES)
        names.difference_update(self.denied_tool_names)
        return sorted(name for name in names if name)


def _normalize_names(values: Optional[Iterable[str]]) -> frozenset[str]:
    if values is None:
        return frozenset()
    return frozenset(str(value).strip() for value in values if str(value).strip())
