"""
chatbot/qq_bot.py - QQ 机器人
===============================
QQ 机器人适配器。

注意: QQ 官方机器人 API 有严格限制，本实现支持:
  1. QQ 官方机器人 (go-cqhttp / Lagrange / LLOneBot 等 OneBot 协议)
  2. 纯 HTTP/Websocket 连接，无需 Node.js

配置方式:
  - 通过 OneBot v11 HTTP API 连接
  - 需要先运行 OneBot 实现 (如 Lagrange.Core)
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


class QQBot(BaseChatBot):
    """
    QQ 机器人适配器 (OneBot v11 协议)。

    配置要求:
      - token: OneBot HTTP API 地址 (如 http://127.0.0.1:3000)
      - extra.ws_url: OneBot WebSocket 地址 (如 ws://127.0.0.1:3001)
      - extra.self_id: 机器人 QQ 号

    需要预先运行 OneBot 实现 (推荐 Lagrange.Core):
      https://github.com/LagrangeDev/Lagrange.Core
    """

    platform_name = "qq"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        # token 字段存储 OneBot HTTP API 地址
        self.api_url = self.token.rstrip("/") if self.token else ""
        self.ws_url = self.config.get("ws_url", "")
        self.self_id = self.config.get("self_id", "")
        self._session = None

    async def start(self):
        """启动 QQ 机器人"""
        if not HAS_AIOHTTP:
            self.logger.error("请安装 aiohttp: pip install aiohttp")
            return

        if not self.api_url:
            self.logger.error("QQ OneBot API 地址未配置")
            return

        self._session = aiohttp.ClientSession()
        self._running = True

        # 测试连接
        try:
            async with self._session.get(f"{self.api_url}/get_login_info",
                                          timeout=aiohttp.ClientTimeout(total=5)) as resp:
                info = await resp.json()
                if info.get("status") == "ok":
                    self.self_id = str(info["data"].get("user_id", ""))
                    self.logger.info(f"QQ 机器人已连接: {self.self_id}")
                else:
                    self.logger.warning(f"QQ 连接测试失败: {info}")
        except Exception as e:
            self.logger.warning(f"无法连接 OneBot API ({self.api_url}): {e}")

        # 启动 WebSocket 监听
        if self.ws_url:
            asyncio.create_task(self._ws_listen())
        else:
            self.logger.info("QQ 机器人使用 HTTP API 模式(仅发送)")
            while self._running:
                await asyncio.sleep(1)

    async def stop(self):
        """停止 QQ 机器人"""
        self._running = False
        if self._session:
            await self._session.close()
        self.logger.info("QQ 机器人已停止")

    async def send_message(self, response: ChatResponse) -> bool:
        """发送消息到 QQ"""
        if not self._session or not self.api_url:
            return False

        try:
            if response.is_group:
                # 群消息
                url = f"{self.api_url}/send_group_msg"
                payload = {
                    "group_id": int(response.chat_id),
                    "message": response.text[:4000],
                }
            else:
                # 私聊
                url = f"{self.api_url}/send_private_msg"
                payload = {
                    "user_id": int(response.user_id or response.chat_id),
                    "message": response.text[:4000],
                }

            if response.reply_to:
                payload["message"] = f"[CQ:reply,id={response.reply_to}]{payload['message']}"

            async with self._session.post(url, json=payload) as resp:
                result = await resp.json()
                if result.get("status") == "ok":
                    return True
                self.logger.error(f"QQ 发送失败: {result}")
                return False
        except Exception as e:
            self.logger.error(f"QQ 发送消息异常: {e}")
            return False

    async def _ws_listen(self):
        """WebSocket 监听消息"""
        if not self.ws_url:
            return

        while self._running:
            try:
                async with self._session.ws_connect(self.ws_url) as ws:
                    self.logger.info(f"QQ WebSocket 已连接: {self.ws_url}")

                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            data = json.loads(msg.data)

                            # OneBot v11 事件格式
                            if data.get("post_type") == "message":
                                await self._handle_event(data)
                        elif msg.type in (
                            aiohttp.WSMsgType.ERROR,
                            aiohttp.WSMsgType.CLOSED,
                        ):
                            break

            except Exception as e:
                self.logger.error(f"QQ WebSocket 异常: {e}")
                if self._running:
                    await asyncio.sleep(5)

    async def _handle_event(self, data: dict):
        """处理 OneBot 事件"""
        user_id = str(data.get("user_id", ""))
        raw_msg = data.get("raw_message", data.get("message", ""))

        # 去除 CQ 码
        import re
        clean_msg = re.sub(r'\[CQ:[^\]]+\]', '', raw_msg).strip()

        if not clean_msg:
            return

        # 只处理 @机器人 或私信
        is_group = data.get("message_type") == "group"
        if is_group:
            group_id = str(data.get("group_id", ""))
            # 检查是否被 @
            at_pattern = f"[CQ:at,qq={self.self_id}]"
            if at_pattern not in data.get("message", ""):
                return  # 群聊中未被 @，忽略
            chat_id = group_id
        else:
            chat_id = user_id

        message = ChatMessage(
            platform=self.platform_name,
            chat_id=chat_id,
            user_id=user_id,
            username=data.get("sender", {}).get("nickname", ""),
            text=clean_msg,
            is_group=is_group,
            reply_to=str(data.get("message_id", "")),
            raw_data=data,
        )
        await self._handle_message(message)
