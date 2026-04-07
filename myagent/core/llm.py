"""
core/llm.py - LLM 客户端模块
=============================
统一封装多种 LLM 提供商的调用接口。
支持: OpenAI, Anthropic (Claude), Ollama (本地), 自定义兼容接口
"""
from __future__ import annotations

import json
import time
import asyncio
from typing import Optional, Dict, Any, List, Generator
from dataclasses import dataclass, field

from core.logger import get_logger
from core.utils import safe_json_parse, retry_async

logger = get_logger("myagent.llm")


# ==============================================================================
# 消息类型定义
# ==============================================================================

@dataclass
class Message:
    """聊天消息"""
    role: str              # system | user | assistant | tool
    content: str = ""
    name: str = ""         # 消息发送者标识
    tool_call_id: str = "" # 工具调用ID
    tool_calls: List[Dict] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        result = {"role": self.role, "content": self.content}
        if self.name:
            result["name"] = self.name
        if self.tool_call_id:
            result["tool_call_id"] = self.tool_call_id
        if self.tool_calls:
            result["tool_calls"] = self.tool_calls
        return result

    @classmethod
    def from_dict(cls, data: dict) -> "Message":
        return cls(
            role=data.get("role", "user"),
            content=data.get("content", ""),
            name=data.get("name", ""),
            tool_call_id=data.get("tool_call_id", ""),
            tool_calls=data.get("tool_calls", []),
        )


@dataclass
class LLMResponse:
    """LLM 响应"""
    content: str = ""
    tool_calls: List[Dict] = field(default_factory=list)
    usage: Dict[str, int] = field(default_factory=dict)
    model: str = ""
    finish_reason: str = ""
    raw_response: Any = None
    success: bool = True
    error: str = ""


# ==============================================================================
# LLM 客户端
# ==============================================================================

