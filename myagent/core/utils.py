"""
core/utils.py - 通用工具函数
============================
"""
from __future__ import annotations

import json
import uuid
import time
import asyncio
import functools
import re
from datetime import datetime, timezone
from typing import Any, Dict, Optional, TypeVar, Callable, Coroutine

T = TypeVar("T")


def timestamp() -> str:
    """返回 ISO 8601 格式时间戳"""
    return datetime.now(timezone.utc).isoformat()


def timestamp_ms() -> int:
    """返回毫秒级时间戳"""
    return int(time.time() * 1000)


def generate_id(prefix: str = "") -> str:
    """生成唯一 ID"""
    uid = uuid.uuid4().hex[:12]
    return f"{prefix}_{uid}" if prefix else uid


def truncate_str(text: str, max_length: int = 50000) -> str:
    """截断过长的文本"""
    if len(text) <= max_length:
        return text
    return text[:max_length] + f"\n... [截断，共 {len(text)} 字符]"


def safe_json_parse(text: str, default: Any = None) -> Any:
    """
    安全解析 JSON，支持从 LLM 输出中提取 JSON 块。
    尝试策略:
      1. 直接解析
      2. 提取 ```json ... ``` 代码块
      3. 提取最外层 { ... } 或 [ ... ]
    """
    if not text:
        return default

    # 策略 1: 直接解析
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 策略 2: 提取 markdown JSON 代码块
    json_block_pattern = r"```(?:json)?\s*\n?(.*?)\n?```"
    matches = re.findall(json_block_pattern, text, re.DOTALL)
    for match in matches:
        try:
            return json.loads(match.strip())
        except json.JSONDecodeError:
            continue

    # 策略 3: 提取第一个 { ... } 或 [ ... ]
    for opener, closer in [("{", "}"), ("[", "]")]:
        start = text.find(opener)
        if start == -1:
            continue
        depth = 0
        for i in range(start, len(text)):
            if text[i] == opener:
                depth += 1
            elif text[i] == closer:
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:i + 1])
                    except json.JSONDecodeError:
                        break

    return default


def validate_json_schema(data: dict, required_fields: list) -> tuple[bool, str]:
    """
    校验 JSON 数据是否包含必需字段。

    Returns:
        (是否有效, 错误信息)
    """
    missing = [f for f in required_fields if f not in data]
    if missing:
        return False, f"缺少必需字段: {', '.join(missing)}"
    return True, ""


def format_execution_result(
    success: bool,
    output: str = "",
    error: str = "",
    metadata: Optional[dict] = None,
) -> dict:
    """格式化执行结果为标准结构"""
    return {
        "success": success,
        "output": truncate_str(output),
        "error": truncate_str(error) if error else "",
        "metadata": metadata or {},
        "timestamp": timestamp(),
    }


def run_async(coro: Coroutine) -> Any:
    """
    在已有事件循环中安全地运行异步协程。
    如果已在 async 上下文中，创建新线程运行。
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor() as pool:
            future = pool.submit(asyncio.run, coro)
            return future.result()
    else:
        return asyncio.run(coro)


def retry_async(
    func: Callable,
    max_retries: int = 3,
    delay: float = 1.0,
    backoff: float = 2.0,
    logger=None,
):
    """
    异步重试装饰器。

    Args:
        func: 异步函数
        max_retries: 最大重试次数
        delay: 初始延迟(秒)
        backoff: 退避因子
        logger: 日志器
    """
    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        last_error = None
        for attempt in range(max_retries):
            try:
                return await func(*args, **kwargs)
            except Exception as e:
                last_error = e
                if logger:
                    logger.warning(f"第 {attempt + 1}/{max_retries} 次重试 ({func.__name__}): {e}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(delay * (backoff ** attempt))
        raise last_error  # type: ignore
    return wrapper


def detect_platform() -> str:
    """检测当前操作系统平台"""
    import platform
    system = platform.system().lower()
    if system == "windows":
        return "windows"
    elif system == "darwin":
        return "macos"
    elif system == "linux":
        return "linux"
    return "unknown"


def get_shell_command() -> str:
    """根据平台返回默认 shell"""
    platform_name = detect_platform()
    if platform_name == "windows":
        return "powershell"
    return "bash"


def sanitize_filename(name: str) -> str:
    """清理文件名，移除非法字符"""
    return re.sub(r'[<>:"/\\|?*]', '_', name).strip()
