"""
chatbot/wechat_bot.py - 微信机器人
=====================================
微信机器人适配器。

注意: 微信没有官方的第三方机器人 API。
本实现支持:
  1. 通过 wechaty / wechaty-puppet 的 HTTP 桥接
  2. 通过 ComWeChatRobotClient (仅 Windows)
  3. 通过 WxPusher (微信公众号消息推送)

推荐方案: 使用 WxPusher 接收公众号消息，或使用 HTTP 桥接。
"""
from __future__ import annotations

import asyncio
import json
from typing import Optional

from chatbot.base import BaseChatBot, ChatMessage, ChatResponse

try:
    import aiohttp
    HAS_AIOHTTP = True
except ImportError:
    HAS_AIOHTTP = False


class WeChatBot(BaseChatBot):
    """
    微信机器人适配器。

    配置要求(根据模式不同):
      - WxPusher 模式:
        - app_id: WxPusher 应用 ID
        - app_secret: WxPusher 应用密钥
      - HTTP 桥接模式:
        - token: HTTP API 地址
        - extra.wx_callback_url: 回调地址
    """

    platform_name = "wechat"

    def __init__(self, app_id: str = "", app_secret: str = "", **kwargs):
        super().__init__(**kwargs)
        self.app_id = app_id
        self.app_secret = app_secret
        self._session = None
        self._mode = "wxpusher" if app_id else "http_bridge"

    async def start(self):
        """启动微信机器人"""
        if not HAS_AIOHTTP:
            self.logger.error("请安装 aiohttp: pip install aiohttp")
            return

        self._session = aiohttp.ClientSession()
        self._running = True

        if self._mode == "wxpusher" and self.app_id:
            self.logger.info("微信机器人(WxPusher 模式)已启动")
        elif self._mode == "http_bridge" and self.token:
            self.logger.info(f"微信机器人(HTTP 桥接模式)已启动: {self.token}")
        else:
            self.logger.warning("微信机器人配置不完整，将使用最小功能模式")
            self.logger.info(
                "提示: 微信接入建议使用 WxPusher 或 HTTP 桥接。"
                "详见 README。"
            )

        while self._running:
            await asyncio.sleep(1)

    async def stop(self):
        """停止微信机器人"""
        self._running = False
        if self._session:
            await self._session.close()
        self.logger.info("微信机器人已停止")

    async def send_message(self, response: ChatResponse) -> bool:
        """发送消息到微信"""
        if not self._session:
            return False

        try:
            if self._mode == "wxpusher":
                return await self._send_wxpusher(response)
            elif self._mode == "http_bridge":
                return await self._send_http_bridge(response)
        except Exception as e:
            self.logger.error(f"微信发送消息异常: {e}")
            return False

    async def _send_wxpusher(self, response: ChatResponse) -> bool:
        """通过 WxPusher 发送"""
        if not self.app_id:
            return False

        url = "https://wxpusher.zjiecode.com/api/send/message"
        payload = {
            "appToken": self.app_id,
            "content": response.text[:5000],
            "contentType": 1,  # 文本
            "uids": [response.user_id] if response.user_id else [],
        }

        async with self._session.post(url, json=payload) as resp:
            result = await resp.json()
            if result.get("code") == 1000:
                return True
            self.logger.error(f"WxPusher 发送失败: {result}")
            return False

    async def _send_http_bridge(self, response: ChatResponse) -> bool:
        """通过 HTTP 桥接发送"""
        if not self.token:
            return False

        url = f"{self.token.rstrip('/')}/send"
        payload = {
            "to": response.user_id or response.chat_id,
            "message": response.text,
        }

        async with self._session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            return resp.status == 200
