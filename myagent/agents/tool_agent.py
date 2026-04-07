"""
agents/tool_agent.py - 工具 Agent
===================================
负责系统执行、文件操作、技能调用。
主 Agent 通过 ToolAgent 间接调用执行引擎和技能系统。
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from core.logger import get_logger
from core.llm import Message
from agents.base import BaseAgent, AgentContext
from core.utils import timestamp, safe_json_parse, truncate_str

logger = get_logger("myagent.agent.tool")


class ToolAgent(BaseAgent):
    """
    工具执行 Agent。

    职责:
      - 执行代码(Python/Shell/PowerShell)
      - 调用技能系统
      - 文件操作
      - 系统命令
      - 结果格式化和错误恢复
    """

    name = "tool_agent"
    description = "负责代码执行和工具调用的专业Agent"

    # 系统提示词
    SYSTEM_PROMPT = """你是一个精准的代码执行助手。你的任务是根据指令执行代码或调用工具，并返回结构化结果。

重要规则:
1. 严格按要求执行，不要过度发挥
2. 代码必须安全、可靠
3. 如果执行失败，分析错误并尝试修复
4. 结果必须结构化返回(JSON格式)
5. 对于危险操作，先确认再执行

输出格式(JSON):
{
  "action": "execute_code | call_skill | analyze_result",
  "language": "python | shell | powershell",
  "code": "要执行的代码",
  "skill_name": "技能名称",
  "skill_params": {},
  "analysis": "结果分析",
  "suggestion": "下一步建议"
}"""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._execution_log: List[Dict] = []

    async def process(self, context: AgentContext) -> AgentContext:
        """
        处理工具执行请求。

        通过 context.metadata["tool_action"] 指定操作:
          - execute: 执行代码
          - call_skill: 调用技能
          - auto: 由LLM自动决定(默认)
        """
        action = context.metadata.get("tool_action", "auto")

        try:
            if action == "execute":
                result = await self._execute_direct(context)
            elif action == "call_skill":
                result = await self._call_skill_direct(context)
            elif action == "auto":
                result = await self._auto_execute(context)
            else:
                result = {"error": f"未知工具操作: {action}"}

            context.working_memory["tool_result"] = result

            # 记录执行日志
            self._execution_log.append({
                "timestamp": timestamp(),
                "action": action,
                "success": result.get("success", False),
                "task_id": context.task_id,
            })

        except Exception as e:
            logger.error(f"工具 Agent 执行失败: {e}")
            context.working_memory["tool_result"] = {
                "success": False,
                "error": f"工具执行异常: {str(e)}",
            }

        return context

    async def _execute_direct(self, context: AgentContext) -> Dict:
        """直接执行代码"""
        language = context.metadata.get("language", "python")
        code = context.metadata.get("code", "")

        if not code:
            return {"success": False, "error": "未提供代码"}

        return await self._run_code(language, code)

    async def _call_skill_direct(self, context: AgentContext) -> Dict:
        """直接调用技能"""
        skill_name = context.metadata.get("skill_name", "")
        skill_params = context.metadata.get("skill_params", {})

        if not skill_name:
            return {"success": False, "error": "未指定技能名称"}

        return await self._invoke_skill(skill_name, skill_params)

    async def _auto_execute(self, context: AgentContext) -> Dict:
        """由 LLM 自动决定执行方式"""
        user_msg = context.user_message

        # 如果上下文已有工具调用结果，进行分析
        if "tool_result" in context.working_memory:
            return await self._analyze_result(context)

        # 检查是否是技能调用请求
        if self.skills and user_msg.startswith("!"):
            parts = user_msg[1:].split(maxsplit=1)
            skill_name = parts[0]
            param_str = parts[1] if len(parts) > 1 else "{}"
            try:
                params = json.loads(param_str) if param_str.startswith("{") else {"query": param_str}
            except json.JSONDecodeError:
                params = {"query": param_str}
            return await self._invoke_skill(skill_name, params)

        # 让 LLM 决定如何执行
        messages = [
            Message(role="system", content=self.SYSTEM_PROMPT),
        ]

        # 添加对话历史
        for msg in context.conversation_history[-10:]:
            messages.append(msg)

        messages.append(Message(role="user", content=user_msg))

        # 获取可用技能列表
        if self.skills:
            available_skills = self.skills.list_skills_info()
            skills_desc = "\n".join(
                f"- {s['name']}: {s['description']}" for s in available_skills
            )
            messages.append(Message(
                role="system",
                content=f"可用技能列表:\n{skills_desc}\n\n"
                        f"你也可以直接执行 Python/Shell/PowerShell 代码。",
            ))

        # 使用 JSON 模式
        result = await self._call_llm_json(
            messages, required_fields=["action"]
        )

        if "error" in result:
            return {"success": False, "error": result["error"]}

        action = result.get("action", "")

        if action == "execute_code":
            language = result.get("language", "python")
            code = result.get("code", "")
            return await self._run_code(language, code)

        elif action == "call_skill":
            skill_name = result.get("skill_name", "")
            skill_params = result.get("skill_params", {})
            return await self._invoke_skill(skill_name, skill_params)

        elif action == "analyze_result":
            # 如果LLM只是分析，返回分析结果
            return {
                "success": True,
                "data": result,
                "message": result.get("analysis", result.get("suggestion", "")),
            }

        else:
            return {"success": False, "error": f"未知 action: {action}"}

    async def _run_code(self, language: str, code: str) -> Dict:
        """执行代码"""
        if not self.executor:
            return {"success": False, "error": "执行引擎未初始化"}

        exec_result = await self.executor.execute(
            language=language,
            code=code,
        )

        return {
            "success": exec_result.success,
            "stdout": exec_result.stdout,
            "stderr": exec_result.stderr,
            "error": exec_result.error,
            "exit_code": exec_result.exit_code,
            "execution_time": exec_result.execution_time,
            "language": language,
            "llm_message": exec_result.to_llm_message(),
        }

    async def _invoke_skill(self, skill_name: str, params: Dict) -> Dict:
        """调用技能"""
        if not self.skills:
            return {"success": False, "error": "技能系统未初始化"}

        skill_result = await self.skills.execute(skill_name, **params)
        return skill_result.to_dict()

    async def _analyze_result(self, context: AgentContext) -> Dict:
        """分析执行结果"""
        prev_result = context.working_memory.get("tool_result", {})
        user_msg = context.user_message

        messages = [
            Message(
                role="system",
                content="你是一个执行结果分析专家。请分析以下执行结果，给出下一步建议。"
                        "输出 JSON 格式:\n"
                        '{"analysis": "分析结论", "success": true/false, '
                        '"suggestion": "下一步建议", "fixed_code": "修复后的代码(如需)"}'
            ),
            Message(role="user", content=f"执行结果:\n{json.dumps(prev_result, ensure_ascii=False, indent=2)}\n\n"
                                         f"用户请求: {user_msg}"),
        ]

        result = await self._call_llm_json(messages)
        return result

    def get_execution_log(self, limit: int = 50) -> List[Dict]:
        """获取执行日志"""
        return self._execution_log[-limit:]

    def clear_execution_log(self):
        """清空执行日志"""
        self._execution_log.clear()
