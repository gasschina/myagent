"""
chatbot/manager.py - 聊天平台管理器
=====================================
统一管理所有聊天平台的生命周期和消息路由。
纯 Python 实现，不依赖 Node.js 或额外网关。
"""
from __future__ import annotations

import asyncio
from typing import Any, Callable, Dict, List, Optional

from core.logger import get_logger
from chatbot.base import BaseChatBot, ChatMessage, ChatResponse
from config import ChatPlatformConfig

logger = get_logger("myagent.chatbot")


class ChatBotManager:
    """
    聊天平台管理器。

    功能:
      - 统一管理多个聊天平台
      - 消息路由到主 Agent
      - 多用户/多会话隔离
      - 平台独立的生命周期管理
      - 后台异步运行

    使用示例:
        manager = ChatBotManager()
        manager.setup_platforms(config.chat_platforms, message_handler=handle_message)
        await manager.start_all()
    """

    def __init__(self):
        self._bots: Dict[str, BaseChatBot] = {}
        self._session_map: Dict[str, str] = {}  # session_id -> last_message

    def setup_platforms(
        self,
        platform_configs: List[ChatPlatformConfig],
        message_handler: Callable,
    ):
        """
        初始化所有聊天平台。

        Args:
            platform_configs: 平台配置列表
            message_handler: 统一消息处理回调
        """
        for cfg in platform_configs:
            if not cfg.enabled:
                continue
            try:
                bot = self._create_bot(cfg, message_handler)
                if bot:
                    self._bots[cfg.platform] = bot
                    logger.info(f"聊天平台已配置: {cfg.platform}")
            except Exception as e:
                logger.error(f"平台 {cfg.platform} 初始化失败: {e}")

    def _create_bot(
        self,
        config: ChatPlatformConfig,
        message_handler: Callable,
    ) -> Optional[BaseChatBot]:
        """根据配置创建对应的聊天机器人实例"""
        platform = config.platform

        if platform == "telegram":
            from chatbot.telegram_bot import TelegramBot
            return TelegramBot(
                token=config.token,
                allowed_users=config.allowed_users,
                message_handler=message_handler,
                **config.extra,
            )

        elif platform == "discord":
            from chatbot.discord_bot import DiscordBot
            return DiscordBot(
                token=config.token,
                allowed_users=config.allowed_users,
                message_handler=message_handler,
                **config.extra,
            )

        elif platform == "feishu":
            from chatbot.feishu_bot import FeishuBot
            return FeishuBot(
                token=config.token,
                app_id=config.app_id,
                app_secret=config.app_secret,
                allowed_users=config.allowed_users,
                message_handler=message_handler,
                **config.extra,
            )

        elif platform == "qq":
            from chatbot.qq_bot import QQBot
            return QQBot(
                token=config.token,
                allowed_users=config.allowed_users,
                message_handler=message_handler,
                **config.extra,
            )

        elif platform == "wechat":
            from chatbot.wechat_bot import WeChatBot
            return WeChatBot(
                token=config.token,
                allowed_users=config.allowed_users,
                message_handler=message_handler,
                **config.extra,
            )

        else:
            logger.warning(f"不支持的平台: {platform}")
            return None

    async def start_all(self):
        """启动所有聊天平台"""
        tasks = []
        for name, bot in self._bots.items():
            logger.info(f"启动聊天平台: {name}")
            task = asyncio.create_task(self._run_bot(name, bot))
            tasks.append(task)
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _run_bot(self, name: str, bot: BaseChatBot):
        """安全运行单个聊天平台"""
        try:
            await bot.start()
        except Exception as e:
            logger.error(f"聊天平台 {name} 运行异常: {e}")

    async def stop_all(self):
        """停止所有聊天平台"""
        for name, bot in self._bots.items():
            try:
                await bot.stop()
            except Exception as e:
                logger.error(f"停止 {name} 失败: {e}")
        logger.info("所有聊天平台已停止")

    async def send_to_all(self, text: str):
        """向所有平台广播消息"""
        for bot in self._bots.values():
            try:
                await bot.send_message(ChatResponse(text=text))
            except Exception as e:
                logger.error(f"广播失败: {e}")

    def get_active_platforms(self) -> List[str]:
        """获取活跃平台列表"""
        return list(self._bots.keys())

    def get_stats(self) -> Dict[str, Any]:
        """获取状态"""
        return {
            "active_platforms": list(self._bots.keys()),
            "platform_count": len(self._bots),
        }
