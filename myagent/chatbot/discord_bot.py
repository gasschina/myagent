"""
chatbot/discord_bot.py - Discord 机器人
=========================================
使用 discord.py 库接入 Discord。
纯 Python 异步实现。
"""
from __future__ import annotations

import asyncio
from typing import Optional, List

from chatbot.base import BaseChatBot, ChatMessage, ChatResponse

try:
    import discord
    HAS_DISCORD = True
except ImportError:
    HAS_DISCORD = False


class DiscordBot(BaseChatBot):
    """
    Discord 机器人适配器。

    配置要求:
      - token: Bot Token (从 Discord Developer Portal 获取)
    """

    platform_name = "discord"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._client: Optional[discord.Client] = None
        self._intents = discord.Intents.default()
        self._intents.message_content = True
        self._intents.members = True

    async def start(self):
        """启动 Discord 机器人"""
        if not HAS_DISCORD:
            self.logger.error("请安装 discord.py: pip install discord.py")
            return

        if not self.token:
            self.logger.error("Discord Bot Token 未配置")
            return

        self._client = discord.Client(intents=self._intents)

        @self._client.event
        async def on_ready():
            self.logger.info(f"Discord 机器人已登录: {self._client.user}")
            await self._client.change_presence(
                activity=discord.Activity(
                    type=discord.ActivityType.listening,
                    name="MyAgent"
                )
            )

        @self._client.event
        async def on_message(message: discord.Message):
            # 忽略自己的消息
            if message.author == self._client.user:
                return

            # 只处理 Bot 被提及或私信
            is_dm = isinstance(message.channel, discord.DMChannel)
            is_mentioned = self._client.user.mentioned_in(message)
            has_prefix = message.content.startswith("!")

            if not (is_dm or is_mentioned or has_prefix):
                return

            # 去除 @提及 和前缀
            text = message.content
            text = text.replace(f"<@{self._client.user.id}>", "").strip()
            text = text.replace(f"<@!{self._client.user.id}>", "").strip()
            if text.startswith("!"):
                text = text[1:].strip()

            if not text:
                return

            chat_message = ChatMessage(
                platform=self.platform_name,
                chat_id=str(message.channel.id),
                user_id=str(message.author.id),
                username=message.author.display_name,
                text=text,
                is_group=not is_dm,
                reply_to=str(message.reference.message_id) if message.reference else "",
                raw_data={
                    "guild_id": str(message.guild.id) if message.guild else "",
                    "channel_id": str(message.channel.id),
                },
            )
            await self._handle_message(chat_message)

        self._running = True
        try:
            await self._client.start(self.token)
        except Exception as e:
            self.logger.error(f"Discord 机器人启动失败: {e}")
            self._running = False

    async def stop(self):
        """停止 Discord 机器人"""
        self._running = False
        if self._client:
            await self._client.close()
        self.logger.info("Discord 机器人已停止")

    async def send_message(self, response: ChatResponse) -> bool:
        """发送消息到 Discord"""
        if not self._client or not self._client.is_ready() or not response.chat_id:
            return False

        try:
            channel = self._client.get_channel(int(response.chat_id))
            if not channel:
                channel = self._client.get_user(int(response.chat_id))

            if not channel:
                self.logger.error(f"无法找到频道/用户: {response.chat_id}")
                return False

            # Discord 消息长度限制 2000
            text = response.text
            if len(text) > 2000:
                # 分段发送
                chunks = [text[i:i+2000] for i in range(0, len(text), 2000)]
                for chunk in chunks:
                    if isinstance(channel, discord.abc.Messageable):
                        await channel.send(chunk)
                    else:
                        await channel.send(chunk)
            else:
                if isinstance(channel, discord.abc.Messageable):
                    await channel.send(text)
                else:
                    await channel.send(text)

            return True
        except Exception as e:
            self.logger.error(f"发送消息失败: {e}")
            return False
