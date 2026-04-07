"""
agents/memory_agent.py - 记忆 Agent
=====================================
负责读写记忆、总结经验、检索历史知识。
主 Agent 通过 MemoryAgent 间接操作记忆系统。
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from core.logger import get_logger
from core.llm import Message
from agents.base import BaseAgent, AgentContext
from core.utils import timestamp

logger = get_logger("myagent.agent.memory")


class MemoryAgent(BaseAgent):
    """
    记忆管理 Agent。

    职责:
      - 对话上下文管理(短期记忆)
      - 任务进度跟踪(工作记忆)
      - 知识/经验存储(长期记忆)
      - 记忆检索与总结
      - 错误模式记录(避免重复犯错)
    """

    name = "memory_agent"
    description = "负责记忆管理和知识检索的专业Agent"

    # 记忆总结系统提示词
    SUMMARY_PROMPT = """你是一个记忆总结专家。请将以下对话历史总结为简洁的知识要点。

要求:
1. 提取关键信息: 任务目标、执行步骤、结果、教训
2. 标记重要偏好和约束
3. 记录有用的代码片段和命令
4. 识别错误和修复方法
5. 输出 JSON 格式:
{
  "summary": "一句话总结",
  "key_points": ["要点1", "要点2"],
  "preferences": {"偏好领域": "偏好值"},
  "errors_learned": [{"error": "错误描述", "fix": "修复方法"}],
  "useful_commands": ["命令1", "命令2"],
  "importance": 0.8
}"""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._auto_summarize = True

    async def process(self, context: AgentContext) -> AgentContext:
        """
        处理记忆相关请求。

        通过 context.metadata["memory_action"] 指定操作:
          - save_conversation: 保存对话到短期记忆
          - get_context: 获取当前对话上下文
          - save_progress: 保存工作进度
          - get_progress: 获取工作进度
          - search: 搜索记忆
          - summarize: 总结当前对话
          - record_error: 记录错误模式
          - save_preference: 保存用户偏好
          - get_relevant: 获取与当前任务相关的历史记忆
        """
        action = context.metadata.get("memory_action", "")
        session_id = context.session_id

        try:
            if action == "save_conversation":
                await self._save_conversation(context, session_id)
            elif action == "get_context":
                await self._get_context(context, session_id)
            elif action == "save_progress":
                await self._save_progress(context, session_id)
            elif action == "get_progress":
                await self._get_progress(context, session_id)
            elif action == "search":
                await self._search(context, session_id)
            elif action == "summarize":
                await self._summarize(context, session_id)
            elif action == "record_error":
                await self._record_error(context, session_id)
            elif action == "save_preference":
                await self._save_preference(context, session_id)
            elif action == "get_relevant":
                await self._get_relevant(context, session_id)
            else:
                logger.warning(f"未知的记忆操作: {action}")

        except Exception as e:
            logger.error(f"记忆 Agent 处理失败: {e}")
            context.working_memory["memory_error"] = str(e)

        return context

    async def _save_conversation(self, context: AgentContext, session_id: str):
        """保存对话消息到短期记忆"""
        if not self.memory:
            return

        for msg in context.conversation_history:
            if msg.role in ("user", "assistant", "system", "tool"):
                # 避免重复保存
                self.memory.add_short_term(
                    session_id=session_id,
                    role=msg.role,
                    content=msg.content,
                    key=f"conv_{msg.role}",
                )

        # 修剪旧消息
        max_msgs = self.config.get("memory", {}).get("max_short_term", 50)
        self.memory.prune_conversation(session_id, max_msgs)

        logger.debug(f"对话已保存到短期记忆 (session={session_id})")

    async def _get_context(self, context: AgentContext, session_id: str):
        """获取对话上下文"""
        if not self.memory:
            return

        entries = self.memory.get_conversation(session_id, limit=50)
        context.working_memory["conversation_context"] = [
            {"role": e.role, "content": e.content} for e in entries
        ]
        logger.debug(f"已加载对话上下文: {len(entries)} 条")

    async def _save_progress(self, context: AgentContext, session_id: str):
        """保存任务进度到工作记忆"""
        if not self.memory:
            return

        progress = context.metadata.get("progress_data", {})
        for key, value in progress.items() if isinstance(progress, dict) else []:
            self.memory.add_working(
                session_id=session_id,
                key=key,
                content=str(value) if not isinstance(value, str) else value,
            )

        # 也保存整体状态
        task_status = context.working_memory.get("task_status", "进行中")
        self.memory.add_working(
            session_id=session_id,
            key="task_status",
            content=task_status,
            metadata=context.working_memory,
        )
        logger.debug(f"工作进度已保存 (session={session_id})")

    async def _get_progress(self, context: AgentContext, session_id: str):
        """获取工作进度"""
        if not self.memory:
            return

        entries = self.memory.get_working(session_id)
        context.working_memory["task_progress"] = [
            {"key": e.key, "content": e.content, "metadata": e.metadata}
            for e in entries
        ]
        logger.debug(f"已加载工作进度: {len(entries)} 条")

    async def _search(self, context: AgentContext, session_id: str):
        """搜索记忆"""
        if not self.memory:
            return

        query = context.metadata.get("search_query", context.user_message)
        category = context.metadata.get("search_category", "")
        limit = context.metadata.get("search_limit", 10)

        results = self.memory.search(query, session_id=session_id,
                                     category=category, limit=limit)
        context.working_memory["search_results"] = [
            {
                "id": e.id,
                "category": e.category,
                "key": e.key,
                "content": e.content[:2000],
                "summary": e.summary,
                "importance": e.importance,
                "created_at": e.created_at,
            }
            for e in results
        ]

    async def _summarize(self, context: AgentContext, session_id: str):
        """总结当前对话"""
        if not self.memory or not self.llm:
            return

        entries = self.memory.get_recent_for_summary(session_id)
        if len(entries) < 5:
            logger.debug("对话太短，不需要总结")
            return

        # 构建对话文本
        conv_text = "\n".join(
            f"[{e.role}] {e.content}" for e in entries
        )

        messages = [
            Message(role="system", content=self.SUMMARY_PROMPT),
            Message(role="user", content=f"请总结以下对话:\n\n{conv_text}"),
        ]

        result = await self._call_llm_json(messages)
        if "error" not in result:
            self.memory.save_summary(
                session_id=session_id,
                summary=result.get("summary", ""),
            )

            # 保存偏好
            preferences = result.get("preferences", {})
            for key, value in preferences.items():
                self.memory.add_long_term(
                    session_id="global",
                    key="user_pref",
                    content=f"{key}: {value}",
                    importance=0.7,
                )

            # 保存错误经验
            for err_item in result.get("errors_learned", []):
                self.memory.record_error_pattern(
                    error=err_item.get("error", ""),
                    fix=err_item.get("fix", ""),
                )

            logger.info(f"对话已总结并保存到长期记忆 (session={session_id})")
            # 清理已总结的旧对话
            self.memory.clear_conversation(session_id)

    async def _record_error(self, context: AgentContext, session_id: str):
        """记录错误模式"""
        if not self.memory:
            return

        error = context.metadata.get("error", "")
        fix = context.metadata.get("fix", "")
        if error:
            self.memory.record_error_pattern(error=error, fix=fix,
                                              session_id=session_id)

    async def _save_preference(self, context: AgentContext, session_id: str):
        """保存用户偏好"""
        if not self.memory:
            return

        pref_key = context.metadata.get("pref_key", "")
        pref_value = context.metadata.get("pref_value", "")
        if pref_key and pref_value:
            self.memory.add_long_term(
                session_id="global",
                key="user_pref",
                content=f"{pref_key}: {pref_value}",
                summary=f"{pref_key}={pref_value}",
                importance=0.7,
            )

    async def _get_relevant(self, context: AgentContext, session_id: str):
        """获取与当前任务相关的历史记忆"""
        if not self.memory:
            return

        query = context.user_message

        # 搜索长期记忆
        long_term = self.memory.search(
            query, session_id="", category="long_term", limit=5
        )
        # 搜索错误模式
        errors = self.memory.get_error_patterns(session_id="global", limit=3)
        # 搜索偏好
        prefs = self.memory.get_preferences(session_id="global")

        context.working_memory["relevant_memories"] = {
            "long_term": [
                {"content": e.content[:500], "key": e.key, "summary": e.summary}
                for e in long_term
            ],
            "error_patterns": [
                {"content": e.content[:300]} for e in errors
            ],
            "preferences": [
                {"content": e.content[:200]} for e in prefs[-5:]
            ],
        }

        # 构建上下文提示
        context_parts = []
        if prefs:
            context_parts.append("## 用户偏好")
            context_parts.extend(f"- {e.content}" for e in prefs[-5:])

        if errors:
            context_parts.append("\n## 历史错误(避免重复)")
            context_parts.extend(f"- {e.content[:200]}" for e in errors)

        if long_term:
            context_parts.append("\n## 相关经验")
            context_parts.extend(f"- {e.summary or e.content[:200]}" for e in long_term)

        if context_parts:
            context.working_memory["memory_context_prompt"] = "\n".join(context_parts)
