"""
core/llm.py - LLM 客户端模块
=============================
统一封装多种 LLM 提供商的调用接口。
支持: OpenAI, Anthropic (Claude), Ollama (本地), Zhipu GLM, 自定义兼容接口

增强功能:
- JSON 严格解析（4 策略）
- 流式输出 (Streaming)
- Token 用量追踪 & 费用统计
- 全局重试 + 指数退避
- asyncio 现代化 (get_running_loop)
"""
from __future__ import annotations

import json
import time
import asyncio
from typing import (
    Optional, Dict, Any, List, Generator, AsyncGenerator,
)
from dataclasses import dataclass, field

from core.logger import get_logger
from core.utils import safe_json_parse

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

    支持提供商: openai, anthropic, ollama, zhipu, custom

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

        # ---- Token 用量追踪 ----
        self._total_prompt_tokens: int = 0
        self._total_completion_tokens: int = 0
        self._total_tokens_used: int = 0
        self._total_cost: float = 0.0
        self._call_count: int = 0

    # ------------------------------------------------------------------
    # Token 用量 & 费用追踪
    # ------------------------------------------------------------------

    def _record_usage(self, usage: Dict[str, int], model: str = ""):
        """记录一次调用的 token 用量并估算费用。"""
        prompt = usage.get("prompt_tokens", 0) or usage.get("input_tokens", 0)
        completion = usage.get("completion_tokens", 0) or usage.get("output_tokens", 0)
        total = prompt + completion

        self._total_prompt_tokens += prompt
        self._total_completion_tokens += completion
        self._total_tokens_used += total
        self._call_count += 1

        # 简单费用估算 (每百万 token, 粗略均价)
        cost = self._estimate_cost(prompt, completion, model or self.model)
        self._total_cost += cost

    @staticmethod
    def _estimate_cost(prompt_tokens: int, completion_tokens: int, model: str) -> float:
        """
        按模型粗略估算 API 费用 (USD)。
        价格基于 2024 年公开信息，仅做参考。
        """
        # (input price per 1M tokens, output price per 1M tokens)
        pricing: Dict[str, tuple] = {
            "gpt-4": (30.0, 60.0),
            "gpt-4-turbo": (10.0, 30.0),
            "gpt-4o": (2.5, 10.0),
            "gpt-4o-mini": (0.15, 0.6),
            "gpt-3.5-turbo": (0.5, 1.5),
            "claude-3-opus": (15.0, 75.0),
            "claude-3-sonnet": (3.0, 15.0),
            "claude-3-haiku": (0.25, 1.25),
            "claude-3.5-sonnet": (3.0, 15.0),
            "glm-4": (14.0, 14.0),
            "glm-4-flash": (0.1, 0.1),
            "glm-4-plus": (10.0, 10.0),
        }
        # 找到最匹配的模型价格
        input_price, output_price = 2.0, 8.0  # 默认
        for model_key, (ip, op) in pricing.items():
            if model_key in model:
                input_price, output_price = ip, op
                break
        return (prompt_tokens / 1_000_000) * input_price + (completion_tokens / 1_000_000) * output_price

    def get_usage_stats(self) -> Dict[str, Any]:
        """获取用量统计。"""
        return {
            "total_prompt_tokens": self._total_prompt_tokens,
            "total_completion_tokens": self._total_completion_tokens,
            "total_tokens_used": self._total_tokens_used,
            "total_cost_usd": round(self._total_cost, 6),
            "call_count": self._call_count,
            "model": self.model,
            "provider": self.provider,
        }

    def reset_usage(self):
        """重置用量统计。"""
        self._total_prompt_tokens = 0
        self._total_completion_tokens = 0
        self._total_tokens_used = 0
        self._total_cost = 0.0
        self._call_count = 0
        logger.info("用量统计已重置")

    # ------------------------------------------------------------------
    # 客户端初始化
    # ------------------------------------------------------------------

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
        elif self.provider == "zhipu":
            self._init_zhipu()
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

    def _init_zhipu(self):
        """初始化 Zhipu (智谱) GLM 客户端

        使用 OpenAI 兼容接口:
        - API base: https://open.bigmodel.cn/api/paas/v4/
        - 环境变量: ZHIPUAI_API_KEY
        """
        try:
            from openai import OpenAI
            import os

            api_key = self.api_key or os.environ.get("ZHIPUAI_API_KEY", "")
            if not api_key:
                raise ValueError(
                    "Zhipu API Key 未设置，请传入 api_key 或设置 ZHIPUAI_API_KEY 环境变量"
                )

            base_url = self.base_url or "https://open.bigmodel.cn/api/paas/v4/"
            self.base_url = base_url

            self._client = OpenAI(
                api_key=api_key,
                base_url=base_url,
            )
            if not self.model or self.model == "gpt-4":
                self.model = "glm-4-flash"
            logger.info(f"Zhipu GLM 客户端已初始化 (model={self.model}, url={base_url})")
        except ImportError:
            raise ImportError("请安装 openai: pip install openai")

    # ------------------------------------------------------------------
    # 核心 Chat 方法 (含重试)
    # ------------------------------------------------------------------

    async def _run_with_retry(self, func, *args, **kwargs):
        """
        带指数退避的通用重试包装器。

        重试策略:
        - 最多 self.max_retries 次 (默认 3)
        - 退避延迟: 1s, 2s, 4s, ...
        - 重试条件: 连接错误 / 速率限制 / 服务器错误
        """
        last_error = None
        for attempt in range(self.max_retries):
            try:
                return await func(*args, **kwargs)
            except Exception as e:
                last_error = e
                error_lower = str(e).lower()
                # 判断是否值得重试
                is_retryable = any(keyword in error_lower for keyword in (
                    "connection",
                    "timeout",
                    "rate_limit",
                    "rate limit",
                    "429",
                    "500",
                    "502",
                    "503",
                    "504",
                    "overloaded",
                    "capacity",
                ))
                if not is_retryable or attempt >= self.max_retries - 1:
                    raise
                delay = 1.0 * (2 ** attempt)  # 1s, 2s, 4s ...
                logger.warning(
                    f"LLM 调用第 {attempt + 1}/{self.max_retries} 次重试 "
                    f"(延迟 {delay:.1f}s): {e}"
                )
                await asyncio.sleep(delay)
        raise last_error  # type: ignore

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
            if self.provider in ("openai", "custom", "zhipu"):
                response = await self._run_with_retry(self._chat_openai, request_kwargs)
            elif self.provider == "anthropic":
                response = await self._run_with_retry(
                    self._chat_anthropic, messages, request_kwargs
                )
            elif self.provider == "ollama":
                response = await self._run_with_retry(self._chat_ollama, request_kwargs)
            else:
                return LLMResponse(success=False, error="未知提供商")

            # 记录用量
            if response.usage:
                self._record_usage(response.usage, response.model)

            return response

        except Exception as e:
            logger.error(f"LLM 调用失败: {e}")
            return LLMResponse(success=False, error=str(e))

    # ------------------------------------------------------------------
    # 提供商专属调用方法
    # ------------------------------------------------------------------

    async def _chat_openai(self, kwargs: dict) -> LLMResponse:
        """OpenAI / 兼容接口调用 (含 Zhipu)"""
        loop = asyncio.get_running_loop()
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
        loop = asyncio.get_running_loop()

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
        loop = asyncio.get_running_loop()

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

    # ------------------------------------------------------------------
    # 流式输出 (Streaming)
    # ------------------------------------------------------------------

    async def chat_stream(
        self,
        messages: List[Message],
        tools: Optional[List[Dict]] = None,
        tool_choice: str = "auto",
        response_format: Optional[Dict] = None,
        **kwargs,
    ) -> AsyncGenerator[str, None]:
        """
        流式聊天：逐 chunk yield 文本片段。

        Args:
            messages: 消息列表
            tools: 可用工具列表
            tool_choice: 工具选择策略
            response_format: 响应格式
            **kwargs: 额外参数

        Yields:
            str: 每次 yield 一个文本 chunk
        """
        self._ensure_client()

        msg_dicts = [m.to_dict() for m in messages]
        request_kwargs = {
            "model": self.model,
            "messages": msg_dicts,
            "temperature": kwargs.pop("temperature", self.temperature),
            "max_tokens": self.max_tokens,
            "stream": True,
        }
        if tools:
            request_kwargs["tools"] = tools
            request_kwargs["tool_choice"] = tool_choice
        if response_format:
            request_kwargs["response_format"] = response_format
        request_kwargs.update(kwargs)

        try:
            if self.provider in ("openai", "custom", "zhipu"):
                async for chunk in self._stream_openai(request_kwargs):
                    yield chunk
            elif self.provider == "anthropic":
                async for chunk in self._stream_anthropic(messages, request_kwargs):
                    yield chunk
            elif self.provider == "ollama":
                async for chunk in self._stream_ollama(request_kwargs):
                    yield chunk
            else:
                logger.error(f"流式调用不支持提供商: {self.provider}")
        except Exception as e:
            logger.error(f"流式 LLM 调用失败: {e}")

    async def _stream_openai(self, kwargs: dict) -> AsyncGenerator[str, None]:
        """OpenAI / 兼容接口 (含 Zhipu) 流式调用"""
        loop = asyncio.get_running_loop()

        def _create_stream():
            return self._client.chat.completions.create(**kwargs)

        stream = await loop.run_in_executor(None, _create_stream)

        # 使用迭代器在 executor 中逐步获取
        def _next_chunk(it):
            try:
                return next(it)
            except StopIteration:
                return None

        iterator = iter(stream)
        while True:
            chunk = await loop.run_in_executor(None, _next_chunk, iterator)
            if chunk is None:
                break
            if chunk.choices and chunk.choices[0].delta.content:
                yield chunk.choices[0].delta.content

    async def _stream_anthropic(
        self, messages: List[Message], kwargs: dict
    ) -> AsyncGenerator[str, None]:
        """Anthropic Claude 流式调用"""
        loop = asyncio.get_running_loop()

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
            "stream": True,
        }
        if system_msg:
            create_kwargs["system"] = system_msg

        def _create_stream():
            return self._client.messages.create(**create_kwargs)

        stream = await loop.run_in_executor(None, _create_stream)

        def _next_event(it):
            try:
                return next(it)
            except StopIteration:
                return None

        iterator = iter(stream)
        while True:
            event = await loop.run_in_executor(None, _next_event, iterator)
            if event is None:
                break
            if event.type == "content_block_delta":
                if hasattr(event.delta, "text"):
                    yield event.delta.text

    async def _stream_ollama(self, kwargs: dict) -> AsyncGenerator[str, None]:
        """Ollama 流式调用"""
        import requests
        loop = asyncio.get_running_loop()

        url = f"{self.base_url}/api/chat"
        payload = {
            "model": self.model,
            "messages": kwargs["messages"],
            "stream": True,
            "options": {
                "temperature": self.temperature,
                "num_predict": self.max_tokens,
            }
        }

        def _request_stream():
            return requests.post(url, json=payload, timeout=self.timeout, stream=True)

        resp = await loop.run_in_executor(None, _request_stream)
        resp.raise_for_status()

        import io
        buffer = ""

        def _read_chunk():
            nonlocal buffer
            # Read line by line from streaming response
            for line in resp.iter_lines():
                if not line:
                    continue
                decoded = line.decode("utf-8")
                try:
                    data = json.loads(decoded)
                    return data.get("message", {}).get("content", "")
                except json.JSONDecodeError:
                    continue
            return None

        while True:
            chunk = await loop.run_in_executor(None, _read_chunk)
            if chunk is None:
                break
            if chunk:
                yield chunk

    # ------------------------------------------------------------------
    # 同步包装
    # ------------------------------------------------------------------

    def chat_sync(
        self,
        messages: List[Message],
        **kwargs,
    ) -> LLMResponse:
        """同步版本聊天方法"""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                future = pool.submit(asyncio.run, self.chat(messages, **kwargs))
                return future.result()
        else:
            return asyncio.run(self.chat(messages, **kwargs))

    # ------------------------------------------------------------------
    # JSON 严格解析 (4 策略)
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_json_strict(text: str) -> Any:
        """
        使用多策略严格解析 JSON 字符串。
        复用 core.utils.safe_json_parse (3 策略: 直接/代码块/括号匹配)。

        Returns:
            解析后的 Python 对象，全部失败返回 None。
        """
        if not text:
            return None
        result = safe_json_parse(text)
        if result is not None:
            return result
        return None

    async def chat_json_strict(
        self,
        messages: List[Message],
        required_fields: Optional[List[str]] = None,
        max_retries: int = 2,
        **kwargs,
    ) -> Dict[str, Any]:
        """
        发送聊天请求并严格解析为 JSON 对象。

        特性:
        - 自动注入 JSON 模式系统指令
        - 使用 temperature=0 确保确定性
        - 4 策略严格解析
        - 失败自动重试 (默认 1 次)
        - 校验 required_fields

        Args:
            messages: 消息列表
            required_fields: 必需字段列表
            max_retries: 解析失败最大重试次数 (默认 2，即原始 + 1 次重试)
            **kwargs: 额外参数

        Returns:
            解析后的字典
        """
        # 注入 JSON 模式系统指令
        json_system = (
            "你必须且只能以合法 JSON 格式回复。"
            "不要包含任何多余文本、解释、说明或 markdown 标记。"
            "直接输出 JSON 对象或 JSON 数组。"
        )
        has_json_instruction = False
        for m in messages:
            if m.role == "system" and "json" in m.content.lower():
                has_json_instruction = True
                break

        if not has_json_instruction:
            messages = [
                Message(role="system", content=json_system),
                *messages,
            ]

        # 强制 temperature=0
        kwargs.setdefault("temperature", 0)

        last_error = ""
        for attempt in range(max_retries):
            response = await self.chat(messages, **kwargs)

            if not response.success:
                last_error = response.error
                logger.warning(
                    f"chat_json_strict 第 {attempt + 1}/{max_retries} 次 "
                    f"调用失败: {response.error}"
                )
                if attempt < max_retries - 1:
                    await asyncio.sleep(1.0)
                continue

            # 4 策略解析
            result = self._parse_json_strict(response.content)

            if result is not None:
                if isinstance(result, list):
                    result = {"items": result}
                if not isinstance(result, dict):
                    last_error = f"解析结果不是 dict/list，而是 {type(result).__name__}"
                    logger.warning(last_error)
                    if attempt < max_retries - 1:
                        await asyncio.sleep(1.0)
                    continue

                # 校验必需字段
                if required_fields:
                    missing = [f for f in required_fields if f not in result]
                    if missing:
                        last_error = f"缺少必需字段: {', '.join(missing)}"
                        logger.warning(
                            f"chat_json_strict 第 {attempt + 1}/{max_retries} 次: {last_error}"
                        )
                        # 追加提醒后重试
                        messages = [
                            *messages,
                            Message(
                                role="user",
                                content=(
                                    f"你的回复缺少必需字段: {', '.join(missing)}。"
                                    f"请重新以合法 JSON 格式回复，包含所有必需字段。"
                                ),
                            ),
                        ]
                        if attempt < max_retries - 1:
                            await asyncio.sleep(1.0)
                        continue

                return result

            last_error = "所有解析策略均失败"
            logger.warning(
                f"chat_json_strict 第 {attempt + 1}/{max_retries} 次: {last_error}"
            )
            # 追加提醒后重试
            messages = [
                *messages,
                Message(
                    role="user",
                    content="你的回复无法解析为 JSON。请只输出合法的 JSON，不要包含其他文本。",
                ),
            ]
            if attempt < max_retries - 1:
                await asyncio.sleep(1.0)

        return {"error": f"JSON 解析失败: {last_error}", "raw": response.content if 'response' in dir() else ""}

    # ------------------------------------------------------------------
    # 原有 JSON 方法 (保留向后兼容)
    # ------------------------------------------------------------------

    async def chat_json(
        self,
        messages: List[Message],
        required_fields: Optional[List[str]] = None,
        **kwargs,
    ) -> Dict[str, Any]:
        """
        发送聊天请求并解析为 JSON 对象。
        委托给 chat_json_strict() (更严格的 4 策略解析 + 重试)。
        添加 response_format=json_object 以利用 OpenAI 兼容的 JSON mode。
        """
        kwargs.setdefault("response_format", {"type": "json_object"})
        return await self.chat_json_strict(
            messages, required_fields=required_fields, max_retries=1, **kwargs
        )


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
