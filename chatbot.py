"""
聊天平台接入模块 - 纯Python轻量接入
=====================================
支持: Telegram / Discord / 飞书 / QQ / 微信
异步运行，不阻塞主 Agent
多用户/多会话隔离
不使用网关、不使用 Node.js
"""
import os
import sys
import json
import time
import uuid
import logging
import threading
import traceback
from typing import Any, Callable, Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("myagent.chatbot")

from config import get_config


# ============================================================
# 通用消息模型
# ============================================================

@dataclass
class ChatMessage:
    """统一聊天消息格式"""
    platform: str            # telegram / discord / feishu / qq / wechat
    message_id: str = ""
    chat_id: str = ""        # 会话/频道ID
    user_id: str = ""
    username: str = ""
    content: str = ""        # 消息文本
    is_group: bool = False   # 是否群组消息
    reply_to: str = ""       # 回复的消息ID
    timestamp: float = 0.0
    raw_data: Dict = field(default_factory=dict)  # 原始数据

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = time.time()
        if not self.message_id:
            self.message_id = str(uuid.uuid4())


@dataclass
class ChatResponse:
    """统一响应格式"""
    platform: str
    chat_id: str = ""
    message_id: str = ""
    content: str = ""
    parse_mode: str = ""     # Markdown / HTML / 空文本
    reply_to: str = ""       # 回复哪条消息
    metadata: Dict = field(default_factory=dict)


# ============================================================
# 聊天平台基类
# ============================================================

class ChatPlatform(ABC):
    """聊天平台抽象基类"""

    name: str = ""
    is_running: False

    def __init__(self, config: Dict, on_message: Callable[[ChatMessage], ChatResponse]):
        self.config = config
        self.on_message = on_message  # 消息处理回调
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self.allowed_users = config.get("allowed_users", [])
        self.is_running = False
        self._stats = {
            "messages_received": 0,
            "messages_sent": 0,
            "errors": 0,
        }

    @abstractmethod
    def start(self) -> None:
        """启动平台监听"""
        pass

    @abstractmethod
    def stop(self) -> None:
        """停止平台监听"""
        pass

    @abstractmethod
    def send_message(self, response: ChatResponse) -> bool:
        """发送消息"""
        pass

    def _is_user_allowed(self, user_id: str) -> bool:
        """检查用户是否被允许"""
        if not self.allowed_users:
            return True  # 未配置白名单=允许所有
        return user_id in self.allowed_users

    def _make_http_request(
        self,
        url: str,
        method: str = "GET",
        data: Optional[bytes] = None,
        headers: Optional[Dict] = None,
        timeout: int = 30,
    ) -> Tuple[Optional[Dict], Optional[str]]:
        """通用 HTTP 请求 (使用 urllib)"""
        try:
            from urllib.request import Request, urlopen
            from urllib.error import URLError, HTTPError

            req = Request(url, data=data, method=method)
            req.add_header('User-Agent', 'MyAgent/1.0')
            if headers:
                for k, v in headers.items():
                    req.add_header(k, v)

            with urlopen(req, timeout=timeout) as resp:
                body = resp.read().decode('utf-8', errors='replace')
                try:
                    return json.loads(body), None
                except:
                    return {"raw": body}, None
        except HTTPError as e:
            error_body = ""
            try:
                error_body = e.read().decode('utf-8', errors='replace')
            except:
                pass
            return None, f"HTTP {e.code}: {error_body[:500]}"
        except URLError as e:
            return None, f"网络错误: {e.reason}"
        except Exception as e:
            return None, str(e)

    def get_stats(self) -> Dict:
        return dict(self._stats)


# ============================================================
# Telegram Bot
# ============================================================

