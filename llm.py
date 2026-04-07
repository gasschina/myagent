"""
LLM 接口层 - 统一模型调用
========================
支持 OpenAI / 智谱(Zhipu) / 自定义 API
统一请求/响应格式
"""
import json
import time
import logging
import re
import threading
from typing import Any, Dict, List, Optional, Callable
from dataclasses import dataclass, field

logger = logging.getLogger("myagent.llm")

from config import get_config


# ============================================================
# 数据模型
# ============================================================

@dataclass
class Message:
    """聊天消息"""
    role: str           # system / user / assistant / tool
    content: str = ""
    name: str = ""      # 发送者名称
    tool_call_id: str = ""
    tool_calls: List[Dict] = field(default_factory=list)

    def to_dict(self) -> Dict:
        d = {"role": self.role, "content": self.content}
        if self.name:
            d["name"] = self.name
        if self.tool_call_id:
            d["tool_call_id"] = self.tool_call_id
        if self.tool_calls:
            d["tool_calls"] = self.tool_calls
        return d


@dataclass
class ToolDefinition:
    """工具定义"""
    type: str = "function"
    function: Dict[str, Any] = field(default_factory=dict)

    @staticmethod
    def from_func(name: str, description: str, parameters: Dict) -> "ToolDefinition":
        return ToolDefinition(function={
            "name": name,
            "description": description,
            "parameters": parameters,
        })

    def to_dict(self) -> Dict:
        return {"type": self.type, "function": self.function}


@dataclass
class ChatResponse:
    """LLM 响应"""
    content: str = ""
    tool_calls: List[Dict] = field(default_factory=list)
    finish_reason: str = ""
    usage: Dict[str, int] = field(default_factory=dict)
    raw: Optional[Dict] = None
    model: str = ""
    duration_ms: float = 0


# ============================================================
# JSON 输出解析 (强校验)
# ============================================================

class JSONParser:
    """
    强校验 JSON 解析器
    确保从 LLM 输出中提取合法 JSON
    """

    @staticmethod
    def extract_json(text: str) -> Optional[Dict]:
        """
        从文本中提取 JSON 对象
        尝试多种策略:
        1. 直接解析
        2. 提取 ```json ... ``` 块
        3. 提取 { ... } 块
        4. 修复常见格式问题
        """
        text = text.strip()

        # 策略1: 直接解析
        try:
            result = json.loads(text)
            if isinstance(result, dict):
                return result
        except:
            pass

        # 策略2: 提取 markdown json 块
        patterns = [
            r'```json\s*\n(.*?)\n```',
            r'```\s*\n(.*?)\n```',
        ]
        for pattern in patterns:
            match = re.search(pattern, text, re.DOTALL)
            if match:
                try:
                    result = json.loads(match.group(1).strip())
                    if isinstance(result, dict):
                        return result
                except:
                    pass

        # 策略3: 提取最外层 { ... }
        brace_start = text.find('{')
        if brace_start >= 0:
            # 找到匹配的 }
            depth = 0
            for i in range(brace_start, len(text)):
                if text[i] == '{':
                    depth += 1
                elif text[i] == '}':
                    depth -= 1
                    if depth == 0:
                        json_str = text[brace_start:i+1]
                        try:
                            result = json.loads(json_str)
                            if isinstance(result, dict):
                                return result
                        except:
                            pass
                        break

        # 策略4: 尝试修复常见问题
        fixed = JSONParser._try_fix_json(text)
        if fixed:
            try:
                result = json.loads(fixed)
                if isinstance(result, dict):
                    return result
            except:
                pass

        return None

    @staticmethod
    def extract_json_array(text: str) -> Optional[List]:
        """提取 JSON 数组"""
        text = text.strip()
        # 直接解析
        try:
            result = json.loads(text)
            if isinstance(result, list):
                return result
        except:
            pass

        # 提取 [ ... ]
        bracket_start = text.find('[')
        if bracket_start >= 0:
            depth = 0
            for i in range(bracket_start, len(text)):
                if text[i] == '[':
                    depth += 1
                elif text[i] == ']':
                    depth -= 1
                    if depth == 0:
                        json_str = text[bracket_start:i+1]
                        try:
                            return json.loads(json_str)
                        except:
                            pass
                        break
        return None

    @staticmethod
    def _try_fix_json(text: str) -> Optional[str]:
        """尝试修复常见 JSON 格式问题"""
        fixed = text
        # 移除注释
        fixed = re.sub(r'//.*$', '', fixed, flags=re.MULTILINE)
        fixed = re.sub(r'/\*.*?\*/', '', fixed, flags=re.DOTALL)
        # 移除尾部逗号
        fixed = re.sub(r',\s*([}\]])', r'\1', fixed)
        # 移除控制字符
        fixed = re.sub(r'[\x00-\x1f\x7f]', '', fixed)
        return fixed

    @staticmethod
    def strict_parse(text: str, required_keys: Optional[List[str]] = None) -> Optional[Dict]:
        """
        严格解析 JSON，校验必要字段
        """
        obj = JSONParser.extract_json(text)
        if obj is None:
            return None
        if required_keys:
            for key in required_keys:
                if key not in obj:
                    return None
        return obj


