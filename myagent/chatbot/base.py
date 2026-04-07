"""
chatbot/base.py - 聊天平台基类
================================
定义所有聊天平台的统一接口。
每个平台必须继承此类并实现 send/receive 方法。
"""
from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from core.logger import get_logger
from core.utils import generate_id, timestamp


@dataclass
class ChatMessage:
    """统一聊天消息格式"""
    id: str = field(default_factory=lambda: generate_id("msg"))
    platform: str = ""               # 来源平台: telegram/discord/feishu/qq/wechat
    chat_id: str = ""                # 聊天/群组 ID
    user_id: str = ""                # 用户 ID
    username: str = ""               # 用户名/昵称
    text: str = ""                   # 消息文本
    is_group: bool = False           # 是否群聊
    reply_to: str = ""               # 回复的消息 ID
    raw_data: Dict[str, Any] = field(default_factory=dict)  # 原始平台数据
    timestamp: str = field(default_factory=timestamp)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "platform": self.platform,
            "chat_id": self.chat_id,
            "user_id": self.user_id,
            "username": self.username,
            "text": self.text,
            "is_group": self.is_group,
            "timestamp": self.timestamp,
        }


@dataclass
class ChatResponse:
    """聊天回复"""
    chat_id: str = ""
    user_id: str = ""
    text: str = ""
    parse_mode: str = ""             # markdown | html | ""
    reply_to: str = ""               # 回复的消息 ID
    files: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)


class BaseChatBot(ABC):
    """
    聊天平台基类。

    所有聊天平台适配器必须继承此类。
    """

    platform_name: str = "base"
    enabled: bool = False

    def __init__(
        self,
        token: str = "",
        allowed_users: Optional[List[str]] = None,
        message_handler: Optional[Callable] = None,
        **config,
    ):
        self.token = token
        self.allowed_users = set(allowed_users or [])
        self.message_handler = message_handler
        self.config = config
        self._running = False
        self.logger = get_logger(f"myagent.chatbot.{self.platform_name}")

    @abstractmethod
    async def start(self):
        """启动聊天机器人"""
        pass

    @abstractmethod
    async def stop(self):
        """停止聊天机器人"""
        pass

    @abstractmethod
    async def send_message(self, response: ChatResponse) -> bool:
        """发送消息"""
        pass

    def is_user_allowed(self, user_id: str) -> bool:
        """检查用户是否在白名单中"""
        if not self.allowed_users:
            return True  # 空白名单 = 允许所有
        return user_id in self.allowed_users

    def _generate_session_id(self, message: ChatMessage) -> str:
        """为消息生成会话 ID(支持多用户/多会话隔离)"""
        if message.is_group:
            return f"{self.platform_name}_group_{message.chat_id}"
        return f"{self.platform_name}_dm_{message.user_id}"

    async def _handle_message(self, message: ChatMessage):
        """处理收到的消息(统一入口)"""
        if not self.is_user_allowed(message.user_id):
            self.logger.warning(f"用户被拒绝: {message.username} ({message.user_id})")
            return

        self.logger.info(
            f"[{self.platform_name}] 收到消息: "
            f"{message.username}: {message.text[:100]}"
        )

        if self.message_handler:
            try:
                await self.message_handler(message, self)
            except Exception as e:
                self.logger.error(f"消息处理失败: {e}")