class TelegramBot(ChatPlatform):
    """Telegram 机器人"""

    name = "telegram"

    def start(self) -> None:
        token = self.config.get("bot_token", "")
        if not token:
            logger.error("Telegram: 未配置 bot_token")
            return

        logger.info("Telegram Bot 启动中...")
        self.is_running = True
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._poll_loop, daemon=True, name="telegram-bot")
        self._thread.start()
        logger.info("Telegram Bot 已启动")

    def stop(self) -> None:
        self.is_running = False
        self._stop_event.set()
        logger.info("Telegram Bot 已停止")

    def _poll_loop(self) -> None:
        """长轮询获取消息"""
        token = self.config.get("bot_token", "")
        poll_interval = self.config.get("poll_interval", 1.0)
        offset = 0
        base_url = f"https://api.telegram.org/bot{token}"

        while not self._stop_event.is_set():
            try:
                url = f"{base_url}/getUpdates?offset={offset}&timeout=30&allowed_updates=[\"message\"]"
                data, error = self._make_http_request(url, timeout=35)

                if error:
                    logger.warning(f"Telegram getUpdates 错误: {error}")
                    time.sleep(poll_interval * 5)
                    continue

                if data and data.get("ok"):
                    results = data.get("result", [])
                    for update in results:
                        offset = update.get("update_id", 0) + 1
                        self._process_update(update)

                time.sleep(poll_interval)

            except Exception as e:
                logger.error(f"Telegram 轮询异常: {e}")
                time.sleep(poll_interval * 5)

    def _process_update(self, update: Dict) -> None:
        """处理 Telegram 更新"""
        message = update.get("message", {})
        if not message:
            return

        chat = message.get("chat", {})
        from_user = message.get("from", {})
        text = message.get("text", "") or message.get("caption", "")

        if not text:
            return

        user_id = str(from_user.get("id", ""))
        if not self._is_user_allowed(user_id):
            logger.info(f"Telegram: 用户 {user_id} 不在白名单中")
            return

        chat_msg = ChatMessage(
            platform=self.name,
            message_id=str(message.get("message_id", "")),
            chat_id=str(chat.get("id", "")),
            user_id=user_id,
            username=from_user.get("username", ""),
            content=text,
            is_group=chat.get("type") == "group",
            timestamp=message.get("date", 0),
            raw_data=update,
        )

        self._stats["messages_received"] += 1

        # 调用消息处理
        try:
            response = self.on_message(chat_msg)
            if response and response.content:
                self.send_message(response)
        except Exception as e:
            logger.error(f"Telegram 处理消息异常: {e}")
            self._stats["errors"] += 1

    def send_message(self, response: ChatResponse) -> bool:
        token = self.config.get("bot_token", "")
        base_url = f"https://api.telegram.org/bot{token}"

        payload = {
            "chat_id": response.chat_id,
            "text": response.content[:4096],  # Telegram 消息长度限制
        }
        if response.parse_mode:
            payload["parse_mode"] = response.parse_mode
        if response.reply_to:
            payload["reply_to_message_id"] = response.reply_to

        data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        result, error = self._make_http_request(
            f"{base_url}/sendMessage",
            method="POST",
            data=data,
            headers={"Content-Type": "application/json"},
        )

        if error:
            logger.error(f"Telegram 发送消息失败: {error}")
            return False

        self._stats["messages_sent"] += 1
        return True


# ============================================================
# Discord Bot
# ============================================================

