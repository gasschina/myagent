"""
chatbot/wechat_bot.py - 微信机器人
=====================================
微信机器人适配器。

注意: 微信没有官方的个人号第三方机器人 API。
本实现支持以下模式:
  1. WxPusher (微信公众号消息推送 + 接收用户消息回调)
  2. HTTP 桥接模式 (通过 wechaty / comwechat 等桥接)
  3. 企业微信 Webhook (单向推送)

推荐方案:
  - 需要双向通信: HTTP 桥接模式
  - 只需推送: WxPusher / 企业微信 Webhook
"""
from __future__ import annotations

import asyncio
import json
import hashlib
import time
from typing import Optional, Dict, Any

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
      - WxPusher 模式 (mode="wxpusher"):
        - app_id: WxPusher 应用 Token (appToken)
        - app_secret: WxPusher 应用 Secret (用于回调验证)

      - HTTP 桥接模式 (mode="http_bridge"):
        - token: HTTP 桥接 API 地址 (如 http://localhost:3000)
        - extra.callback_port: 回调监听端口，默认 8100
        - extra.callback_token: 回调验证 Token

      - 企业微信 Webhook (mode="wework_webhook"):
        - token: Webhook Key (https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx)

    额外配置 (kwargs):
      - mode: "wxpusher" | "http_bridge" | "wework_webhook"
    """

    platform_name = "wechat"

    def __init__(self, app_id: str = "", app_secret: str = "", **kwargs):
        super().__init__(**kwargs)
        self.app_id = app_id
        self.app_secret = app_secret
        self._session: Optional[aiohttp.ClientSession] = None
        self._mode = kwargs.get("mode", "")

        # 根据配置自动选择模式
        if not self._mode:
            if app_id:
                self._mode = "wxpusher"
            elif self.token:
                # 判断是否为企业微信 Webhook URL
                if "qyapi.weixin.qq.com" in self.token:
                    self._mode = "wework_webhook"
                else:
                    self._mode = "http_bridge"
            else:
                self._mode = "http_bridge"

        # HTTP 桥接模式的回调配置
        self._callback_port = kwargs.get("callback_port", 8100)
        self._callback_token = kwargs.get("callback_token", "")
        # 消息去重
        self._processed_msg_ids: Dict[str, float] = {}

    async def start(self):
        """启动微信机器人"""
        if not HAS_AIOHTTP:
            self.logger.error("请安装 aiohttp: pip install aiohttp")
            return

        self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))
        self._running = True

        if self._mode == "wxpusher":
            await self._start_wxpusher()
        elif self._mode == "http_bridge":
            await self._start_http_bridge()
        elif self._mode == "wework_webhook":
            self.logger.info("企业微信 Webhook 模式已启动 (仅支持推送)")
            while self._running:
                await asyncio.sleep(1)
        else:
            self.logger.warning(f"未知模式: {self._mode}")
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
            elif self._mode == "wework_webhook":
                return await self._send_wework_webhook(response)
            else:
                self.logger.error(f"不支持的发送模式: {self._mode}")
                return False
        except Exception as e:
            self.logger.error(f"微信发送消息异常: {e}")
            return False

    # ==========================================================================
    # WxPusher 模式
    # ==========================================================================

    async def _start_wxpusher(self):
        """启动 WxPusher 模式（含消息接收回调服务器）"""
        if not self.app_id:
            self.logger.error("WxPusher 模式需要配置 app_id (appToken)")
            return

        self.logger.info(
            f"微信机器人(WxPusher 模式)已启动 - AppToken: {self.app_id[:8]}..."
        )

        # 如果配置了 app_secret，启动回调服务器接收用户消息
        if self.app_secret:
            asyncio.create_task(self._wxpusher_callback_server())
            self.logger.info(f"WxPusher 回调服务器已启动: 0.0.0.0:{self._callback_port}")
        else:
            self.logger.warning(
                "WxPusher 模式未配置 app_secret，无法接收用户消息。"
                "如需双向通信，请在 WxPusher 管理后台配置回调地址。"
            )

        while self._running:
            await asyncio.sleep(1)

    async def _wxpusher_callback_server(self):
        """WxPusher 消息接收回调服务器"""
        try:
            from aiohttp import web
        except ImportError:
            self.logger.error("需要 aiohttp: pip install aiohttp")
            return

        app = web.Application()
        app.router.add_post("/wxpusher/callback", self._handle_wxpusher_callback)
        app.router.add_get("/wxpusher/health", self._health_check)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", self._callback_port)
        await site.start()

        while self._running:
            await asyncio.sleep(1)

    async def _handle_wxpusher_callback(self, request):
        """处理 WxPusher 消息回调"""
        try:
            from aiohttp import web
            data = await request.json()

            # WxPusher 验证签名
            signature = request.headers.get("Signature", "")
            if signature and self.app_secret:
                body = await request.text()
                expected = self._wxpusher_sign(body, self.app_secret)
                if signature != expected:
                    self.logger.warning("WxPusher 签名验证失败")
                    return web.json_response({"code": -1, "msg": "签名错误"})

            action = data.get("action", "")
            data_type = data.get("type", "")

            # 验证消息（首次配置回调时的验证请求）
            if action == "appVerify":
                verify_token = data.get("data", {}).get("verifyToken", "")
                if verify_token == self.app_secret:
                    return web.json_response({"code": 0, "msg": "ok"})
                return web.json_response({"code": -1, "msg": "验证失败"})

            # 处理用户消息
            if data_type == "msg":
                msg_data = data.get("data", {})
                user_id = msg_data.get("from", "")
                msg_id = msg_data.get("msgId", "")
                content = msg_data.get("content", "")

                if self._is_duplicate_msg(msg_id):
                    return web.json_response({"code": 0})

                self._mark_msg_processed(msg_id)

                # 消息内容解析
                # WxPusher content 格式: {"type":"text","content":"xxx"} 或纯文本
                if isinstance(content, str):
                    try:
                        content_obj = json.loads(content)
                        text = content_obj.get("content", content)
                    except json.JSONDecodeError:
                        text = content
                else:
                    text = str(content)

                if not text.strip():
                    return web.json_response({"code": 0})

                message = ChatMessage(
                    platform=self.platform_name,
                    chat_id=f"wxpusher_{user_id}",
                    user_id=user_id,
                    username=msg_data.get("fromName", user_id),
                    text=text.strip(),
                    is_group=False,
                    raw_data={"wxpusher_data": msg_data},
                )

                # 异步处理消息
                asyncio.create_task(self._handle_message(message))

            return web.json_response({"code": 0, "msg": "success"})

        except Exception as e:
            self.logger.error(f"WxPusher 回调处理失败: {e}")
            from aiohttp import web
            return web.json_response({"code": -1, "msg": str(e)})

    async def _send_wxpusher(self, response: ChatResponse) -> bool:
        """通过 WxPusher 发送消息"""
        if not self.app_id:
            return False

        url = "https://wxpusher.zjiecode.com/api/send/message"
        payload = {
            "appToken": self.app_id,
            "content": response.text[:5000],
            "contentType": 1,  # 1=文本, 2=HTML, 3=Markdown
            "uids": [response.user_id] if response.user_id else [],
            "topicIds": [],
        }

        # 如果是回复消息，添加引用
        if response.reply_to:
            payload["reference"] = {
                "msgId": response.reply_to,
                "type": 1,
            }

        async with self._session.post(url, json=payload) as resp:
            result = await resp.json()
            if result.get("code") == 1000:
                return True
            self.logger.error(f"WxPusher 发送失败: {result}")
            return False

    @staticmethod
    def _wxpusher_sign(body: str, token: str) -> str:
        """WxPusher 签名计算"""
        return hashlib.sha1(f"{body}{token}".encode()).hexdigest()

    # ==========================================================================
    # HTTP 桥接模式
    # ==========================================================================

    async def _start_http_bridge(self):
        """启动 HTTP 桥接模式"""
        bridge_url = self.token or ""
        if not bridge_url:
            self.logger.warning(
                "HTTP 桥接模式未配置 API 地址 (token)。"
                "请设置微信桥接服务的地址 (如 http://localhost:3000)。"
            )
        else:
            self.logger.info(f"微信机器人(HTTP 桥接模式)已启动: {bridge_url}")

        # 启动回调接收服务器
        asyncio.create_task(self._bridge_callback_server())

        while self._running:
            await asyncio.sleep(1)

    async def _bridge_callback_server(self):
        """HTTP 桥接回调服务器"""
        try:
            from aiohttp import web
        except ImportError:
            self.logger.error("需要 aiohttp: pip install aiohttp")
            return

        app = web.Application()
        app.router.add_post("/wechat/callback", self._handle_bridge_callback)
        app.router.add_get("/wechat/health", self._health_check)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", self._callback_port)
        await site.start()
        self.logger.info(
            f"微信桥接回调服务器已启动: http://0.0.0.0:{self._callback_port}/wechat/callback"
        )

        while self._running:
            await asyncio.sleep(1)

    async def _handle_bridge_callback(self, request):
        """
        处理 HTTP 桥接回调。

        兼容多种桥接协议格式:
        - wechaty-puppet: {"from": "xxx", "to": "xxx", "text": "xxx", "room": "..."}
        - comwechat: {"data": {"fromUser": "xxx", "toUser": "xxx", "content": "xxx"}}
        - 通用格式: {"user_id": "xxx", "message": "xxx", "group_id": "..."}
        """
        try:
            from aiohttp import web
            data = await request.json()

            # Token 验证
            if self._callback_token:
                auth = request.headers.get("Authorization", "")
                if auth != f"Bearer {self._callback_token}":
                    self.logger.warning("桥接回调 Token 验证失败")
                    return web.json_response({"code": 403})

            # 尝试多种格式解析
            message = self._parse_bridge_message(data)
            if message and message.text.strip():
                asyncio.create_task(self._handle_message(message))

            return web.json_response({"code": 0})

        except Exception as e:
            self.logger.error(f"桥接回调处理失败: {e}")
            from aiohttp import web
            return web.json_response({"code": 500, "msg": str(e)})

    def _parse_bridge_message(self, data: dict) -> Optional[ChatMessage]:
        """
        解析桥接消息，兼容多种协议格式。

        支持的格式:
        1. wechaty: {"from": "wxid_xxx", "text": "hello", "room": "roomid"}
        2. comwechat: {"data": {"fromUser": "wxid_xxx", "content": "hello", "isGroup": 0}}
        3. 通用: {"user_id": "xxx", "message": "xxx", "group_id": "xxx"}
        """
        text = ""
        user_id = ""
        chat_id = ""
        username = ""
        is_group = False
        msg_id = ""

        # 格式1: wechaty-puppet
        if "from" in data and "text" in data:
            user_id = data["from"]
            text = data["text"]
            room = data.get("room", "")
            is_group = bool(room)
            chat_id = room if room else user_id
            username = data.get("name", data.get("from", ""))
            msg_id = data.get("id", "")

        # 格式2: comwechat
        elif "data" in data:
            inner = data["data"]
            user_id = inner.get("fromUser", inner.get("fromGroup", ""))
            text = inner.get("content", inner.get("msg", ""))
            chat_id = inner.get("toUser", "")
            is_group = inner.get("isGroup", 0) == 1
            if is_group:
                chat_id = inner.get("fromGroup", chat_id)
            else:
                chat_id = user_id
            username = inner.get("fromName", user_id)
            msg_id = inner.get("msgId", inner.get("newMsgId", ""))

        # 格式3: 通用格式
        elif "user_id" in data or "message" in data:
            user_id = data.get("user_id", "")
            text = data.get("message", data.get("text", data.get("content", "")))
            chat_id = data.get("group_id", data.get("chat_id", user_id))
            is_group = bool(data.get("group_id", ""))
            username = data.get("username", user_id)
            msg_id = data.get("msg_id", "")

        else:
            self.logger.debug(f"无法识别的桥接消息格式: {list(data.keys())}")
            return None

        if self._is_duplicate_msg(msg_id):
            return None
        self._mark_msg_processed(msg_id)

        return ChatMessage(
            platform=self.platform_name,
            chat_id=chat_id,
            user_id=user_id,
            username=username,
            text=text.strip(),
            is_group=is_group,
            reply_to=msg_id,
            raw_data={"bridge_data": data},
        )

    async def _send_http_bridge(self, response: ChatResponse) -> bool:
        """通过 HTTP 桥接发送消息"""
        if not self.token:
            return False

        base_url = self.token.rstrip("/")
        headers = {}
        if self._callback_token:
            headers["Authorization"] = f"Bearer {self._callback_token}"

        payload = {
            "to": response.user_id or response.chat_id,
            "message": response.text,
            "type": "text",
        }

        # 群聊消息需要指定群
        if hasattr(response, "metadata") and response.metadata.get("is_group"):
            payload["room"] = response.chat_id
            payload["to"] = response.user_id  # @指定用户或空

        try:
            async with self._session.post(
                f"{base_url}/send",
                json=payload,
                headers=headers,
            ) as resp:
                if resp.status == 200:
                    result = await resp.json()
                    if result.get("code", 0) == 0 or result.get("success", True):
                        return True
                self.logger.error(f"HTTP 桥接发送失败: {resp.status}")
                return False
        except Exception as e:
            self.logger.error(f"HTTP 桥接发送异常: {e}")
            return False

    # ==========================================================================
    # 企业微信 Webhook 模式
    # ==========================================================================

    async def _send_wework_webhook(self, response: ChatResponse) -> bool:
        """通过企业微信 Webhook 发送"""
        if not self.token:
            return False

        # 支持 Webhook Key 或完整 URL
        if self.token.startswith("http"):
            url = self.token
        else:
            url = f"https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={self.token}"

        # 支持 Markdown 格式
        msg_type = "markdown" if response.parse_mode == "markdown" else "text"
        payload: Dict[str, Any] = {
            "msgtype": msg_type,
            msg_type: {
                "content": response.text[:4096],
            },
        }

        # 支持提及指定人
        if response.user_id:
            mentioned_list = [response.user_id]
            if msg_type == "text":
                payload["text"]["mentioned_list"] = mentioned_list

        async with self._session.post(url, json=payload) as resp:
            result = await resp.json()
            if result.get("errcode") == 0:
                return True
            self.logger.error(f"企业微信 Webhook 发送失败: {result}")
            return False

    # ==========================================================================
    # 辅助方法
    # ==========================================================================

    async def _health_check(self, request):
        """健康检查端点"""
        from aiohttp import web
        return web.json_response({
            "status": "ok",
            "mode": self._mode,
            "running": self._running,
        })

    def _is_duplicate_msg(self, msg_id: str) -> bool:
        """消息去重检查"""
        if not msg_id:
            return False
        return msg_id in self._processed_msg_ids

    def _mark_msg_processed(self, msg_id: str):
        """标记消息已处理"""
        if not msg_id:
            return
        self._processed_msg_ids[msg_id] = time.time()
        # 清理 5 分钟前的记录
        now = time.time()
        expired = [
            mid for mid, ts in self._processed_msg_ids.items()
            if now - ts > 300
        ]
        for mid in expired:
            del self._processed_msg_ids[mid]

    async def send_markdown(self, response: ChatResponse) -> bool:
        """发送 Markdown 格式消息（WxPusher/企业微信支持）"""
        response_copy = ChatResponse(
            chat_id=response.chat_id,
            user_id=response.user_id,
            text=response.text,
            parse_mode="markdown",
            reply_to=response.reply_to,
            metadata=response.metadata,
        )
        # WxPusher 支持内容类型 3=Markdown
        if self._mode == "wxpusher":
            try:
                url = "https://wxpusher.zjiecode.com/api/send/message"
                payload = {
                    "appToken": self.app_id,
                    "content": response.text[:5000],
                    "contentType": 3,  # Markdown
                    "uids": [response.user_id] if response.user_id else [],
                }
                async with self._session.post(url, json=payload) as resp:
                    result = await resp.json()
                    return result.get("code") == 1000
            except Exception as e:
                self.logger.error(f"WxPusher Markdown 发送失败: {e}")
                return False
        return await self.send_message(response_copy)
