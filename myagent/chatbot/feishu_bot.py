"""
chatbot/feishu_bot.py - 飞书机器人
===================================
使用飞书开放平台 SDK 接入飞书。
纯 Python 异步实现，支持:
  - WebSocket 长连接模式接收事件（推荐）
  - HTTP 回调模式接收事件（备用，需公网地址）
  - HTTP API 发送消息
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from typing import Optional, Dict, Any

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
      - ws: WebSocket 长连接（默认，推荐，无需公网地址）
      - webhook: HTTP 回调（需要公网地址和 verify_key）
      - polling: 轮询模式（兜底）

    额外配置 (kwargs):
      - receive_mode: "ws" | "webhook" | "polling"，默认 "ws"
      - verification_token: 事件订阅验证令牌（webhook 模式需要）
      - encrypt_key: 事件加密密钥（可选）
    """

    platform_name = "feishu"
    API_BASE = "https://open.feishu.cn/open-apis"

    def __init__(self, app_id: str = "", app_secret: str = "", **kwargs):
        super().__init__(**kwargs)
        self.app_id = app_id
        self.app_secret = app_secret
        self._tenant_access_token = ""
        self._token_expires = 0
        self._session: Optional[aiohttp.ClientSession] = None
        self._ws_session: Optional[aiohttp.ClientWebSocketResponse] = None
        self._receive_mode = kwargs.get("receive_mode", "ws")
        self._verification_token = kwargs.get("verification_token", "")
        self._encrypt_key = kwargs.get("encrypt_key", "")
        self._process_tasks: set = set()
        # 消息去重（飞书可能重复推送）
        self._processed_events: Dict[str, float] = {}
        self._dedup_window = 300  # 5 分钟去重窗口

    async def start(self):
        """启动飞书机器人"""
        if not HAS_AIOHTTP:
            self.logger.error("请安装 aiohttp: pip install aiohttp")
            return

        if not self.app_id or not self.app_secret:
            self.logger.error("飞书 App ID / App Secret 未配置")
            return

        self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
        self._running = True

        # 获取 tenant_access_token
        await self._refresh_token()

        if self._receive_mode == "ws":
            asyncio.create_task(self._ws_listen())
            self.logger.info("飞书机器人已启动 (WebSocket 长连接模式)")
        elif self._receive_mode == "webhook":
            asyncio.create_task(self._webhook_server())
            self.logger.info("飞书机器人已启动 (Webhook 回调模式)")
        else:
            asyncio.create_task(self._poll_listen())
            self.logger.info("飞书机器人已启动 (轮询模式)")

        # 保持运行
        while self._running:
            await asyncio.sleep(1)

    async def stop(self):
        """停止飞书机器人"""
        self._running = False
        # 关闭 WebSocket
        if self._ws_session:
            try:
                await self._ws_session.close()
            except Exception:
                pass
        # 取消所有消息处理任务
        for task in self._process_tasks:
            task.cancel()
        self._process_tasks.clear()
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
                "content": json.dumps({"text": response.text[:5000]}, ensure_ascii=False),
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
    # WebSocket 长连接模式（推荐）
    # ==========================================================================

    async def _ws_listen(self):
        """通过 WebSocket 长连接接收事件"""
        reconnect_delay = 1
        max_reconnect_delay = 30

        while self._running:
            try:
                await self._ensure_token()

                # 获取 WebSocket 长连接地址
                ws_url = await self._get_ws_endpoint()
                if not ws_url:
                    self.logger.error("获取 WebSocket 地址失败，5秒后重试")
                    await asyncio.sleep(5)
                    continue

                self.logger.info(f"正在连接飞书 WebSocket: {ws_url[:80]}...")

                headers = {"Authorization": f"Bearer {self._tenant_access_token}"}
                async with self._session.ws_connect(
                    ws_url,
                    headers=headers,
                    heartbeat=30,
                    max_msg_size=1024 * 1024 * 10,  # 10MB
                ) as ws:
                    self._ws_session = ws
                    reconnect_delay = 1  # 连接成功后重置重连间隔
                    self.logger.info("飞书 WebSocket 已连接")

                    async for msg in ws:
                        if not self._running:
                            break
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            try:
                                data = json.loads(msg.data)
                                await self._handle_ws_event(data)
                            except json.JSONDecodeError as e:
                                self.logger.warning(f"WS 消息解析失败: {e}")
                        elif msg.type in (aiohttp.WSMsgType.CLOSED,
                                          aiohttp.WSMsgType.ERROR):
                            self.logger.warning(f"WebSocket 连接关闭: {msg.type}")
                            break
                        elif msg.type == aiohttp.WSMsgType.PING:
                            await ws.pong()

            except aiohttp.ClientError as e:
                self.logger.error(f"飞书 WebSocket 连接异常: {e}")
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"飞书 WebSocket 未知异常: {e}")

            # 指数退避重连
            self._ws_session = None
            if self._running:
                self.logger.info(f"{reconnect_delay}秒后重连...")
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, max_reconnect_delay)

    async def _get_ws_endpoint(self) -> str:
        """获取 WebSocket 长连接端点地址"""
        try:
            url = f"{self.API_BASE}/im/v1/events/ws"
            headers = {"Authorization": f"Bearer {self._tenant_access_token}"}
            async with self._session.get(url, headers=headers) as resp:
                result = await resp.json()
                if result.get("code") == 0:
                    ws_url = result.get("data", {}).get("url", "")
                    if ws_url:
                        return ws_url
                    self.logger.error(f"WS 响应无 URL: {result}")
                else:
                    self.logger.error(f"获取 WS 端点失败: {result}")
        except Exception as e:
            self.logger.error(f"获取 WS 端点异常: {e}")
        return ""

    async def _handle_ws_event(self, data: dict):
        """处理 WebSocket 事件"""
        # 飞书 WebSocket 推送格式:
        # {"schema": "2.0", "header": {...}, "event": {...}, "url": "..."}
        schema = data.get("schema", "")

        if schema == "2.0":
            # 肯定应答 (ACK) - 必须在收到事件后立即返回
            if "url" in data:
                ack_url = data["url"]
                asyncio.create_task(self._send_ws_ack(ack_url))

            # 解析事件
            event = data.get("event", {})
            header = data.get("header", {})

            # 消息接收事件
            if header.get("event_type") == "im.message.receive_v1":
                event_id = header.get("event_id", "")
                if self._is_duplicate(event_id):
                    self.logger.debug(f"重复事件已忽略: {event_id}")
                    return

                self._mark_processed(event_id)
                asyncio.create_task(self._process_message_event(event))

            # 其他事件类型
            elif header.get("event_type"):
                self.logger.debug(
                    f"收到非消息事件: {header.get('event_type')}"
                )

        elif schema == "3.0":
            # schema 3.0 - 包含 challenge 验证
            header = data.get("header", {})
            event_type = header.get("event_type", "")
            if event_type == "connection":
                # 连接建立确认
                self.logger.info("飞书 WebSocket 连接确认已收到")

    async def _send_ws_ack(self, ack_url: str):
        """发送 WebSocket 事件确认"""
        try:
            async with self._session.post(ack_url) as resp:
                if resp.status == 200:
                    self.logger.debug("WS 事件 ACK 已发送")
                else:
                    text = await resp.text()
                    self.logger.warning(f"WS ACK 失败 ({resp.status}): {text}")
        except Exception as e:
            self.logger.error(f"WS ACK 异常: {e}")

    async def _process_message_event(self, event: dict):
        """处理消息事件，转换为 ChatMessage 并分发"""
        try:
            # 解析飞书消息结构
            sender = event.get("sender", {})
            sender_id = sender.get("sender_id", {})
            user_id = sender_id.get("user_id", "")
            sender_type = sender_id.get("union_id", "")
            chat_id = event.get("message", {}).get("chat_id", "")
            message_id = event.get("message", {}).get("message_id", "")
            msg_type = event.get("message", {}).get("msg_type", "text")
            content_str = event.get("message", {}).get("content", "{}")

            # 解析消息内容
            try:
                content = json.loads(content_str)
                text = content.get("text", "")
            except json.JSONDecodeError:
                text = content_str

            # 只处理文本消息（图片、文件等暂不支持）
            if msg_type not in ("text", "post"):
                if msg_type == "post":
                    # 富文本消息，提取纯文本
                    try:
                        post_content = json.loads(content_str)
                        text = self._extract_post_text(post_content)
                    except Exception:
                        text = "[富文本消息]"
                else:
                    self.logger.debug(f"忽略非文本消息类型: {msg_type}")
                    return

            # 忽略机器人自己的消息
            if user_id == self.app_id:
                return

            # 判断是否群聊
            chat_type = event.get("message", {}).get("chat_type", "p2p")
            is_group = chat_type == "group"

            # 构建统一消息格式
            message = ChatMessage(
                platform=self.platform_name,
                chat_id=chat_id,
                user_id=user_id,
                username=sender_type or user_id,
                text=text.strip(),
                is_group=is_group,
                reply_to=message_id,
                raw_data={"event": event, "msg_type": msg_type},
            )

            # 异步处理消息
            await self._handle_message(message)

        except Exception as e:
            self.logger.error(f"处理飞书消息事件失败: {e}")

    def _extract_post_text(self, post_content: dict) -> str:
        """从飞书富文本消息中提取纯文本"""
        try:
            title = post_content.get("title", "")
            content = post_content.get("content", [])
            texts = []
            for node in content:
                if isinstance(node, list):
                    for item in node:
                        if isinstance(item, dict) and item.get("tag") == "text":
                            texts.append(item.get("text", ""))
                elif isinstance(node, dict) and node.get("tag") == "text":
                    texts.append(node.get("text", ""))
            body = "".join(texts)
            return f"{title}\n{body}" if title else body
        except Exception:
            return "[富文本消息]"

    # ==========================================================================
    # Webhook 回调模式（备用）
    # ==========================================================================

    async def _webhook_server(self):
        """启动简单的 HTTP 服务器接收 webhook 回调"""
        try:
            from aiohttp import web
        except ImportError:
            self.logger.error("Webhook 模式需要 aiohttp: pip install aiohttp")
            return

        app = web.Application()
        app.router.add_post("/feishu/webhook", self._handle_webhook)

        runner = web.AppRunner(app)
        await runner.setup()
        port = self.config.get("webhook_port", 8080)
        site = web.TCPSite(runner, "0.0.0.0", port)
        await site.start()
        self.logger.info(f"飞书 Webhook 服务器已启动: http://0.0.0.0:{port}/feishu/webhook")

        while self._running:
            await asyncio.sleep(1)

    async def _handle_webhook(self, request):
        """处理飞书 webhook 回调"""
        try:
            from aiohttp import web
            data = await request.json()

            # URL 验证（首次配置时飞书会发 challenge）
            if "challenge" in data:
                return web.json_response({"challenge": data["challenge"]})

            # 事件验证
            if self._verification_token:
                token = data.get("header", {}).get("token", "")
                if token != self._verification_token:
                    self.logger.warning("Webhook 验证令牌不匹配")
                    return web.json_response({"code": 403})

            # 解析事件
            event = data.get("event", {})
            header = data.get("header", {})
            event_type = header.get("event_type", "")
            event_id = header.get("event_id", "")

            if event_type == "im.message.receive_v1":
                if self._is_duplicate(event_id):
                    return web.json_response({"code": 0})
                self._mark_processed(event_id)
                asyncio.create_task(self._process_message_event(event))

            return web.json_response({"code": 0})

        except Exception as e:
            self.logger.error(f"Webhook 处理失败: {e}")
            from aiohttp import web
            return web.json_response({"code": 500})

    # ==========================================================================
    # 轮询模式（兜底）
    # ==========================================================================

    async def _poll_listen(self):
        """通过轮询获取消息（需要开启消息审核回调或使用第三方转发）"""
        self.logger.warning(
            "飞书轮询模式: 此模式需要额外的消息转发服务。"
            "建议使用 WebSocket 模式或 Webhook 模式。"
        )
        poll_interval = 5
        while self._running:
            await asyncio.sleep(poll_interval)

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

    def _is_duplicate(self, event_id: str) -> bool:
        """检查是否为重复事件"""
        if not event_id:
            return False
        now = time.time()
        if event_id in self._processed_events:
            return True
        return False

    def _mark_processed(self, event_id: str):
        """标记事件已处理"""
        if not event_id:
            return
        self._processed_events[event_id] = time.time()
        # 清理过期记录
        now = time.time()
        expired = [
            eid for eid, ts in self._processed_events.items()
            if now - ts > self._dedup_window
        ]
        for eid in expired:
            del self._processed_events[eid]

    # ==========================================================================
    # 辅助方法
    # ==========================================================================

    async def get_chat_info(self, chat_id: str) -> Optional[dict]:
        """获取群聊/单聊信息"""
        await self._ensure_token()
        try:
            url = f"{self.API_BASE}/im/v1/chats/{chat_id}"
            headers = {"Authorization": f"Bearer {self._tenant_access_token}"}
            async with self._session.get(url, headers=headers) as resp:
                result = await resp.json()
                if result.get("code") == 0:
                    return result.get("data", {})
        except Exception as e:
            self.logger.error(f"获取聊天信息失败: {e}")
        return None

    async def send_card_message(self, chat_id: str, card_content: dict) -> bool:
        """
        发送消息卡片（富文本交互卡片）。

        Args:
            chat_id: 聊天 ID
            card_content: 飞书消息卡片 JSON 内容
        """
        await self._ensure_token()
        try:
            url = f"{self.API_BASE}/im/v1/messages"
            headers = {
                "Authorization": f"Bearer {self._tenant_access_token}",
                "Content-Type": "application/json",
            }
            payload = {
                "receive_id": chat_id,
                "msg_type": "interactive",
                "content": json.dumps(card_content, ensure_ascii=False),
            }
            params = {"receive_id_type": "chat_id"}
            async with self._session.post(url, headers=headers, json=payload,
                                           params=params) as resp:
                result = await resp.json()
                return result.get("code") == 0
        except Exception as e:
            self.logger.error(f"发送消息卡片失败: {e}")
            return False

    async def reply_message(self, message_id: str, text: str) -> bool:
        """回复指定消息"""
        await self._ensure_token()
        try:
            url = f"{self.API_BASE}/im/v1/messages/{message_id}/reply"
            headers = {
                "Authorization": f"Bearer {self._tenant_access_token}",
                "Content-Type": "application/json",
            }
            payload = {
                "msg_type": "text",
                "content": json.dumps({"text": text[:5000]}, ensure_ascii=False),
            }
            async with self._session.post(url, headers=headers, json=payload) as resp:
                result = await resp.json()
                if result.get("code") == 0:
                    return True
                self.logger.error(f"飞书回复失败: {result}")
                return False
        except Exception as e:
            self.logger.error(f"飞书回复消息异常: {e}")
            return False
