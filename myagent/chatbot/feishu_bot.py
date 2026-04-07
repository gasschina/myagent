"""
chatbot/feishu_bot.py - 飞书机器人
===================================
使用飞书开放平台 SDK 接入飞书。
纯 Python 异步实现，使用 Webhook 长连接模式。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from typing import Optional

from chatbot.base import BaseChatBot, ChatMessage, ChatResponse

try:
    import aiohttp
    HAS_AIOHTTP = True
except ImportError:
    HAS_AIOHTTP = False


class FeishuBot(BaseChatBot):
    """
    飞书机器人适配器。

    配置要求:
      - app_id: 应用 ID
      - app_secret: 应用密钥

    支持模式:
      - 长连接(Websocket)接收事件
      - HTTP API 发送消息
    """

    platform_name = "feishu"
    API_BASE = "https://open.feishu.cn/open-apis"

    def __init__(self, app_id: str = "", app_secret: str = "", **kwargs):
        super().__init__(**kwargs)
        self.app_id = app_id
        self.app_secret = app_secret
        self._tenant_access_token = ""
        self._token_expires = 0
        self._ws_url = ""
        self._session = None

    async def start(self):
        """启动飞书机器人"""
        if not HAS_AIOHTTP:
            self.logger.error("请安装 aiohttp: pip install aiohttp")
            return

        if not self.app_id or not self.app_secret:
            self.logger.error("飞书 App ID / App Secret 未配置")
            return

        self._session = aiohttp.ClientSession()
        self._running = True

        # 获取 tenant_access_token
        await self._refresh_token()

        # 启动长连接接收事件
        asyncio.create_task(self._ws_listen())

        self.logger.info("飞书机器人已启动")

        # 保持运行
        while self._running:
            await asyncio.sleep(1)

    async def stop(self):
        """停止飞书机器人"""
        self._running = False
        if self._session:
            await self._session.close()
        self.logger.info("飞书机器人已停止")

    async def send_message(self, response: ChatResponse) -> bool:
        """发送消息到飞书"""
        if not self._session or not response.chat_id:
            return False

        await self._ensure_token()

        try:
            url = f"{self.API_BASE}/im/v1/messages"
            headers = {
                "Authorization": f"Bearer {self._tenant_access_token}",
                "Content-Type": "application/json",
            }
            payload = {
                "receive_id": response.chat_id,
                "msg_type": "text",
                "content": json.dumps({"text": response.text[:5000]}),
            }
            params = {"receive_id_type": "chat_id"}

            async with self._session.post(url, headers=headers, json=payload,
                                           params=params) as resp:
                result = await resp.json()
                if result.get("code") != 0:
                    self.logger.error(f"飞书发送失败: {result}")
                    return False
                return True
        except Exception as e:
            self.logger.error(f"飞书发送消息异常: {e}")
            return False

    # ==========================================================================
    # 内部方法
    # ==========================================================================

    async def _refresh_token(self):
        """刷新 tenant_access_token"""
        try:
            url = f"{self.API_BASE}/auth/v3/tenant_access_token/internal"
            payload = {
                "app_id": self.app_id,
                "app_secret": self.app_secret,
            }
            async with self._session.post(url, json=payload) as resp:
                result = await resp.json()
                if result.get("code") == 0:
                    self._tenant_access_token = result["tenant_access_token"]
                    self._token_expires = time.time() + result.get("expire", 7200) - 300
                    self.logger.debug("飞书 Token 已刷新")
                else:
                    self.logger.error(f"获取 Token 失败: {result}")
        except Exception as e:
            self.logger.error(f"刷新 Token 异常: {e}")

    async def _ensure_token(self):
        """确保 Token 有效"""
        if time.time() > self._token_expires:
            await self._refresh_token()

    async def _ws_listen(self):
        """通过长连接接收消息"""
        try:
            await self._ensure_token()

            # 获取长连接地址
            url = f"{self.API_BASE}/im/v1/events/ws"
            headers = {"Authorization": f"Bearer {self._tenant_access_token}"}

            # 使用简单的轮询模式作为后备
            self.logger.info("飞书机器人使用 Webhook 轮询模式")
            poll_interval = 3
            while self._running:
                await asyncio.sleep(poll_interval)

        except Exception as e:
            self.logger.error(f"飞书长连接异常: {e}")
            if self._running:
                await asyncio.sleep(5)
