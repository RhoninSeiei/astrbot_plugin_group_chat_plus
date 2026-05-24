"""
注意力冷却管理器模块

管理用户注意力冷却状态。当决策AI决定不回复时，
用户会被添加到注意力冷却列表，阻止自动增加关注度，
直到满足解除条件。

核心功能：
1. 注意力冷却列表管理 - 添加、移除、查询用户注意力冷却状态
2. 超时自动解除 - 注意力冷却超过最大时长时自动解除
3. 与关注列表同步 - 保持数据一致性
4. 持久化存储 - 将数据保存到磁盘

作者: Him666233
版本: v1.2.1
"""

import time
import asyncio
import json
from pathlib import Path
from typing import Dict, Any, Optional, List
from astrbot.api.all import logger

# 调试日志开关
DEBUG_MODE: bool = False


class CooldownManager:
    """
    注意力冷却状态管理器（支持持久化）

    主要功能：
    1. 注意力冷却列表管理 - 追踪处于注意力冷却状态的用户
    2. 超时检测 - 自动解除过期的注意力冷却状态
    3. 数据同步 - 与关注列表同步
    4. 持久化存储 - 保存到 data/plugin_data/chat_plus/cooldown_data.json

    数据结构：
    _cooldown_map: Dict[str, Dict[str, Dict[str, Any]]] = {
        "chat_key": {
            "user_id": {
                "cooldown_start": timestamp,  # 注意力冷却开始时间
                "reason": str,                # 注意力冷却原因
                "user_name": str,             # 用户名（用于日志）
            }
        }
    }
    """

    # 注意力冷却列表数据
    _cooldown_map: Dict[str, Dict[str, Dict[str, Any]]] = {}
    _pending_cooldown_map: Dict[str, Dict[str, Dict[str, Any]]] = {}

    # 异步锁
    _lock = asyncio.Lock()

    # 持久化存储路径
    _storage_path: Optional[Path] = None

    # 初始化标志
    _initialized: bool = False

    # 配置常量（可通过配置文件调整）
    MAX_COOLDOWN_DURATION: int = 600  # 最大注意力冷却时长（秒），默认10分钟
    COOLDOWN_TRIGGER_THRESHOLD: float = 0.3  # 触发注意力冷却的最小关注度阈值
    COOLDOWN_ATTENTION_DECREASE: float = 0.2  # 触发注意力冷却时额外减少的关注度
    ENABLE_PENDING_COOLDOWN: bool = True
    PENDING_COOLDOWN_GRACE_USER_MESSAGES: int = 1
    PENDING_COOLDOWN_MAX_WAIT_SECONDS: int = 60
    PENDING_COOLDOWN_SAME_USER_PROBABILITY_FLOOR: float = 0.18
    ENABLE_AUTO_RELEASE: bool = True

    # 自动保存配置
    AUTO_SAVE_INTERVAL: int = 60  # 自动保存间隔（秒）
    _last_save_time: float = 0  # 上次保存时间

    @staticmethod
    def initialize(
        data_dir: Optional[str] = None, config: Optional[Dict[str, Any]] = None
    ) -> None:
        """
        初始化注意力冷却管理器（设置存储路径并加载数据anager (set storage path and load data)

        参数：
            data_dir: 数据目录路径（来自 StarTools.get_data_dir()）
            config: 插件配置字典（用于加载注意力冷却配置）
        """
        if CooldownManager._initialized:
            return

        if not data_dir:
            logger.error(
                "[注意力冷却] 未提供 data_dir，持久化已禁用。"
                "请使用 StarTools.get_data_dir() 获取数据目录。"
            )
            CooldownManager._storage_path = None
            CooldownManager._initialized = True
            return

        # 设置存储路径
        CooldownManager._storage_path = Path(data_dir) / "cooldown_data.json"

        # 加载已有数据
        CooldownManager._load_from_disk()

        # 加载配置参数
        if config:
            CooldownManager._load_config(config)

        CooldownManager._initialized = True

        if DEBUG_MODE:
            logger.info(f"[注意力冷却] 持久化已初始化：{CooldownManager._storage_path}")
            logger.info(
                f"[注意力冷却] 配置：最大时长={CooldownManager.MAX_COOLDOWN_DURATION}秒，"
                f"阈值={CooldownManager.COOLDOWN_TRIGGER_THRESHOLD}，"
                f"减少量={CooldownManager.COOLDOWN_ATTENTION_DECREASE}"
            )

    @staticmethod
    def _load_config(config: Dict[str, Any]) -> None:
        """
        从配置字典加载注意力冷却配置

        说明：配置由 main.py 统一提取后传入，此处直接使用传入的值
          提供默认值（避免 AstrBot 平台多次读取配置的问题）

        参数：
            config: 插件配置字典（由 main.py 统一提取）
        """
        # 最大注意力冷却时长
        CooldownManager.MAX_COOLDOWN_DURATION = int(
            config.get("cooldown_max_duration", CooldownManager.MAX_COOLDOWN_DURATION)
        )

        # 触发注意力冷却的关注度阈值
        CooldownManager.COOLDOWN_TRIGGER_THRESHOLD = float(
            config.get(
                "cooldown_trigger_threshold",
                CooldownManager.COOLDOWN_TRIGGER_THRESHOLD,
            )
        )

        # 触发注意力冷却时额外减少的关注度
        CooldownManager.COOLDOWN_ATTENTION_DECREASE = float(
            config.get(
                "cooldown_attention_decrease",
                CooldownManager.COOLDOWN_ATTENTION_DECREASE,
            )
        )
        CooldownManager.ENABLE_PENDING_COOLDOWN = bool(
            config.get("enable_pending_attention_cooldown", True)
        )
        CooldownManager.PENDING_COOLDOWN_GRACE_USER_MESSAGES = max(
            1, int(config.get("pending_cooldown_grace_user_messages", 1))
        )
        CooldownManager.PENDING_COOLDOWN_MAX_WAIT_SECONDS = max(
            5, int(config.get("pending_cooldown_max_wait_seconds", 60))
        )
        CooldownManager.PENDING_COOLDOWN_SAME_USER_PROBABILITY_FLOOR = max(
            0.0,
            min(
                1.0,
                float(config.get("pending_cooldown_same_user_probability_floor", 0.18)),
            ),
        )
        CooldownManager.ENABLE_AUTO_RELEASE = bool(
            config.get("enable_cooldown_auto_release", True)
        )

    @staticmethod
    def _load_from_disk() -> None:
        """从磁盘加载注意力冷却数据"""
        if (
            not CooldownManager._storage_path
            or not CooldownManager._storage_path.exists()
        ):
            if DEBUG_MODE:
                logger.info("[注意力冷却] 无历史文件，从空白开始")
            return

        try:
            with open(CooldownManager._storage_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict) and (
                    "active" in data or "pending" in data
                ):
                    active = data.get("active", {}) or {}
                    pending = data.get("pending", {}) or {}
                    CooldownManager._cooldown_map = (
                        active if isinstance(active, dict) else {}
                    )
                    CooldownManager._pending_cooldown_map = (
                        pending if isinstance(pending, dict) else {}
                    )
                else:
                    CooldownManager._cooldown_map = data if isinstance(data, dict) else {}
                    CooldownManager._pending_cooldown_map = {}
                if DEBUG_MODE:
                    logger.info(
                        f"[注意力冷却] 已加载 active={len(CooldownManager._cooldown_map)} "
                        f"pending={len(CooldownManager._pending_cooldown_map)} 个会话"
                    )
        except json.JSONDecodeError as e:
            logger.error(f"[注意力冷却] 数据损坏：{e}，从空白开始")
            CooldownManager._cooldown_map = {}
            CooldownManager._pending_cooldown_map = {}
        except Exception as e:
            logger.error(f"[注意力冷却] 加载失败：{e}，从空白开始")
            CooldownManager._cooldown_map = {}
            CooldownManager._pending_cooldown_map = {}

    @staticmethod
    def _save_to_disk(force: bool = False) -> None:
        """
        将注意力冷却数据保存到磁盘

        参数：
            force: 强制保存（跳过时间检查）
        """
        if not CooldownManager._storage_path:
            return

        # 检查是否需要保存（避免频繁写入磁盘）
        current_time = time.time()
        if (
            not force
            and (current_time - CooldownManager._last_save_time)
            < CooldownManager.AUTO_SAVE_INTERVAL
        ):
            return

        try:
            # 确保目录存在
            CooldownManager._storage_path.parent.mkdir(parents=True, exist_ok=True)

            # 保存数据
            with open(CooldownManager._storage_path, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "active": CooldownManager._cooldown_map,
                        "pending": CooldownManager._pending_cooldown_map,
                    },
                    f,
                    ensure_ascii=False,
                    indent=2,
                )

            CooldownManager._last_save_time = current_time
            if DEBUG_MODE:
                logger.info(
                    f"[注意力冷却] 已保存到磁盘（{len(CooldownManager._cooldown_map)} 个会话）"
                )
        except Exception as e:
            logger.error(f"[注意力冷却] 保存失败：{e}")

    @staticmethod
    async def _auto_save_if_needed() -> None:
        """如果超过时间阈值则自动保存"""
        CooldownManager._save_to_disk(force=False)

    @staticmethod
    def _build_pending_entry(
        user_name: str,
        reason: str,
        trigger_message_id: str = "",
        trigger_message_timestamp: float = 0,
        trigger_attention_before: float = 0.0,
        trigger_attention_after: float = 0.0,
    ) -> Dict[str, Any]:
        return {
            "pending_start": time.time(),
            "reason": reason,
            "user_name": user_name or "未知",
            "trigger_message_id": trigger_message_id or "",
            "trigger_message_timestamp": trigger_message_timestamp or 0,
            "trigger_attention_before": trigger_attention_before,
            "trigger_attention_after": trigger_attention_after,
            "consumed_user_messages": 0,
            "grace_message_budget": max(
                1, CooldownManager.PENDING_COOLDOWN_GRACE_USER_MESSAGES
            ),
            "same_user_reengage_seen": False,
            "last_same_user_decision": "",
        }

    @staticmethod
    async def add_pending_cooldown(
        chat_key: str,
        user_id: str,
        user_name: str,
        reason: str = "decision_ai_no_reply",
        trigger_message_id: str = "",
        trigger_message_timestamp: float = 0,
        trigger_attention_before: float = 0.0,
        trigger_attention_after: float = 0.0,
    ) -> bool:
        """添加待冷却状态。待冷却只观察同一用户后续消息，确认后再升级为正式冷却。"""
        if not CooldownManager.ENABLE_PENDING_COOLDOWN:
            return False

        async with CooldownManager._lock:
            chat_pending = CooldownManager._pending_cooldown_map.setdefault(
                chat_key, {}
            )
            existed = user_id in chat_pending
            chat_pending[user_id] = CooldownManager._build_pending_entry(
                user_name=user_name,
                reason=reason,
                trigger_message_id=trigger_message_id,
                trigger_message_timestamp=trigger_message_timestamp,
                trigger_attention_before=trigger_attention_before,
                trigger_attention_after=trigger_attention_after,
            )
            if not existed:
                logger.info(
                    f"[注意力冷却] 用户 {user_name}(ID:{user_id}) 已进入候选冷却，原因：{reason}"
                )
            await CooldownManager._auto_save_if_needed()
            return not existed

    @staticmethod
    async def get_pending_info(chat_key: str, user_id: str) -> Optional[Dict[str, Any]]:
        async with CooldownManager._lock:
            chat_pending = CooldownManager._pending_cooldown_map.get(chat_key)
            if not chat_pending or user_id not in chat_pending:
                return None
            info = chat_pending[user_id].copy()
            info["elapsed_time"] = time.time() - info.get("pending_start", 0)
            info["remaining_time"] = max(
                0,
                CooldownManager.PENDING_COOLDOWN_MAX_WAIT_SECONDS
                - info["elapsed_time"],
            )
            return info

    @staticmethod
    async def is_in_pending_cooldown(chat_key: str, user_id: str) -> bool:
        async with CooldownManager._lock:
            return user_id in CooldownManager._pending_cooldown_map.get(chat_key, {})

    @staticmethod
    async def clear_pending_cooldown(
        chat_key: str, user_id: str, reason: str = "manual"
    ) -> bool:
        async with CooldownManager._lock:
            chat_pending = CooldownManager._pending_cooldown_map.get(chat_key)
            if not chat_pending or user_id not in chat_pending:
                return False

            pending_info = chat_pending[user_id]
            user_name = pending_info.get("user_name", "未知")
            duration = time.time() - pending_info.get("pending_start", 0)
            del chat_pending[user_id]
            if not chat_pending:
                CooldownManager._pending_cooldown_map.pop(chat_key, None)

            logger.info(
                f"[注意力冷却] 用户 {user_name}(ID:{user_id}) 已从候选冷却列表移除，"
                f"原因：{reason}，持续时间：{duration:.1f}秒"
            )
            CooldownManager._save_to_disk(force=True)
            return True

    @staticmethod
    async def promote_pending_to_active(
        chat_key: str, user_id: str, reason: str = "pending_promoted"
    ) -> bool:
        async with CooldownManager._lock:
            chat_pending = CooldownManager._pending_cooldown_map.get(chat_key)
            if not chat_pending or user_id not in chat_pending:
                return False

            pending_info = chat_pending[user_id]
            chat_cooldowns = CooldownManager._cooldown_map.setdefault(chat_key, {})
            chat_cooldowns[user_id] = {
                "cooldown_start": time.time(),
                "reason": reason,
                "user_name": pending_info.get("user_name", "未知"),
                "promoted_from_pending": True,
                "trigger_message_id": pending_info.get("trigger_message_id", ""),
                "trigger_message_timestamp": pending_info.get(
                    "trigger_message_timestamp", 0
                ),
            }

            del chat_pending[user_id]
            if not chat_pending:
                CooldownManager._pending_cooldown_map.pop(chat_key, None)

            logger.info(
                f"[注意力冷却] 用户 {pending_info.get('user_name', '未知')}(ID:{user_id}) "
                f"候选冷却已升级为正式冷却，原因：{reason}"
            )
            CooldownManager._save_to_disk(force=True)
            return True

    @staticmethod
    async def consume_pending_by_same_user_message(
        chat_key: str,
        user_id: str,
        message_id: str = "",
        message_timestamp: float = 0,
        is_at_ai: bool = False,
        mention_other: bool = False,
        has_trigger_keyword: bool = False,
        is_empty_at: bool = False,
    ) -> Optional[Dict[str, Any]]:
        async with CooldownManager._lock:
            chat_pending = CooldownManager._pending_cooldown_map.get(chat_key)
            if not chat_pending:
                return None
            pending_info = chat_pending.get(user_id)
            if not pending_info:
                return None

            pending_info["last_same_user_message_id"] = message_id or ""
            pending_info["last_same_user_message_timestamp"] = message_timestamp or 0
            pending_info["last_same_user_is_at_ai"] = bool(is_at_ai)
            pending_info["last_same_user_mention_other"] = bool(mention_other)

            if is_at_ai or is_empty_at or (has_trigger_keyword and not mention_other):
                pending_info["same_user_reengage_seen"] = True
                pending_info["last_same_user_decision"] = "reengage_ai"
            else:
                pending_info["consumed_user_messages"] = (
                    pending_info.get("consumed_user_messages", 0) + 1
                )
                pending_info["last_same_user_decision"] = (
                    "still_other_target" if mention_other else "ambiguous"
                )

            snapshot = pending_info.copy()
            snapshot["should_promote"] = not pending_info.get(
                "same_user_reengage_seen", False
            ) and pending_info.get("consumed_user_messages", 0) >= pending_info.get(
                "grace_message_budget",
                CooldownManager.PENDING_COOLDOWN_GRACE_USER_MESSAGES,
            )
            return snapshot

    @staticmethod
    async def mark_pending_decision_result(
        chat_key: str,
        user_id: str,
        should_reply: bool,
        explicitly_to_other: bool = False,
    ) -> Optional[str]:
        async with CooldownManager._lock:
            chat_pending = CooldownManager._pending_cooldown_map.get(chat_key)
            if not chat_pending:
                return None
            pending_info = chat_pending.get(user_id)
            if not pending_info:
                return None

            if should_reply or pending_info.get("same_user_reengage_seen", False):
                pending_info["last_same_user_decision"] = "reengage_ai"
                return "cancel"

            if explicitly_to_other:
                pending_info["last_same_user_decision"] = "still_other_target"

            if pending_info.get("consumed_user_messages", 0) >= pending_info.get(
                "grace_message_budget",
                CooldownManager.PENDING_COOLDOWN_GRACE_USER_MESSAGES,
            ):
                return "promote"

            return "keep"

    @staticmethod
    async def check_and_release_expired_pending(chat_key: str) -> List[str]:
        released_users: List[str] = []
        current_time = time.time()
        async with CooldownManager._lock:
            chat_pending = CooldownManager._pending_cooldown_map.get(chat_key)
            if not chat_pending:
                return released_users

            for user_id, pending_info in list(chat_pending.items()):
                elapsed = current_time - pending_info.get("pending_start", 0)
                if elapsed < CooldownManager.PENDING_COOLDOWN_MAX_WAIT_SECONDS:
                    continue
                user_name = pending_info.get("user_name", "未知")
                del chat_pending[user_id]
                released_users.append(user_id)
                logger.info(
                    f"[注意力冷却] 用户 {user_name}(ID:{user_id}) 候选冷却已自动解除，"
                    f"原因：超时，持续时间：{elapsed:.1f}秒"
                )

            if not chat_pending:
                CooldownManager._pending_cooldown_map.pop(chat_key, None)

        return released_users

    @staticmethod
    async def is_user_under_cooldown_control(chat_key: str, user_id: str) -> tuple:
        async with CooldownManager._lock:
            if user_id in CooldownManager._cooldown_map.get(chat_key, {}):
                return True, "active"
            if user_id in CooldownManager._pending_cooldown_map.get(chat_key, {}):
                return True, "pending"
            return False, "none"

    @staticmethod
    async def handle_same_user_reengage(
        chat_key: str, user_id: str, is_at_ai: bool = False
    ) -> Dict[str, bool]:
        result = {"cleared_pending": False, "cleared_active": False}
        if await CooldownManager.is_in_pending_cooldown(chat_key, user_id):
            result["cleared_pending"] = await CooldownManager.clear_pending_cooldown(
                chat_key, user_id, reason="same_user_reengage"
            )
        return result

    @staticmethod
    async def add_to_cooldown(
        chat_key: str,
        user_id: str,
        user_name: str,
        reason: str = "decision_ai_no_reply",
        trigger_message_id: str = "",
        trigger_message_timestamp: float = 0,
    ) -> bool:
        """
        将用户添加到注意力冷却列表

        参数：
            chat_key: 会话唯一标识符
            user_id: 用户ID
            user_name: 用户名（用于日志）
            reason: 注意力冷却原因

        返回
            Wh成功添加（如果已在注意力冷却中则返回False）
        """
        async with CooldownManager._lock:
            # 初始化 chat_key
            if chat_key not in CooldownManager._cooldown_map:
                CooldownManager._cooldown_map[chat_key] = {}

            chat_cooldowns = CooldownManager._cooldown_map[chat_key]

            # 检查是否已在注意力冷却中
            if user_id in chat_cooldowns:
                if DEBUG_MODE:
                    logger.info(
                        f"[注意力冷却] 用户 {user_name}(ID:{user_id}) 已在注意力冷却中，跳过"
                    )
                return False

            if user_id in CooldownManager._pending_cooldown_map.get(chat_key, {}):
                del CooldownManager._pending_cooldown_map[chat_key][user_id]
                if not CooldownManager._pending_cooldown_map[chat_key]:
                    CooldownManager._pending_cooldown_map.pop(chat_key, None)

            # 添加到注意力冷却列表
            chat_cooldowns[user_id] = {
                "cooldown_start": time.time(),
                "reason": reason,
                "user_name": user_name,
                "trigger_message_id": trigger_message_id or "",
                "trigger_message_timestamp": trigger_message_timestamp or 0,
            }

            logger.info(
                f"[注意力冷却] 用户 {user_name}(ID:{user_id}) 已添加到注意力冷却列表，原因：{reason}"
            )

            # 自动保存
            await CooldownManager._auto_save_if_needed()

            return True

    @staticmethod
    async def remove_from_cooldown(
        chat_key: str, user_id: str, reason: str = "manual"
    ) -> bool:
        """
        将用户从注意力冷却列表移除

        参数：
            chat_key: 会话唯一标识符
            user_id: 用户ID
            reason: 移除原因

        返回：
            是否成功移除（如果不在注意力冷却中则返回Falsemoved (False if not in cooldown)
        """
        async with CooldownManager._lock:
            if chat_key not in CooldownManager._cooldown_map:
                return False

            chat_cooldowns = CooldownManager._cooldown_map[chat_key]

            if user_id not in chat_cooldowns:
                return False

            # 获取用户信息用于日志
            user_info = chat_cooldowns[user_id]
            user_name = user_info.get("user_name", "未知")
            cooldown_start = user_info.get("cooldown_start", 0)
            duration = time.time() - cooldown_start

            # 从注意力冷却列表移除
            del chat_cooldowns[user_id]

            # 清理空会话
            if not chat_cooldowns:
                del CooldownManager._cooldown_map[chat_key]

            logger.info(
                f"[注意力冷却] 用户 {user_name}(ID:{user_id}) 已从注意力冷却列表移除，"
                f"原因：{reason}，持续时间：{duration:.1f}秒"
            )

            # 强制保存（状态变更）
            CooldownManager._save_to_disk(force=True)

            return True

    @staticmethod
    async def is_in_cooldown(chat_key: str, user_id: str) -> bool:
        """
        检查用户是否在注意力冷却列表中

        参数：
            chat_key: 会话唯一标识符
            user_id: 用户ID

        返回：
            是否在注意力冷却列表中
        """
        async with CooldownManager._lock:
            if chat_key not in CooldownManager._cooldown_map:
                return False

            return user_id in CooldownManager._cooldown_map[chat_key]

    @staticmethod
    async def get_cooldown_info(
        chat_key: str, user_id: str
    ) -> Optional[Dict[str, Any]]:
        """
        获取用户注意力冷却信息

        参数：
            chat_key: 会话唯一标识符
            user_id: 用户ID

        返回：
            注意力冷却信息字典，如果不在注意力冷却中则返回None
        """
        async with CooldownManager._lock:
            if chat_key not in CooldownManager._cooldown_map:
                return None

            chat_cooldowns = CooldownManager._cooldown_map[chat_key]

            if user_id not in chat_cooldowns:
                return None

            info = chat_cooldowns[user_id].copy()
            # 计算已过时间
            info["elapsed_time"] = time.time() - info.get("cooldown_start", 0)
            info["remaining_time"] = max(
                0, CooldownManager.MAX_COOLDOWN_DURATION - info["elapsed_time"]
            )

            return info

    @staticmethod
    async def check_and_release_expired(chat_key: str) -> List[str]:
        """
        检查并释放会话中过期的注意力冷却状态

        参数：
            chat_key: 会话唯一标识符

        返回：
            被释放的用户ID列表
        """
        released_users: List[str] = []
        current_time = time.time()

        if not CooldownManager.ENABLE_AUTO_RELEASE:
            return released_users

        async with CooldownManager._lock:
            if chat_key not in CooldownManager._cooldown_map:
                return released_users

            chat_cooldowns = CooldownManager._cooldown_map[chat_key]
            users_to_release: List[str] = []

            # 查找过期的注意力冷却
            for user_id, cooldown_info in chat_cooldowns.items():
                cooldown_start = cooldown_info.get("cooldown_start", 0)
                elapsed_time = current_time - cooldown_start

                if elapsed_time >= CooldownManager.MAX_COOLDOWN_DURATION:
                    users_to_release.append(user_id)

            # 释放过期用户（在迭代外执行以避免迭代时修改字典）
            for user_id in users_to_release:
                user_info = chat_cooldowns[user_id]
                user_name = user_info.get("user_name", "未知")
                cooldown_start = user_info.get("cooldown_start", 0)
                duration = current_time - cooldown_start

                del chat_cooldowns[user_id]
                released_users.append(user_id)

                logger.info(
                    f"[注意力冷却] 用户 {user_name}(ID:{user_id}) 已自动解除注意力冷却，"
                    f"原因：超时，持续时间：{duration:.1f}秒"
                )

            # 清理空会话
            if not chat_cooldowns:
                del CooldownManager._cooldown_map[chat_key]

            # 如果有用户被释放则强制保存
            if released_users:
                CooldownManager._save_to_disk(force=True)

        return released_users

    @staticmethod
    async def sync_with_attention_list(
        chat_key: str, attention_user_ids: List[str]
    ) -> List[str]:
        """
        与关注列表同步注意力冷却列表 - 移除不在关注列表中的用户

        参数：
            chat_key: 会话唯一标识符
            attention_user_ids: 当前在关注列表中的用户ID列表

        返回：
            被移除的用户ID列表
        """
        removed_users: List[str] = []
        pending_removed_users: List[str] = []

        async with CooldownManager._lock:
            if (
                chat_key not in CooldownManager._cooldown_map
                and chat_key not in CooldownManager._pending_cooldown_map
            ):
                return removed_users

            attention_set = set(attention_user_ids)

            if chat_key in CooldownManager._cooldown_map:
                chat_cooldowns = CooldownManager._cooldown_map[chat_key]
                users_to_remove = [
                    user_id
                    for user_id in list(chat_cooldowns.keys())
                    if user_id not in attention_set
                ]
                for user_id in users_to_remove:
                    user_info = chat_cooldowns[user_id]
                    user_name = user_info.get("user_name", "未知")
                    del chat_cooldowns[user_id]
                    removed_users.append(user_id)
                    logger.info(
                        f"[注意力冷却] 用户 {user_name}(ID:{user_id}) 已从注意力冷却列表移除，"
                        f"原因：与关注列表同步（用户不在关注列表中）"
                    )
                if not chat_cooldowns:
                    del CooldownManager._cooldown_map[chat_key]

            if chat_key in CooldownManager._pending_cooldown_map:
                chat_pending = CooldownManager._pending_cooldown_map[chat_key]
                for user_id in list(chat_pending.keys()):
                    if user_id not in attention_set:
                        del chat_pending[user_id]
                        pending_removed_users.append(user_id)
                if not chat_pending:
                    del CooldownManager._pending_cooldown_map[chat_key]

            # 如果有用户被移除则强制保存
            if removed_users or pending_removed_users:
                CooldownManager._save_to_disk(force=True)

        return removed_users + pending_removed_users

    @staticmethod
    async def sync_with_attention_map(
        chat_key: str, attention_map: Optional[Dict[str, Any]]
    ) -> List[str]:
        """与当前注意力追踪表同步，移除不再被追踪的正式/候选冷却用户。"""
        attention_user_ids = list((attention_map or {}).keys())
        return await CooldownManager.sync_with_attention_list(
            chat_key, attention_user_ids
        )

    @staticmethod
    async def on_attention_user_removed(chat_key: str, user_id: str) -> bool:
        """
        当用户从关注列表移除时的回调

        参数：
            chat_key: 会话唯一标识符
            user_id: 从关注列表移除的用户ID

        返回：
            该用户是否也从注意力冷却列表中移除
        """
        async with CooldownManager._lock:
            if chat_key not in CooldownManager._cooldown_map:
                return False

            chat_cooldowns = CooldownManager._cooldown_map[chat_key]

            if user_id not in chat_cooldowns:
                return False

            # 获取用户信息用于日志
            user_info = chat_cooldowns[user_id]
            user_name = user_info.get("user_name", "未知")

            # 从注意力冷却列表移除
            del chat_cooldowns[user_id]

            # 清理空会话
            if not chat_cooldowns:
                del CooldownManager._cooldown_map[chat_key]

            logger.info(
                f"[注意力冷却] 用户 {user_name}(ID:{user_id}) 已从注意力冷却列表移除，"
                f"原因：关注用户已移除"
            )

            # 强制保存
            CooldownManager._save_to_disk(force=True)

            return True

    @staticmethod
    async def clear_session_cooldown(chat_key: str) -> int:
        """
        清除特定会话的所有注意力冷却数据

        参数：
            chat_key: 会话唯一标识符

        返回：
            被清除的用户数量
        """
        async with CooldownManager._lock:
            cleared_count = 0
            pending_cleared_count = 0
            if chat_key in CooldownManager._cooldown_map:
                cleared_count = len(CooldownManager._cooldown_map[chat_key])
                del CooldownManager._cooldown_map[chat_key]
            if chat_key in CooldownManager._pending_cooldown_map:
                pending_cleared_count = len(
                    CooldownManager._pending_cooldown_map[chat_key]
                )
                del CooldownManager._pending_cooldown_map[chat_key]

            total = cleared_count + pending_cleared_count
            if total > 0:
                logger.info(
                    f"[注意力冷却] 会话 {chat_key} 的冷却数据已清除，"
                    f"移除了 {cleared_count} 个正式冷却用户，{pending_cleared_count} 个候选冷却用户"
                )

            # 强制保存
            CooldownManager._save_to_disk(force=True)

            return total

    @staticmethod
    async def clear_all_cooldown() -> int:
        """
        清除所有会话的所有注意力冷却数据

        返回：
            被清除的用户总数
        """
        async with CooldownManager._lock:
            total_cleared = 0
            total_pending_cleared = 0
            for chat_key, chat_cooldowns in CooldownManager._cooldown_map.items():
                total_cleared += len(chat_cooldowns)
            for chat_key, chat_pending in CooldownManager._pending_cooldown_map.items():
                total_pending_cleared += len(chat_pending)

            CooldownManager._cooldown_map = {}
            CooldownManager._pending_cooldown_map = {}

            logger.info(
                f"[注意力冷却] 所有注意力冷却数据已清除，共移除了 {total_cleared} 个正式冷却用户，"
                f"{total_pending_cleared} 个候选冷却用户"
            )

            # 强制保存
            CooldownManager._save_to_disk(force=True)

            return total_cleared + total_pending_cleared

    @staticmethod
    def _validate_user_for_release(
        chat_key: str, user_id: str, attention_user_ids: Optional[List[str]] = None
    ) -> tuple[bool, str]:
        """
        验证用户是否符合注意力冷却解除条件。

        验证内容：
        - 用户在注意力冷却列表中（需求 3.2
        - User了关注列表，用户在关注列表中（需求 3.3）
        - 用户ID与消息发送者匹配（需求 3.1）

        参数：
            chat_key: 会话唯一标识符
            user_id: 要验证的用户ID
            attention_user_ids: 可选的关注列表用户ID列表

        返回：
            元组 (是否有效, 无效原因)
        """
        # 检查用户是否在注意力冷却列表中（需求 3.2）
        if chat_key not in CooldownManager._cooldown_map:
            return False, "会话不在注意力冷却中"

        chat_cooldowns = CooldownManager._cooldown_map[chat_key]

        if user_id not in chat_cooldowns:
            return False, "用户不在注意力冷却中"

        # 检查用户是否在关注列表中（需求 3.3）
        if attention_user_ids is not None:
            if user_id not in attention_user_ids:
                return False, "用户不在关注列表中"

        return True, ""

    @staticmethod
    async def try_release_cooldown_on_reply(
        chat_key: str,
        user_id: str,
        trigger_type: str,
        attention_user_ids: Optional[List[str]] = None,
    ) -> bool:
        """
        当AI回复用户时尝试解除注意力冷却。

        解除条件（需求 2.1, 2.2）
        - 用户通过关键词或@提及触发AI回复（trigger_type="keyword" 或 "at"）
        - 用户的消息通过概率检查且决策AI决定回复（trigger_type="normal"）

        参数：
            chat_key: 会话唯一标识符
            user_id: 触发回复的用户ID
            trigger_type: 触发类型 - "keyword"、"at" 或 "normal"
            attention_user_ids: 可选的关注列表用户ID列表用于验证

        返回：
            注意力冷却是否成功解除
        """
        if await CooldownManager.is_in_pending_cooldown(chat_key, user_id):
            await CooldownManager.clear_pending_cooldown(
                chat_key, user_id, reason=f"reply_trigger:{trigger_type}"
            )
            return True

        async with CooldownManager._lock:
            # 验证用户是否符合解除条件（需求 3.1, 3.2, 3.3
            is_valid, reason = CooldownManager._validate_user_for_release(
                chat_key, user_id, attention_user_ids
            )

            if not is_valid:
                if DEBUG_MODE:
                    logger.info(f"[注意力冷却] 跳过解除用户 {user_id}：{reason}")
                return False

            # 获取用户信息用于日志
            chat_cooldowns = CooldownManager._cooldown_map[chat_key]
            user_info = chat_cooldowns[user_id]
            user_name = user_info.get("user_name", "未知")
            cooldown_start = user_info.get("cooldown_start", 0)
            duration = time.time() - cooldown_start

            # 从注意力冷却列表移除
            del chat_cooldowns[user_id]

            # 清理空会话
            if not chat_cooldowns:
                del CooldownManager._cooldown_map[chat_key]

            logger.info(
                f"[注意力冷却] 用户 {user_name}(ID:{user_id}) 已解除注意力冷却，"
                f"触发类型：{trigger_type}，持续时间：{duration:.1f}秒"
            )

            # 强制保存（状态变更）
            CooldownManager._save_to_disk(force=True)

            return True
