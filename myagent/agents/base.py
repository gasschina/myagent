"""
agents/base.py - Agent 基类
============================
定义所有 Agent 的基础接口和通用能力。
"""
from __future__ import annotations

import json
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Callable

from core.logger import get_logger
from core.llm import LLMClient, LLMResponse, Message
from core.utils import generate_id, timestamp

logger = get_logger("myagent.agent")


@dataclass
class AgentContext:
    """Agent 上下文，在 Agent 之间传递"""
    task_id: str = ""
    session_id: str = "default"
    user_message: str = ""
    conversation_history: List[Message] = field(default_factory=list)
    working_memory: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)
    callbacks: Dict[str, Callable] = field(default_factory=dict)


class BaseAgent(ABC):
    """
    Agent 基类。

    所有 Agent 继承此类，实现 process 方法。
    提供 LLM 调用、消息构建、错误处理等通用能力。
    """

    name: str = "base_agent"
    description: str = "基础 Agent"
    max_iterations: int = 30

    def __init__(
        self,
        llm: Optional[LLMClient] = None,
        memory_manager=None,
        executor=None,
        skill_registry=None,
        task_queue=None,
        config=None,
    ):
        self.llm = llm
        self.memory = memory_manager
        self.executor = executor
        self.skills = skill_registry
        self.task_queue = task_queue
        self.config = config or {}
        self._stats = {"total_tasks": 0, "success": 0, "failed": 0}

    @abstractmethod
    async def process(self, context: AgentContext) -> AgentContext:
        """
        处理任务(子类必须实现)。

        Args:
            context: Agent 上下文

        Returns:
            更新后的 AgentContext
        """
        pass

    def _build_system_prompt(self) -> str:
        """构建系统提示词(子类可重写)"""
        return f"你是 {self.name}，{self.description}。"

    async def _call_llm(
        self,
        messages: List[Message],
        tools: Optional[List[Dict]] = None,
        **kwargs,
    ) -> LLMResponse:
        """调用 LLM"""
        if not self.llm:
            return LLMResponse(success=False, error="LLM 客户端未初始化")

        response = await self.llm.chat(messages, tools=tools, **kwargs)
        if not response.success:
            logger.error(f"{self.name} LLM 调用失败: {response.error}")
        return response

    async def _call_llm_json(self, messages: List[Message], **kwargs) -> Dict[str, Any]:
        """调用 LLM 并获取 JSON 响应"""
        if not self.llm:
            return {"error": "LLM 客户端未初始化"}
        return await self.llm.chat_json(messages, **kwargs)

    def _message(self, role: str, content: str, **kwargs) -> Message:
        """快捷创建消息"""
        return Message(role=role, content=content, **kwargs)

    def update_stats(self, success: bool):
        """更新统计"""
        self._stats["total_tasks"] += 1
        if success:
            self._stats["success"] += 1
        else:
            self._stats["failed"] += 1

    def get_stats(self) -> Dict:
        return dict(self._stats)