class DiscordBot(ChatPlatform):
    """Discord 机器人"""

    name = "discord"
    _gateway_url = "wss://gateway.discord.gg/?v=10&encoding=json"

    def start(self) -> None:
        token = self.config.get("bot_token", "")
        if not token:
            logger.error("Discord: 未配置 bot_token")
            return

        logger.info("Discord Bot 启动中...")
        self.is_running = True
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="discord-bot")
        self._thread.start()

    def stop(self) -> None:
        self.is_running = False
        self._stop_event.set()
        logger.info("Discord Bot 已停止")

    def _run(self) -> None:
        """运行 Discord Bot (使用 WebSocket)"""
        token = self.config.get("bot_token", "")

        try:
            import websocket
        except ImportError:
            logger.error("Discord Bot 需要 websocket-client: pip install websocket-client")
            return

        self.ws = websocket.WebSocket()
        heartbeat_interval = 41250
        session_id = ""
        seq = None

        try:
            self.ws.connect(self._gateway_url)
            logger.info("Discord: 已连接 Gateway")

            while not self._stop_event.is_set():
                try:
                    self.ws.settimeout(5)
                    msg = self.ws.recv()
                    if not msg:
                        continue

                    data = json.loads(msg)
                    op = data.get("op")
                    t = data.get("t", "")
                    d = data.get("d", {})

                    if op == 10:  # Hello
                        heartbeat_interval = d.get("heartbeat_interval", 41250) / 1000
                        # Identify
                        identify = {
                            "op": 2,
                            "d": {
                                "token": token,
                                "intents": 1 << 9 | 1 << 10,  # GUILD_MESSAGES | DIRECT_MESSAGES
                                "properties": {
                                    "os": sys.platform,
                                    "browser": "MyAgent",
                                    "device": "MyAgent",
                                },
                            }
                        }
                        self.ws.send(json.dumps(identify))

                    elif op == 11:  # Heartbeat ACK
                        pass

                    elif op == 1:  # Heartbeat
                        self.ws.send(json.dumps({"op": 1, "d": seq}))

                    elif t == "READY":
                        session_id = d.get("session_id", "")
                        logger.info(f"Discord: Bot 就绪, session={session_id}")

                    elif t == "MESSAGE_CREATE":
                        self._process_message(d, seq)

                    if data.get("s"):
                        seq = data["s"]

                except Exception as e:
                    if not self._stop_event.is_set():
                        logger.error(f"Discord 消息处理异常: {e}")
                        time.sleep(1)

        except Exception as e:
            logger.error(f"Discord 连接失败: {e}")

    def _process_message(self, data: Dict, seq: Any = None) -> None:
        """处理 Discord 消息"""
        author = data.get("author", {})

        # 忽略自己的消息
        if author.get("bot", False):
            return

        content = data.get("content", "")
        if not content:
            return

        channel_id = data.get("channel_id", "")
        user_id = author.get("id", "")
        guild_id = data.get("guild_id", "")

        # 检查频道/用户白名单
        allowed_channels = self.config.get("allowed_channels", [])
        if allowed_channels and channel_id not in allowed_channels:
            return
        if not self._is_user_allowed(user_id):
            return

        chat_msg = ChatMessage(
            platform=self.name,
            message_id=data.get("id", ""),
            chat_id=channel_id,
            user_id=user_id,
            username=author.get("username", ""),
            content=content,
            is_group=bool(guild_id),
            timestamp=data.get("timestamp", 0),
            raw_data=data,
        )

        self._stats["messages_received"] += 1

        try:
            response = self.on_message(chat_msg)
            if response and response.content:
                self.send_message(response)
        except Exception as e:
            logger.error(f"Discord 处理消息异常: {e}")
            self._stats["errors"] += 1

    def send_message(self, response: ChatResponse) -> bool:
        token = self.config.get("bot_token", "")
        channel_id = response.chat_id

        payload = {
            "content": response.content[:2000],  # Discord 消息限制
        }
        if response.reply_to:
            payload["message_reference"] = {"message_id": response.reply_to}

        data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        result, error = self._make_http_request(
            f"https://discord.com/api/v10/channels/{channel_id}/messages",
            method="POST",
            data=data,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bot {token}",
            },
        )

        if error:
            logger.error(f"Discord 发送消息失败: {error}")
            return False

        self._stats["messages_sent"] += 1
        return True


# ============================================================
# 飞书 Bot
# ============================================================

