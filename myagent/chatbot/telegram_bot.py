"""
chatbot/telegram_bot.py - Telegram 机器人
===========================================
使用 python-telegram-bot 库接入 Telegram。
纯 Python 实现，异步运行。
"""
from __future__ import annotations

import asyncio
from typing import Optional, List

from chatbot.base import BaseChatBot, ChatMessage, ChatResponse

try:
    from telegram import Update
    from telegram.ext import (
        Application, CommandHandler, MessageHandler,
        filters, ContextTypes,
    )
    HAS_TELEGRAM = True
except ImportError:
    HAS_TELEGRAM = False


class TelegramBot(BaseChatBot):
    """
    Telegram 机器人适配器。

    配置要求:
      - token: Bot Token (从 @BotFather 获取)
    """

    platform_name = "telegram"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._app: Optional[Application] = None

    async def start(self):
        """启动 Telegram 机器人"""
        if not HAS_TELEGRAM:
            self.logger.error("请安装 python-telegram-bot: pip install python-telegram-bot")
            return

        if not self.token:
            self.logger.error("Telegram Bot Token 未配置")
            return

        # 创建 Application
        self._app = Application.builder().token(self.token).build()

        # 注册处理器
        self._app.add_handler(CommandHandler("start", self._cmd_start))
        self._app.add_handler(CommandHandler("help", self._cmd_help))
        self._app.add_handler(CommandHandler("status", self._cmd_status))
        self._app.add_handler(CommandHandler("clear", self._cmd_clear))
        self._app.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self._on_message)
        )

        self._running = True

        # 启动轮询
        await self._app.initialize()
        await self._app.start()
        await self._app.updater.start_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True,
        )

        self.logger.info("Telegram 机器人已启动")

        # 保持运行
        while self._running:
            await asyncio.sleep(1)

    async def stop(self):
        """停止 Telegram 机器人"""
        self._running = False
        if self._app:
            await self._app.updater.stop()
            await self._app.stop()
            await self._app.shutdown()
        self.logger.info("Telegram 机器人已停止")

    async def send_message(self, response: ChatResponse) -> bool:
        """发送消息到 Telegram"""
        if not self._app or not response.chat_id:
            return False

        try:
            kwargs = {
                "chat_id": int(response.chat_id) if response.chat_id.isdigit() else response.chat_id,
                "text": response.text[:4096],  # Telegram 消息限制
            }
            if response.parse_mode:
                kwargs["parse_mode"] = response.parse_mode
            if response.reply_to:
                kwargs["reply_to_message_id"] = int(response.reply_to)

            await self._app.bot.send_message(**kwargs)
            return True
        except Exception as e:
            self.logger.error(f"发送消息失败: {e}")
            return False

    # ==========================================================================
    # 命令处理器
    # ==========================================================================

    async def _cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """处理 /start 命令"""
        await update.message.reply_text(
            "👋 你好！我是 MyAgent，你的本地AI助手。\n\n"
            "直接发送消息即可与我对话。\n"
            "我可以帮你执行代码、操作文件、搜索信息等。\n\n"
            "命令列表:\n"
            "/help - 查看帮助\n"
            "/status - 查看状态\n"
            "/clear - 清除对话历史"
        )

    async def _cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """处理 /help 命令"""
        await update.message.reply_text(
            "🤖 MyAgent 帮助\n\n"
            "我能做什么:\n"
            "• 执行 Python/Shell/PowerShell 代码\n"
            "• 读写搜索文件\n"
            "• 搜索互联网\n"
            "• 查询系统信息\n"
            "• 浏览器自动化\n"
            "• 记住你的偏好和经验\n\n"
            "使用方式:\n"
            "直接用自然语言描述你的需求即可。"
        )

    async def _cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """处理 /status 命令"""
        await update.message.reply_text("✅ MyAgent 正在运行中...")

    async def _cmd_clear(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """处理 /clear 命令"""
        await self._handle_message(ChatMessage(
            platform=self.platform_name,
            chat_id=str(update.effective_chat.id),
            user_id=str(update.effective_user.id),
            username=update.effective_user.username or update.effective_user.first_name or "",
            text="__cmd_clear__",
            is_group=update.effective_chat.type != "private",
            raw_data={"command": "clear"},
        ))
        await update.message.reply_text("🗑️ 对话历史已清除。")

    async def _on_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """处理普通消息"""
        message = ChatMessage(
            platform=self.platform_name,
            chat_id=str(update.effective_chat.id),
            user_id=str(update.effective_user.id),
            username=update.effective_user.username or update.effective_user.first_name or "",
            text=update.message.text or "",
            is_group=update.effective_chat.type != "private",
            reply_to=str(update.message.reply_to_message.message_id) if update.message.reply_to_message else "",
            raw_data={"update": str(update)},
        )
        await self._handle_message(message)