# ============================================================
# LLM 客户端
# ============================================================

class LLMClient:
    """
    LLM 客户端 - 统一接口
    支持多种 provider，自动重试
    """

    def __init__(self, config_override: Optional[Dict] = None):
        cfg = get_config()
        llm_cfg = cfg.get("llm", {})

        if config_override:
            llm_cfg.update(config_override)

        self.provider = llm_cfg.get("provider", "openai")
        self.api_key = llm_cfg.get("api_key", "")
        self.api_base = llm_cfg.get("api_base", "https://api.openai.com/v1")
        self.model = llm_cfg.get("model", "gpt-4o")
        self.temperature = llm_cfg.get("temperature", 0.7)
        self.max_tokens = llm_cfg.get("max_tokens", 4096)
        self.timeout = llm_cfg.get("timeout", 120)
        self.max_retries = llm_cfg.get("max_retries", 3)
        self.retry_delay = llm_cfg.get("retry_delay", 5)

        # HTTP 客户端 (使用 urllib 避免额外依赖)
        import urllib.request
        import urllib.error
        self._urllib = urllib
        self._request_lock = threading.Lock()

        logger.info(f"LLM 初始化: provider={self.provider}, model={self.model}, base={self.api_base}")

    def chat(
        self,
        messages: List[Message],
        tools: Optional[List[ToolDefinition]] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        response_format: Optional[Dict] = None,
        **kwargs
    ) -> ChatResponse:
        """
        发送聊天请求

        参数:
            messages: 消息列表
            tools: 可用工具列表
            temperature: 温度
            max_tokens: 最大 token 数
            response_format: 响应格式 (如 {"type": "json_object"})
        """
        start_time = time.time()

        # 构建请求体
        body: Dict[str, Any] = {
            "model": self.model,
            "messages": [m.to_dict() for m in messages],
            "temperature": temperature or self.temperature,
            "max_tokens": max_tokens or self.max_tokens,
        }

        if tools:
            body["tools"] = [t.to_dict() for t in tools]
            body["tool_choice"] = "auto"

        if response_format:
            body["response_format"] = response_format

        body.update(kwargs)

        # 带重试的请求
        last_error = None
        for attempt in range(self.max_retries):
            try:
                result = self._do_request(body)
                duration = (time.time() - start_time) * 1000
                result.duration_ms = duration
                return result

            except Exception as e:
                last_error = e
                logger.warning(f"LLM 请求失败 (尝试 {attempt+1}/{self.max_retries}): {e}")
                if attempt < self.max_retries - 1:
                    time.sleep(self.retry_delay * (attempt + 1))

        return ChatResponse(
            content=f"LLM 请求失败: {str(last_error)}",
            finish_reason="error",
            duration_ms=(time.time() - start_time) * 1000,
        )

    def chat_simple(
        self,
        system_prompt: str,
        user_message: str,
        history: Optional[List[Dict]] = None,
        **kwargs
    ) -> str:
        """简化版聊天 - 直接返回文本"""
        messages = [Message(role="system", content=system_prompt)]
        if history:
            for h in history:
                messages.append(Message(role=h.get("role", "user"), content=h.get("content", "")))
        messages.append(Message(role="user", content=user_message))

        response = self.chat(messages, **kwargs)
        return response.content

    def chat_json(
        self,
        system_prompt: str,
        user_message: str,
        required_keys: Optional[List[str]] = None,
        history: Optional[List[Dict]] = None,
        **kwargs
    ) -> Optional[Dict]:
        """请求 JSON 格式响应"""
        json_prompt = system_prompt + "\n\n你必须以合法JSON格式回复，不要包含任何其他文本或markdown标记。"
        messages = [Message(role="system", content=json_prompt)]
        if history:
            for h in history:
                messages.append(Message(role=h.get("role", "user"), content=h.get("content", "")))
        messages.append(Message(role="user", content=user_message))

        response = self.chat(
            messages,
            response_format={"type": "json_object"},
            **kwargs
        )

        return JSONParser.strict_parse(response.content, required_keys)

    def _do_request(self, body: Dict) -> ChatResponse:
        """执行 HTTP 请求"""
        url = f"{self.api_base.rstrip('/')}/chat/completions"

        headers = {
            "Content-Type": "application/json",
        }

        # 根据 provider 设置认证头
        if self.provider == "openai" or self.provider == "custom":
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"
        elif self.provider == "zhipu":
            # 智谱 API 格式
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"
                # 智谱使用 generate 端点
                url = f"{self.api_base.rstrip('/')}/chat/completions"

        payload = json.dumps(body, ensure_ascii=False).encode('utf-8')

        req = self._urllib.request.Request(url, data=payload, headers=headers, method='POST')

        with self._request_lock:
            try:
                with self._urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    resp_body = resp.read().decode('utf-8')
                    data = json.loads(resp_body)

                    return self._parse_response(data)

            except self._urllib.error.HTTPError as e:
                error_body = ""
                try:
                    error_body = e.read().decode('utf-8')
                    error_data = json.loads(error_body)
                    error_msg = error_data.get("error", {}).get("message", str(e))
                except:
                    error_msg = f"HTTP {e.code}: {error_body[:500] if error_body else str(e)}"

                logger.error(f"LLM HTTP 错误: {error_msg}")
                raise Exception(error_msg)

            except self._urllib.error.URLError as e:
                raise Exception(f"网络错误: {e.reason}")

    def _parse_response(self, data: Dict) -> ChatResponse:
        """解析 API 响应"""
        try:
            choice = data["choices"][0]
            message = choice["message"]

            content = message.get("content", "") or ""
            tool_calls = message.get("tool_calls", [])
            finish_reason = choice.get("finish_reason", "")

            # 规范化 tool_calls
            normalized_calls = []
            if tool_calls:
                for tc in tool_calls:
                    func = tc.get("function", {})
                    normalized_calls.append({
                        "id": tc.get("id", ""),
                        "type": tc.get("type", "function"),
                        "function": {
                            "name": func.get("name", ""),
                            "arguments": func.get("arguments", "{}"),
                        }
                    })

            usage = data.get("usage", {})

            return ChatResponse(
                content=content,
                tool_calls=normalized_calls,
                finish_reason=finish_reason,
                usage={
                    "prompt_tokens": usage.get("prompt_tokens", 0),
                    "completion_tokens": usage.get("completion_tokens", 0),
                    "total_tokens": usage.get("total_tokens", 0),
                },
                raw=data,
                model=data.get("model", ""),
            )

        except (KeyError, IndexError) as e:
            logger.error(f"解析 LLM 响应失败: {e}, data={json.dumps(data)[:1000]}")
            return ChatResponse(
                content=f"解析响应失败: {e}",
                finish_reason="error",
                raw=data,
            )


# ============================================================
# 全局 LLM 客户端单例
# ============================================================

_global_llm: Optional[LLMClient] = None


def get_llm() -> LLMClient:
    """获取全局 LLM 客户端"""
    global _global_llm
    if _global_llm is None:
        _global_llm = LLMClient()
    return _global_llm


def init_llm(config_override: Optional[Dict] = None) -> LLMClient:
    """初始化全局 LLM 客户端"""
    global _global_llm
    _global_llm = LLMClient(config_override)
    return _global_llm