class FeishuBot(ChatPlatform):
    """飞书机器人"""

    name = "feishu"

    def start(self) -> None:
        app_id = self.config.get("app_id", "")
        app_secret = self.config.get("app_secret", "")
        if not app_id or not app_secret:
            logger.error("飞书: 未配置 app_id 或 app_secret")
            return

        logger.info("飞书 Bot 启动中...")
        self.is_running = True
        self._stop_event.clear()
        self._tenant_token = ""
        self._token_expire = 0

        # 获取 tenant_access_token
        self._refresh_token()

        # 启动轮询 (使用 long polling 模拟)
        self._thread = threading.Thread(target=self._poll_loop, daemon=True, name="feishu-bot")
        self._thread.start()
        logger.info("飞书 Bot 已启动")

    def stop(self) -> None:
        self.is_running = False
        self._stop_event.set()
        logger.info("飞书 Bot 已停止")

    def _refresh_token(self) -> None:
        """刷新飞书 tenant_access_token"""
        app_id = self.config.get("app_id", "")
        app_secret = self.config.get("app_secret", "")

        payload = {
            "app_id": app_id,
            "app_secret": app_secret,
        }
        data = json.dumps(payload).encode('utf-8')

        result, error = self._make_http_request(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            method="POST",
            data=data,
            headers={"Content-Type": "application/json"},
        )

        if result and result.get("code") == 0:
            self._tenant_token = result.get("tenant_access_token", "")
            self._token_expire = time.time() + result.get("expire", 7200) - 60
            logger.info("飞书: token 刷新成功")
        else:
            logger.error(f"飞书 token 刷新失败: {error}")

    def _poll_loop(self) -> None:
        """长轮询获取消息"""
        while not self._stop_event.is_set():
            try:
                if time.time() > self._token_expire:
                    self._refresh_token()

                if not self._tenant_token:
                    time.sleep(10)
                    continue

                # 使用 receive message API
                headers = {
                    "Authorization": f"Bearer {self._tenant_token}",
                    "Content-Type": "application/json",
                }

                result, error = self._make_http_request(
                    "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=user_id",
                    headers=headers,
                    timeout=30,
                )

                if result and result.get("code") == 0:
                    items = result.get("data", {}).get("items", [])
                    for item in items:
                        self._process_message(item)

                time.sleep(2)

            except Exception as e:
                logger.error(f"飞书 轮询异常: {e}")
                time.sleep(10)

    def _process_message(self, item: Dict) -> None:
        """处理飞书消息"""
        # 这里简化处理，实际需要处理事件回调
        # 飞书推荐使用事件订阅，此处使用轮询模拟
        msg_type = item.get("msg_type", "")
        content_str = item.get("body", {}).get("content", "")

        if msg_type == "text":
            try:
                content_data = json.loads(content_str)
                text = content_data.get("text", "")
            except:
                text = content_str
        else:
            text = f"[{msg_type}] {content_str[:200]}"

        if not text:
            return

        chat_msg = ChatMessage(
            platform=self.name,
            message_id=item.get("message_id", ""),
            chat_id=item.get("chat_id", ""),
            user_id=item.get("sender", {}).get("sender_id", {}).get("user_id", ""),
            content=text,
            timestamp=item.get("create_time", ""),
            raw_data=item,
        )

        self._stats["messages_received"] += 1

        try:
            response = self.on_message(chat_msg)
            if response and response.content:
                self.send_message(response)
        except Exception as e:
            logger.error(f"飞书 处理消息异常: {e}")
            self._stats["errors"] += 1

    def send_message(self, response: ChatResponse) -> bool:
        if time.time() > self._token_expire:
            self._refresh_token()

        if not self._tenant_token:
            return False

        payload = {
            "receive_id": response.chat_id,
            "msg_type": "text",
            "content": json.dumps({"text": response.content[:4000]}, ensure_ascii=False),
        }

        data = json.dumps(payload).encode('utf-8')
        result, error = self._make_http_request(
            f"https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=user_id",
            method="POST",
            data=data,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._tenant_token}",
            },
        )

        if error:
            logger.error(f"飞书 发送消息失败: {error}")
            return False

        self._stats["messages_sent"] += 1
        return True


# ============================================================
# QQ Bot
# ============================================================

