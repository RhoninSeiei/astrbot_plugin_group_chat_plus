"""Smart concurrent batch coordinator."""

from __future__ import annotations

import asyncio
import time
from typing import Dict, List, Optional
from astrbot.api import logger


class SmartConcurrentManager:
    """Coordinate same-chat message batches when `concurrent_mode=smart`."""

    _pending: Dict[str, Dict[str, dict]] = {}
    _consumed: Dict[str, dict] = {}
    _lock: asyncio.Lock = None
    _EXPIRE_SECONDS: float = 15.0
    _MAX_BATCH_SIZE: int = 20

    @classmethod
    def _get_lock(cls) -> asyncio.Lock:
        if cls._lock is None:
            cls._lock = asyncio.Lock()
        return cls._lock

    @classmethod
    async def register_arrival(
        cls,
        chat_id: str,
        processing_id: str,
        source_event_id: str = "",
        arrival_seq: int = 0,
        arrival_monotonic: float = 0.0,
    ) -> None:
        try:
            async with cls._get_lock():
                if chat_id not in cls._pending:
                    cls._pending[chat_id] = {}
                existing = cls._pending[chat_id].get(processing_id, {})
                cls._pending[chat_id][processing_id] = {
                    **existing,
                    "processing_id": processing_id,
                    "source_event_id": source_event_id
                    or existing.get("source_event_id", ""),
                    "arrival_seq": arrival_seq or existing.get("arrival_seq", 0),
                    "arrival_monotonic": arrival_monotonic
                    or existing.get("arrival_monotonic", 0.0)
                    or time.monotonic(),
                    "registered_at": existing.get("registered_at", time.time()),
                    "payload_ready": existing.get("payload_ready", False),
                }
                cls._cleanup_expired_locked(chat_id)
        except Exception as e:
            logger.warning(f"[SmartConcurrent] register_arrival 失败: {e}")

    @classmethod
    async def attach_payload(
        cls,
        chat_id: str,
        processing_id: str,
        content: str,
        sender_name: str,
        sender_id: str,
        cached_data: dict,
        is_forced: bool = False,
    ) -> None:
        try:
            async with cls._get_lock():
                if chat_id not in cls._pending:
                    cls._pending[chat_id] = {}
                existing = cls._pending[chat_id].get(processing_id, {})
                cls._pending[chat_id][processing_id] = {
                    **existing,
                    "processing_id": processing_id,
                    "content": content,
                    "sender_name": sender_name,
                    "sender_id": sender_id,
                    "cached_data": cached_data,
                    "is_forced": is_forced,
                    "payload_ready": True,
                    "payload_attached_at": time.time(),
                }
                cls._cleanup_expired_locked(chat_id)
        except Exception as e:
            logger.warning(f"[SmartConcurrent] attach_payload 失败: {e}")

    @classmethod
    async def is_consumed(cls, processing_id: str) -> bool:
        return processing_id in cls._consumed

    @classmethod
    async def get_consumer(cls, processing_id: str) -> Optional[str]:
        info = cls._consumed.get(processing_id)
        if not info:
            return None
        return info.get("anchor_processing_id")

    @classmethod
    async def has_earlier_pending(cls, chat_id: str, processing_id: str) -> bool:
        try:
            async with cls._get_lock():
                cls._cleanup_expired_locked(chat_id)
                current = cls._pending.get(chat_id, {}).get(processing_id)
                if not current:
                    return False
                current_seq = current.get("arrival_seq", 0)
                for pid, entry in cls._pending.get(chat_id, {}).items():
                    if pid == processing_id:
                        continue
                    if entry.get("arrival_seq", 0) < current_seq:
                        return True
                return False
        except Exception as e:
            logger.warning(f"[SmartConcurrent] has_earlier_pending 失败: {e}")
            return False

    @classmethod
    async def claim_batch(cls, chat_id: str, processing_id: str) -> dict:
        try:
            async with cls._get_lock():
                cls._cleanup_expired_locked(chat_id)
                consumed_info = cls._consumed.get(processing_id)
                if consumed_info:
                    return {
                        "is_consumed": True,
                        "anchor_processing_id": consumed_info.get(
                            "anchor_processing_id"
                        ),
                        "merged_entries": [],
                    }

                chat_pending = cls._pending.get(chat_id, {})
                current = chat_pending.get(processing_id)
                if not current:
                    return {
                        "is_missing": True,
                        "is_anchor": False,
                        "merged_entries": [],
                    }

                ordered_entries = sorted(
                    chat_pending.values(),
                    key=lambda entry: (
                        entry.get("arrival_seq", 0),
                        entry.get("arrival_monotonic", 0.0),
                    ),
                )
                if not ordered_entries:
                    return {"is_anchor": False, "merged_entries": []}

                anchor = ordered_entries[0]
                if anchor.get("processing_id") != processing_id:
                    return {
                        "is_anchor": False,
                        "blocked_by": anchor.get("processing_id"),
                        "merged_entries": [],
                    }

                merged_entries: List[dict] = []
                for entry in ordered_entries[1:]:
                    entry_pid = entry.get("processing_id")
                    if not entry_pid or entry_pid == processing_id:
                        continue
                    if len(merged_entries) >= cls._MAX_BATCH_SIZE:
                        break
                    if entry.get("is_forced", False):
                        break
                    if not entry.get("payload_ready", False):
                        continue
                    merged_entries.append(entry)
                    cls._consumed[entry_pid] = {
                        "consumed_at": time.time(),
                        "anchor_processing_id": processing_id,
                    }

                chat_pending.pop(processing_id, None)
                for entry in merged_entries:
                    entry_pid = entry.get("processing_id")
                    if entry_pid:
                        chat_pending.pop(entry_pid, None)
                if not chat_pending:
                    cls._pending.pop(chat_id, None)

                return {
                    "is_anchor": True,
                    "is_consumed": False,
                    "anchor_entry": current,
                    "anchor_is_forced": bool(current.get("is_forced", False)),
                    "merged_entries": merged_entries,
                }
        except Exception as e:
            logger.warning(f"[SmartConcurrent] claim_batch 失败: {e}")
            return {"is_anchor": False, "merged_entries": []}

    @classmethod
    async def remove_self(cls, chat_id: str, processing_id: str) -> None:
        try:
            async with cls._get_lock():
                if chat_id in cls._pending:
                    cls._pending[chat_id].pop(processing_id, None)
                    if not cls._pending[chat_id]:
                        cls._pending.pop(chat_id, None)
                cls._consumed.pop(processing_id, None)
                cls._cleanup_expired_locked(chat_id)
        except Exception as e:
            logger.warning(f"[SmartConcurrent] remove_self 失败: {e}")

    @classmethod
    def _cleanup_expired_locked(cls, chat_id: str) -> None:
        now = time.time()
        if chat_id in cls._pending:
            expired_pending = []
            for pid, entry in cls._pending[chat_id].items():
                registered_at = entry.get("registered_at", now)
                attached_at = entry.get("payload_attached_at", registered_at)
                if now - max(registered_at, attached_at) > cls._EXPIRE_SECONDS:
                    expired_pending.append(pid)
            for pid in expired_pending:
                cls._pending[chat_id].pop(pid, None)
            if not cls._pending[chat_id]:
                cls._pending.pop(chat_id, None)

        expired_consumed = [
            pid
            for pid, info in cls._consumed.items()
            if now - info.get("consumed_at", now) > cls._EXPIRE_SECONDS
        ]
        for pid in expired_consumed:
            cls._consumed.pop(pid, None)
