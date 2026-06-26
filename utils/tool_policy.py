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
    # Reserved for dict-style visible tool filtering; executable ToolSet filtering
    # currently uses resolved visible tool names.
    # Reserved for future AstrBot tool-loop integration. These fields are not
    # wired to ProviderRequest or the agent runner yet.
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

    @staticmethod
    def _get_container_tools(tool_container) -> list:
        if not tool_container:
            return []
        tools = getattr(tool_container, "tools", None)
        if tools is not None:
            return list(tools)
        func_list = getattr(tool_container, "func_list", None)
        if func_list is not None:
            return list(func_list)
        return []

    @classmethod
    def filter_tool_container_for_visible_names(
        cls,
        tool_container,
        visible_names: Optional[Iterable[str]],
    ) -> list[str]:
        if tool_container is None or visible_names is None:
            return []

        visible_set = _normalize_names(visible_names)
        removed_names: list[str] = []
        for tool in cls._get_container_tools(tool_container):
            tool_name = str(getattr(tool, "name", "")).strip()
            if not tool_name or tool_name in visible_set:
                continue

            removed_names.append(tool_name)
            if hasattr(tool_container, "remove_tool"):
                tool_container.remove_tool(tool_name)
            elif hasattr(tool_container, "remove_func"):
                tool_container.remove_func(tool_name)
            elif hasattr(tool_container, "tools"):
                tool_container.tools = [
                    item
                    for item in getattr(tool_container, "tools", [])
                    if str(getattr(item, "name", "")).strip() != tool_name
                ]
            elif hasattr(tool_container, "func_list"):
                tool_container.func_list = [
                    item
                    for item in getattr(tool_container, "func_list", [])
                    if str(getattr(item, "name", "")).strip() != tool_name
                ]

        return removed_names

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