class LLMClient:
    """
    统一 LLM 客户端，支持多种提供商。

    使用示例:
        client = LLMClient(provider="openai", api_key="sk-...", model="gpt-4")
        response = await client.chat([Message(role="user", content="你好")])
    """

    def __init__(
        self,
        provider: str = "openai",
        api_key: str = "",
        base_url: str = "",
        model: str = "gpt-4",
        temperature: float = 0.1,
        max_tokens: int = 4096,
        timeout: int = 120,
        max_retries: int = 3,
        **kwargs,
    ):
        self.provider = provider
        self.api_key = api_key
        self.base_url = base_url
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.max_retries = max_retries
        self.extra = kwargs
        self._client = None

    def _ensure_client(self):
        """延迟初始化 LLM 客户端"""
        if self._client is not None:
            return

        if self.provider in ("openai", "custom"):
            self._init_openai()
        elif self.provider == "anthropic":
            self._init_anthropic()
        elif self.provider == "ollama":
            self._init_ollama()
        else:
            raise ValueError(f"不支持的 LLM 提供商: {self.provider}")

    def _init_openai(self):
        """初始化 OpenAI / 兼容客户端"""
        try:
            from openai import OpenAI
            kwargs = {}
            if self.api_key:
                kwargs["api_key"] = self.api_key
            if self.base_url:
                kwargs["base_url"] = self.base_url
            self._client = OpenAI(**kwargs)
            logger.info(f"OpenAI 客户端已初始化 (model={self.model})")
        except ImportError:
            raise ImportError("请安装 openai: pip install openai")

    def _init_anthropic(self):
        """初始化 Anthropic 客户端"""
        try:
            import anthropic
            key = self.extra.get("anthropic_api_key") or self.api_key
            if not key:
                raise ValueError("Anthropic API Key 未设置")
            self._client = anthropic.Anthropic(api_key=key)
            self.model = self.model or "claude-3-sonnet-20240229"
            logger.info(f"Anthropic 客户端已初始化 (model={self.model})")
        except ImportError:
            raise ImportError("请安装 anthropic: pip install anthropic")

    def _init_ollama(self):
        """初始化 Ollama 客户端"""
        try:
            import requests
            self._client = "ollama"
            if not self.base_url:
                self.base_url = "http://localhost:11434"
            logger.info(f"Ollama 客户端已初始化 (model={self.model}, url={self.base_url})")
        except ImportError:
            raise ImportError("请安装 requests: pip install requests")

    async def chat(
        self,
        messages: List[Message],
        tools: Optional[List[Dict]] = None,
        tool_choice: str = "auto",
        response_format: Optional[Dict] = None,
        **kwargs,
    ) -> LLMResponse:
        """
        发送聊天请求。

        Args:
            messages: 消息列表
            tools: 可用工具列表 (OpenAI function calling 格式)
            tool_choice: 工具选择策略
            response_format: 响应格式 (如 {"type": "json_object"})
            **kwargs: 额外参数

        Returns:
            LLMResponse 对象
        """
        self._ensure_client()

        msg_dicts = [m.to_dict() for m in messages]
        request_kwargs = {
            "model": self.model,
            "messages": msg_dicts,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        if tools:
            request_kwargs["tools"] = tools
            request_kwargs["tool_choice"] = tool_choice
        if response_format:
            request_kwargs["response_format"] = response_format
        request_kwargs.update(kwargs)

        try:
            if self.provider in ("openai", "custom"):
                return await self._chat_openai(request_kwargs)
            elif self.provider == "anthropic":
                return await self._chat_anthropic(messages, request_kwargs)
            elif self.provider == "ollama":
                return await self._chat_ollama(request_kwargs)
        except Exception as e:
            logger.error(f"LLM 调用失败: {e}")
            return LLMResponse(success=False, error=str(e))

        return LLMResponse(success=False, error="未知提供商")

    async def _chat_openai(self, kwargs: dict) -> LLMResponse:
        """OpenAI / 兼容接口调用"""
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(
            None, lambda: self._client.chat.completions.create(**kwargs)
        )

        choice = response.choices[0]
        usage = {}
        if response.usage:
            usage = {
                "prompt_tokens": response.usage.prompt_tokens,
                "completion_tokens": response.usage.completion_tokens,
                "total_tokens": response.usage.total_tokens,
            }

        tool_calls = []
        if choice.message.tool_calls:
            for tc in choice.message.tool_calls:
                try:
                    args = json.loads(tc.function.arguments)
                except json.JSONDecodeError:
                    args = {}
                tool_calls.append({
                    "id": tc.id,
                    "name": tc.function.name,
                    "arguments": args,
                })

        return LLMResponse(
            content=choice.message.content or "",
            tool_calls=tool_calls,
            usage=usage,
            model=response.model,
            finish_reason=choice.finish_reason or "",
            raw_response=response,
        )

    async def _chat_anthropic(self, messages: List[Message], kwargs: dict) -> LLMResponse:
        """Anthropic Claude 接口调用"""
        loop = asyncio.get_event_loop()

        # 转换消息格式
        system_msg = ""
        anth_messages = []
        for m in messages:
            if m.role == "system":
                system_msg = m.content
                continue
            anth_messages.append({"role": m.role, "content": m.content})

        create_kwargs = {
            "model": self.model,
            "messages": anth_messages,
            "max_tokens": self.max_tokens,
        }
        if system_msg:
            create_kwargs["system"] = system_msg

        response = await loop.run_in_executor(
            None, lambda: self._client.messages.create(**create_kwargs)
        )

        content = ""
        for block in response.content:
            if block.type == "text":
                content += block.text

        return LLMResponse(
            content=content,
            usage={
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
            },
            model=response.model,
            finish_reason=response.stop_reason or "",
        )

    async def _chat_ollama(self, kwargs: dict) -> LLMResponse:
        """Ollama 本地模型调用"""
        import requests
        loop = asyncio.get_event_loop()

        url = f"{self.base_url}/api/chat"
        payload = {
            "model": self.model,
            "messages": kwargs["messages"],
            "stream": False,
            "options": {
                "temperature": self.temperature,
                "num_predict": self.max_tokens,
            }
        }

        def _request():
            r = requests.post(url, json=payload, timeout=self.timeout)
            r.raise_for_status()
            return r.json()

        result = await loop.run_in_executor(None, _request)
        return LLMResponse(
            content=result.get("message", {}).get("content", ""),
            usage={
                "prompt_tokens": result.get("prompt_eval_count", 0),
                "completion_tokens": result.get("eval_count", 0),
            },
            model=self.model,
            finish_reason="stop" if result.get("done") else "",
        )

    def chat_sync(
        self,
        messages: List[Message],
        **kwargs,
    ) -> LLMResponse:
        """同步版本聊天方法"""
        return asyncio.get_event_loop().run_until_complete(
            self.chat(messages, **kwargs)
        )

    async def chat_json(
        self,
        messages: List[Message],
        required_fields: Optional[List[str]] = None,
        **kwargs,
    ) -> Dict[str, Any]:
        """
        发送聊天请求并解析为 JSON 对象。
        自动添加 JSON 输出提示，并验证返回结构。

        Args:
            messages: 消息列表
            required_fields: 必需字段列表
            **kwargs: 额外参数

        Returns:
            解析后的字典
        """
        # 确保 system prompt 包含 JSON 输出指示
        has_json_instruction = False
        for m in messages:
            if m.role == "system" and "json" in m.content.lower():
                has_json_instruction = True
                break

        if not has_json_instruction:
            json_prompt = "你必须以合法 JSON 格式回复，不要包含任何多余文本、解释或 markdown 标记。"
            messages = [
                Message(role="system", content=json_prompt),
                *messages,
            ]

        kwargs["response_format"] = {"type": "json_object"}
        response = await self.chat(messages, **kwargs)

        if not response.success:
            return {"error": response.error}

        result = safe_json_parse(response.content, {"raw": response.content})
        if required_fields and isinstance(result, dict):
            from core.utils import validate_json_schema
            valid, err = validate_json_schema(result, required_fields)
            if not valid:
                logger.warning(f"JSON 校验失败: {err}")
                result["_validation_error"] = err

        return result


# ==============================================================================
# 全局 LLM 客户端工厂
# ==============================================================================

_global_llm_client: Optional[LLMClient] = None


def get_llm_client() -> LLMClient:
    """获取全局 LLM 客户端"""
    global _global_llm_client
    if _global_llm_client is None:
        from config import get_config
        cfg = get_config().config.llm
        _global_llm_client = LLMClient(
            provider=cfg.provider,
            api_key=cfg.api_key,
            base_url=cfg.base_url,
            model=cfg.model,
            temperature=cfg.temperature,
            max_tokens=cfg.max_tokens,
            timeout=cfg.timeout,
            max_retries=cfg.max_retries,
            anthropic_api_key=cfg.anthropic_api_key,
        )
    return _global_llm_client


def reset_llm_client():
    """重置全局客户端"""
    global _global_llm_client
    _global_llm_client = None