class QQBot(ChatPlatform):
    """QQ 机器人 (使用官方 API)"""

    name = "qq"

    def start(self) -> None:
        appid = self.config.get("bot_appid", "")
        token = self.config.get("bot_token", "")
        if not appid or not token:
            logger.error("QQ: 未配置 bot_appid 或 bot_token")
            return

        logger.info("QQ Bot 启动中...")
        self.is_running = True
        self._stop_event.clear()
        self._ws_url = ""
        self._session_id = ""

        # 获取 WebSocket gateway
        self._get_gateway()

        if self._ws_url:
            self._thread = threading.Thread(target=self._run_ws, daemon=True, name="qq-bot")
            self._thread.start()
            logger.info("QQ Bot 已启动")
        else:
            logger.error("QQ Bot: 无法获取 Gateway")

    def stop(self) -> None:
        self.is_running = False
        self._stop_event.set()

    def _get_gateway(self) -> None:
        """获取 QQ Bot WebSocket 网关"""
        appid = self.config.get("bot_appid", "")
        token = self.config.get("bot_token", "")

        result, error = self._make_http_request(
            f"https://api.sgroup.qq.com/gateway",
            headers={
                "Authorization": f"QQBot {token}",
            },
        )

        if result:
            self._ws_url = result.get("url", "")

    def _run_ws(self) -> None:
        """WebSocket 连接"""
        try:
            import websocket
        except ImportError:
            logger.error("QQ Bot 需要 websocket-client: pip install websocket-client")
            return

        token = self.config.get("bot_token", "")
        heartbeat_interval = 41250
        seq = None

        try:
            ws = websocket.WebSocket()
            ws.connect(self._ws_url)

            while not self._stop_event.is_set():
                try:
                    ws.settimeout(5)
                    msg = ws.recv()
                    if not msg:
                        continue

                    data = json.loads(msg)
                    op = data.get("op")
                    t = data.get("t", "")
                    d = data.get("d", {})

                    if op == 10:  # Hello
                        heartbeat_interval = d.get("heartbeat_interval", 41250) / 1000
                        identify = {
                            "op": 2,
                            "d": {
                                "token": f"QQBot {token}",
                                "intents": 1 << 25,  # PUBLIC_MESSAGES
                                "shard": [0, 1],
                            }
                        }
                        ws.send(json.dumps(identify))

                    elif op == 11:  # Heartbeat ACK
                        pass

                    elif op == 0 and t == "AT_MESSAGE_CREATE":
                        self._process_message(d)

                    if data.get("s"):
                        seq = data["s"]

                except Exception as e:
                    if not self._stop_event.is_set():
                        time.sleep(1)

        except Exception as e:
            logger.error(f"QQ WebSocket 异常: {e}")

    def _process_message(self, data: Dict) -> None:
        """处理 QQ 消息"""
        content = data.get("content", "")
        if not content:
            return

        chat_msg = ChatMessage(
            platform=self.name,
            message_id=data.get("id", ""),
            chat_id=data.get("channel_id", ""),
            user_id=data.get("author", {}).get("id", ""),
            content=content,
            is_group=True,  # QQ 频道消息
            raw_data=data,
        )

        self._stats["messages_received"] += 1

        try:
            response = self.on_message(chat_msg)
            if response and response.content:
                self.send_message(response)
        except Exception as e:
            logger.error(f"QQ 处理消息异常: {e}")
            self._stats["errors"] += 1

    def send_message(self, response: ChatResponse) -> bool:
        token = self.config.get("bot_token", "")
        msg_id = response.message_id

        if not msg_id:
            return False

        payload = {
            "content": response.content[:2000],
            "msg_id": msg_id,
        }

        data = json.dumps(payload).encode('utf-8')
        result, error = self._make_http_request(
            f"https://api.sgroup.qq.com/v2/channels/{response.chat_id}/messages",
            method="POST",
            data=data,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"QQBot {token}",
            },
        )

        if error:
            logger.error(f"QQ 发送消息失败: {error}")
            return False

        self._stats["messages_sent"] += 1
        return True


# ============================================================
# 微信 Bot
# ============================================================

class WeChatBot(ChatPlatform):
    """微信机器人 (通过 HTTP API 接入)"""

    name = "wechat"

    def start(self) -> None:
        api_url = self.config.get("api_url", "")
        api_token = self.config.get("api_token", "")
        if not api_url:
            logger.error("微信: 未配置 api_url (需要 HTTP API 服务)")
            return

        logger.info("微信 Bot 启动中...")
        self.is_running = True
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._poll_loop, daemon=True, name="wechat-bot")
        self._thread.start()
        logger.info("微信 Bot 已启动")

    def stop(self) -> None:
        self.is_running = False
        self._stop_event.set()

    def _poll_loop(self) -> None:
        """轮询获取微信消息"""
        api_url = self.config.get("api_url", "").rstrip("/")
        api_token = self.config.get("api_token", "")

        while not self._stop_event.is_set():
            try:
                headers = {}
                if api_token:
                    headers["Authorization"] = f"Bearer {api_token}"

                result, error = self._make_http_request(
                    f"{api_url}/messages?limit=10",
                    headers=headers,
                    timeout=30,
                )

                if result and isinstance(result, list):
                    for item in result:
                        self._process_message(item)
                elif result and isinstance(result, dict):
                    items = result.get("messages", result.get("data", []))
                    if isinstance(items, list):
                        for item in items:
                            self._process_message(item)

                time.sleep(2)

            except Exception as e:
                logger.error(f"微信 轮询异常: {e}")
                time.sleep(10)

    def _process_message(self, item: Dict) -> None:
        """处理微信消息"""
        # 适配多种微信 HTTP API 格式
        text = (
            item.get("content", "") or
            item.get("text", "") or
            item.get("message", "") or
            item.get("body", "")
        )

        if not text:
            return

        chat_msg = ChatMessage(
            platform=self.name,
            message_id=item.get("msg_id", item.get("id", "")),
            chat_id=item.get("chat_id", item.get("from", "")),
            user_id=item.get("user_id", item.get("from_user", item.get("sender", ""))),
            username=item.get("nickname", item.get("name", "")),
            content=text,
            raw_data=item,
        )

        self._stats["messages_received"] += 1

        try:
            response = self.on_message(chat_msg)
            if response and response.content:
                self.send_message(response)
        except Exception as e:
            logger.error(f"微信 处理消息异常: {e}")
            self._stats["errors"] += 1

    def send_message(self, response: ChatResponse) -> bool:
        api_url = self.config.get("api_url", "").rstrip("/")
        api_token = self.config.get("api_token", "")

        payload = {
            "to": response.chat_id,
            "content": response.content[:4000],
        }

        data = json.dumps(payload).encode('utf-8')
        headers = {"Content-Type": "application/json"}
        if api_token:
            headers["Authorization"] = f"Bearer {api_token}"

        result, error = self._make_http_request(
            f"{api_url}/send",
            method="POST",
            data=data,
            headers=headers,
        )

        if error:
            logger.error(f"微信 发送消息失败: {error}")
            return False

        self._stats["messages_sent"] += 1
        return True


