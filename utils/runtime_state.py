"""Runtime state container for the group chat plugin."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class RuntimeState:
    """Mutable runtime-only state owned by one plugin instance."""

    processing_sessions: dict[str, str] = field(default_factory=dict)
    proactive_processing_sessions: dict[str, float] = field(default_factory=dict)
    message_cache_snapshots: dict[str, dict] = field(default_factory=dict)
    smart_batch_snapshots: dict[str, list[dict]] = field(default_factory=dict)
    pending_bot_replies: dict[str, list[str]] = field(default_factory=dict)
    agent_done_flags: set[str] = field(default_factory=set)
    duplicate_blocked_messages: dict[str, bool] = field(default_factory=dict)
    saved_messages: dict[str, float] = field(default_factory=dict)
    seen_message_ids: dict[str, float] = field(default_factory=dict)
    command_messages: dict[str, float] = field(default_factory=dict)
    recent_replies_cache: dict[str, list[dict]] = field(default_factory=dict)
    raw_reply_cache: dict[str, str] = field(default_factory=dict)

    def clear_message(self, message_id: str) -> None:
        """Remove entries scoped to one processed message."""
        self.processing_sessions.pop(message_id, None)
        self.message_cache_snapshots.pop(message_id, None)
        self.smart_batch_snapshots.pop(message_id, None)
        self.pending_bot_replies.pop(message_id, None)
        self.agent_done_flags.discard(message_id)
        self.duplicate_blocked_messages.pop(message_id, None)
        self.saved_messages.pop(message_id, None)
        self.raw_reply_cache.pop(message_id, None)

    def clear_all(self) -> None:
        """Clear all runtime-only state containers."""
        self.processing_sessions.clear()
        self.proactive_processing_sessions.clear()
        self.message_cache_snapshots.clear()
        self.smart_batch_snapshots.clear()
        self.pending_bot_replies.clear()
        self.agent_done_flags.clear()
        self.duplicate_blocked_messages.clear()
        self.saved_messages.clear()
        self.seen_message_ids.clear()
        self.command_messages.clear()
        self.recent_replies_cache.clear()
        self.raw_reply_cache.clear()
