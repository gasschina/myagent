"""
agents/main_agent.py - 主 Agent
=================================
总指挥 Agent，负责任务规划、Agent 调度、结果汇总。
"""
from __future__ import annotations

import json
import asyncio
from typing import Any, Dict, List, Optional

from core.logger import get_logger
from core.llm import LLMClient, LLMResponse, Message
from agents.base import BaseAgent, AgentContext
from core.utils import generate_id, timestamp, safe_json_parse, truncate_str

logger = get_logger("myagent.agent.main")


class MainAgent(BaseAgent):
    """
    主 Agent - 总指挥。

    职责:
      - 接收用户消息
      - 任务分析与规划
      - 调度子 Agent (ToolAgent / MemoryAgent)
      - 整合结果并回复
      - 多轮迭代(计划-执行-反思循环)
      - 确保不进入死循环
    """

    name = "main_agent"
    description = "AI助手主控Agent，负责理解用户意图、规划任务、调度执行"

    SYSTEM_PROMPT = """你是 MyAgent - 一个强大的本地桌面AI助手。你拥有以下能力:

## 核心能力
1. **代码执行**: 可以运行 Python、Shell/Bash、PowerShell 代码
2. **文件操作**: 读取、写入、搜索、管理文件
3. **网络搜索**: 搜索互联网、读取网页
4. **系统操作**: 查询系统信息、管理进程
5. **浏览器操作**: 自动化浏览器(如已安装 Playwright)
6. **记忆系统**: 记住用户偏好、历史任务、避免重复犯错

## 工作方式
- 仔细分析用户需求，拆解为可执行的步骤
- 使用可用工具完成任务
- 每一步执行后检查结果，遇到错误自动修复
- 完成后总结成果

## 重要规则
- 优先使用技能系统完成操作，而不是直接写代码
- 执行危险操作前先警告用户
- 保持回复简洁明了
- 如果需要多步操作，先规划再执行
- 用中文回复

## 格式要求
当你需要执行操作时，输出 JSON 格式:
```json
{
  "thought": "你的思考过程",
  "plan": ["步骤1", "步骤2"],
  "actions": [
    {"type": "skill", "name": "技能名", "params": {}},
    {"type": "code", "language": "python", "code": "代码"},
    {"type": "memory", "action": "记忆操作", "data": {}}
  ]
}
```

你可以用 markdown 格式回复普通对话。"""

    def __init__(self, tool_agent=None, memory_agent=None, **kwargs):
        super().__init__(**kwargs)
        self.tool_agent = tool_agent
        self.memory_agent = memory_agent
        self._iteration_count = 0

    async def process(self, context: AgentContext) -> AgentContext:
        """
        主处理循环。

        流程:
          1. 加载相关记忆
          2. 构建消息(系统提示 + 记忆上下文 + 对话历史 + 用户消息)
          3. 调用 LLM
          4. 解析响应(纯文本回复 / 工具调用)
          5. 如需执行工具，调度 ToolAgent
          6. 如需记忆操作，调度 MemoryAgent
          7. 循环直到任务完成或达到最大迭代次数
        """
        task_id = context.task_id or generate_id("task")
        context.task_id = task_id
        self._iteration_count = 0

        logger.info(f"[{task_id}] 开始处理用户请求: {context.user_message[:100]}")

        # Step 1: 加载相关记忆
        if self.memory_agent:
            mem_context = AgentContext(
                task_id=task_id,
                session_id=context.session_id,
                user_message=context.user_message,
                metadata={"memory_action": "get_relevant"},
            )
            await self.memory_agent.process(mem_context)
            if "memory_context_prompt" in mem_context.working_memory:
                context.working_memory["memory_context_prompt"] = \
                    mem_context.working_memory["memory_context_prompt"]

        # Step 2: 保存用户消息到短期记忆
        if self.memory:
            self.memory.add_short_term(
                session_id=context.session_id,
                role="user",
                content=context.user_message,
            )

        # Step 3: 主循环(计划-执行-反思)
        final_response = ""
        max_iter = self.config.get("agent", {}).get("max_iterations", 30) \
                   if isinstance(self.config, dict) else self.max_iterations

        while self._iteration_count < max_iter:
            self._iteration_count += 1
            logger.debug(f"[{task_id}] 迭代 {self._iteration_count}/{max_iter}")

            # 构建消息列表
            messages = self._build_messages(context)

            # 添加可用工具
            tools = self._get_tools()

            # 调用 LLM
            response = await self._call_llm(messages, tools=tools)

            if not response.success:
                final_response = f"⚠️ LLM 调用失败: {response.error}"
                break

            # 解析响应
            content = response.content

            # 检查是否有工具调用 (OpenAI function calling)
            if response.tool_calls:
                tool_results = await self._handle_tool_calls(
                    response.tool_calls, context, task_id
                )
                # 将工具结果加入消息历史
                for tc, result in tool_results:
                    context.conversation_history.append(
                        Message(role="tool", content=json.dumps(result, ensure_ascii=False),
                               tool_call_id=tc["id"], name=tc["name"])
                    )
                    context.conversation_history.append(
                        Message(role="assistant",
                               content=f"[已调用工具 {tc['name']}，结果: "
                                       f"{'成功' if result.get('success') else '失败'}]",
                               tool_calls=[tc])
                    )
                # 继续循环，让 LLM 处理工具结果
                continue

            # 尝试解析 JSON 操作指令
            action_data = safe_json_parse(content)

            if action_data and isinstance(action_data, dict):
                # 有结构化的操作指令
                if "actions" in action_data:
                    # 执行操作列表
                    results = await self._execute_actions(
                        action_data, context, task_id
                    )
                    # 将结果反馈给 LLM
                    context.conversation_history.append(
                        Message(role="assistant", content=content)
                    )
                    result_summary = self._summarize_action_results(results)
                    context.conversation_history.append(
                        Message(role="user",
                               content=f"[执行结果]\n{result_summary}\n\n请基于以上结果继续。")
                    )

                    # 如果任务已完成(所有操作成功)，退出循环
                    all_success = all(r.get("success", False) for r in results)
                    if all_success and results:
                        final_response = action_data.get("thought", "")
                        if "plan" in action_data and action_data["plan"]:
                            final_response += "\n\n已完成: " + " → ".join(action_data["plan"])
                        break

                    continue

                # 单个 action
                if action_data.get("type") == "final_answer":
                    final_response = action_data.get("content", content)
                    break

            # 纯文本回复(没有工具调用)
            final_response = content
            break

        # 保存助手回复到短期记忆
        if self.memory and final_response:
            self.memory.add_short_term(
                session_id=context.session_id,
                role="assistant",
                content=final_response,
            )

        # 清理工作记忆
        context.working_memory["final_response"] = final_response
        context.working_memory["iterations"] = self._iteration_count

        logger.info(f"[{task_id}] 处理完成 (迭代 {self._iteration_count} 次)")

        # 检查是否需要总结对话
        if self.memory_agent and self._iteration_count > 5:
            try:
                await self.memory_agent.process(AgentContext(
                    session_id=context.session_id,
                    metadata={"memory_action": "summarize"},
                ))
            except Exception as e:
                logger.warning(f"对话总结失败: {e}")

        return context

    def _build_messages(self, context: AgentContext) -> List[Message]:
        """构建完整的消息列表"""
        messages = [Message(role="system", content=self.SYSTEM_PROMPT)]

        # 添加记忆上下文
        memory_ctx = context.working_memory.get("memory_context_prompt", "")
        if memory_ctx:
            messages.append(Message(
                role="system",
                content=f"[记忆上下文]\n{memory_ctx}",
            ))

        # 添加对话历史
        for msg in context.conversation_history[-20:]:
            messages.append(msg)

        # 添加当前用户消息
        messages.append(Message(role="user", content=context.user_message))

        return messages

    def _get_tools(self) -> Optional[List[Dict]]:
        """获取可用工具列表(OpenAI function calling 格式)"""
        if not self.skills:
            return None
        try:
            return self.skills.get_all_schemas()
        except Exception as e:
            logger.warning(f"获取工具列表失败: {e}")
            return None

    async def _handle_tool_calls(
        self,
        tool_calls: List[Dict],
        context: AgentContext,
        task_id: str,
    ) -> List[tuple]:
        """处理 OpenAI function calling 工具调用"""
        results = []
        for tc in tool_calls:
            name = tc["name"]
            args = tc.get("arguments", {})
            logger.info(f"[{task_id}] 调用工具: {name}({args})")

            try:
                if self.skills:
                    result = await self.skills.execute(name, **args)
                    result_dict = result.to_dict()
                else:
                    result_dict = {"success": False, "error": "技能系统未初始化"}
            except Exception as e:
                result_dict = {"success": False, "error": str(e)}

            results.append((tc, result_dict))

            # 记录错误模式
            if not result_dict.get("success") and self.memory:
                self.memory.record_error_pattern(
                    error=f"工具 {name} 失败: {result_dict.get('error', '')}",
                )

        return results

    async def _execute_actions(
        self,
        action_data: Dict,
        context: AgentContext,
        task_id: str,
    ) -> List[Dict]:
        """执行操作列表"""
        results = []
        actions = action_data.get("actions", [])

        for action in actions:
            action_type = action.get("type", "")

            if action_type == "skill" and self.skills:
                result = await self.skills.execute(
                    action.get("name", ""),
                    **action.get("params", {}),
                )
                results.append(result.to_dict())

            elif action_type == "code" and self.executor:
                exec_result = await self.executor.execute(
                    language=action.get("language", "python"),
                    code=action.get("code", ""),
                )
                results.append(exec_result.to_dict())

            elif action_type == "memory" and self.memory_agent:
                mem_ctx = AgentContext(
                    task_id=task_id,
                    session_id=context.session_id,
                    metadata={
                        "memory_action": action.get("action", ""),
                        **action.get("data", {}),
                    },
                )
                await self.memory_agent.process(mem_ctx)
                results.append({"success": True, "action": "memory"})

            elif action_type == "final":
                break

            else:
                results.append({
                    "success": False,
                    "error": f"未知操作类型: {action_type}",
                })

        return results

    def _summarize_action_results(self, results: List[Dict]) -> str:
        """汇总操作结果"""
        if not results:
            return "无操作结果"

        parts = []
        for i, r in enumerate(results, 1):
            status = "✅" if r.get("success") else "❌"
            msg = r.get("message", r.get("error", ""))[:200]
            parts.append(f"{i}. {status} {msg}")

        return "\n".join(parts)