# ============================================================
# 聊天平台管理器
# ============================================================

class ChatBotManager:
    """
    聊天平台管理器
    统一管理所有接入的聊天平台
    """

    PLATFORM_MAP = {
        "telegram": TelegramBot,
        "discord": DiscordBot,
        "feishu": FeishuBot,
        "qq": QQBot,
        "wechat": WeChatBot,
    }

    def __init__(self, on_message_handler: Optional[Callable] = None):
        cfg = get_config()
        self.chatbot_config = cfg.get("chatbot", {})
        self.on_message_handler = on_message_handler
        self._platforms: Dict[str, ChatPlatform] = {}
        self._sessions: Dict[str, str] = {}  # (platform, chat_id) -> session_id

    def setup(self) -> None:
        """初始化并启动所有启用的平台"""
        for platform_name, platform_cls in self.PLATFORM_MAP.items():
            config = self.chatbot_config.get(platform_name, {})
            if not config.get("enabled", False):
                logger.info(f"聊天平台 [{platform_name}] 未启用")
                continue

            try:
                platform = platform_cls(config, self._handle_message)
                self._platforms[platform_name] = platform
                logger.info(f"聊天平台 [{platform_name}] 已注册")
            except Exception as e:
                logger.error(f"注册聊天平台 [{platform_name}] 失败: {e}")

    def start_all(self) -> None:
        """启动所有已注册的平台"""
        for name, platform in self._platforms.items():
            try:
                platform.start()
                logger.info(f"聊天平台 [{name}] 已启动")
            except Exception as e:
                logger.error(f"启动聊天平台 [{name}] 失败: {e}")

    def stop_all(self) -> None:
        """停止所有平台"""
        for name, platform in self._platforms.items():
            try:
                platform.stop()
                logger.info(f"聊天平台 [{name}] 已停止")
            except Exception as e:
                logger.error(f"停止聊天平台 [{name}] 失败: {e}")

    def _handle_message(self, message: ChatMessage) -> ChatResponse:
        """
        统一消息处理
        将聊天平台消息路由到 Agent
        """
        # 获取或创建会话
        session_key = f"{message.platform}:{message.chat_id}"
        session_id = self._sessions.get(session_key)
        if not session_id:
            session_id = f"{message.platform}_{message.chat_id}"
            self._sessions[session_key] = session_id

        # 记录来源信息
        source_info = f"[{message.platform}] {message.username or message.user_id}: "

        # 调用 Agent
        if self.on_message_handler:
            reply_text = self.on_message_handler(
                message.content,
                session_id=session_id,
            )
        else:
            reply_text = "消息已收到，但未配置消息处理器。"

        return ChatResponse(
            platform=message.platform,
            chat_id=message.chat_id,
            message_id=message.message_id,
            content=reply_text,
            reply_to=message.message_id,
        )

    def get_stats(self) -> Dict[str, Dict]:
        """获取所有平台统计"""
        stats = {}
        for name, platform in self._platforms.items():
            stats[name] = platform.get_stats()
        return stats

    def get_active_platforms(self) -> List[str]:
        """获取活跃平台列表"""
        return [name for name, p in self._platforms.items() if p.is_running]
