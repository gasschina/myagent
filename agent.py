"""
多Agent架构 - 主控制器
========================
- MasterAgent: 总指挥、任务规划、用户交互
- ToolAgent: 负责系统执行、文件操作、技能调用
- MemoryAgent: 负责读写记忆、总结经验、错误学习

统一调度，避免冲突、死循环、重复执行
"""
import json
import time
import uuid
import logging
import threading
from typing import Any, Dict, List, Optional, Callable
from dataclasses import dataclass, field
from enum import Enum
from datetime import datetime

from config import get_config
from memory import (
    MemoryManager, MemoryStore, MemoryItem, MemoryType,
    TaskProgress
)
from executor import Executor, ExecutionResult, get_executor
from llm import (
    LLMClient, Message, ToolDefinition, ChatResponse,
    JSONParser, get_llm
)
from skills import SkillRegistry, get_skill_registry

logger = logging.getLogger("myagent.agent")


# ============================================================
# Agent 通信协议
# ============================================================

class AgentRole(Enum):
    MASTER = "master"
    TOOL = "tool"
    MEMORY = "memory"


class PlanStepStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class PlanStep:
    """计划步骤"""
    step_id: str = ""
    index: int = 0
    description: str = ""
    tool_name: str = ""
    arguments: Dict = field(default_factory=dict)
    status: str = PlanStepStatus.PENDING.value
    result: Any = None
    error: str = ""
    retries: int = 0


@dataclass
class TaskPlan:
    """任务计划"""
    task_id: str = ""
    description: str = ""
    steps: List[PlanStep] = field(default_factory=list)
    current_index: int = 0
    status: str = "planning"


# ============================================================
# 工具 Agent - 负责执行
# ============================================================

class ToolAgent:
    """
    工具 Agent
    职责:
    - 执行代码 (Python/Shell/PowerShell)
    - 调用技能系统
    - 返回结构化结果
    """

    def __init__(
        self,
        executor: Optional[Executor] = None,
        skill_registry: Optional[SkillRegistry] = None,
    ):
        self.executor = executor or get_executor()
        self.skill_registry = skill_registry or get_skill_registry()
        self._call_history: List[Dict] = []

    def execute_tool_call(self, tool_name: str, arguments: Dict) -> Dict:
        """
        执行工具调用

        参数:
            tool_name: 工具名称
            arguments: 调用参数

        返回:
            结构化结果字典
        """
        start = time.time()

        # 先检查是否为已注册技能
        skill_handler = self.skill_registry.get_handler(tool_name)
        if skill_handler:
            logger.info(f"ToolAgent: 调用技能 [{tool_name}]")
            try:
                result = skill_handler(arguments)
                duration = time.time() - start
                record = {
                    "tool": tool_name,
                    "arguments": arguments,
                    "result": result,
                    "duration": round(duration, 3),
                    "success": not isinstance(result, dict) or "error" not in result,
                }
                self._call_history.append(record)
                return result
            except Exception as e:
                logger.error(f"ToolAgent: 技能执行失败 [{tool_name}]: {e}")
                return {"error": str(e)}

        # 检查是否为代码执行类型
        exec_map = {
            "run_code": lambda args: self.executor.execute(
                args.get("code", ""), args.get("language", "auto"),
                args.get("work_dir"), args.get("timeout")
            ).to_llm_dict(),
            "run_python": lambda args: self.executor.execute(
                args.get("code", ""), "python",
                args.get("work_dir"), args.get("timeout")
            ).to_llm_dict(),
            "run_shell": lambda args: self.executor.execute(
                args.get("code", ""), "shell",
                args.get("work_dir"), args.get("timeout")
            ).to_llm_dict(),
            "run_powershell": lambda args: self.executor.execute(
                args.get("code", ""), "powershell",
                args.get("work_dir"), args.get("timeout")
            ).to_llm_dict(),
        }

        if tool_name in exec_map:
            logger.info(f"ToolAgent: 执行代码 [{tool_name}]")
            try:
                result = exec_map[tool_name](arguments)
                duration = time.time() - start
                self._call_history.append({
                    "tool": tool_name,
                    "result": result,
                    "duration": round(duration, 3),
                    "success": result.get("success", False),
                })
                return result
            except Exception as e:
                return {"error": str(e)}

        # 未知工具
        logger.warning(f"ToolAgent: 未知工具 [{tool_name}]")
        return {"error": f"未知工具: {tool_name}"}

    def get_call_history(self) -> List[Dict]:
        return list(self._call_history)


# ============================================================
# 记忆 Agent - 负责记忆管理
# ============================================================

class MemoryAgent:
    """
    记忆 Agent
    职责:
    - 读写各类记忆
    - 总结经验
    - 从错误中学习
    - 检索相关记忆
    """

    def __init__(self, memory_manager: Optional[MemoryManager] = None):
        self.memory = memory_manager or MemoryManager()

    def on_user_message(self, session_id: str, content: str) -> None:
        """处理用户消息，存入短期记忆"""
        self.memory.add_message(session_id, "user", content)

    def on_assistant_message(self, session_id: str, content: str) -> None:
        """处理助手回复，存入短期记忆"""
        self.memory.add_message(session_id, "assistant", content)

    def on_execution_result(
        self,
        session_id: str,
        tool_name: str,
        arguments: Dict,
        result: Dict,
    ) -> None:
        """处理执行结果，存入工作记忆"""
        success = "error" not in result
        self.memory.record_execution(
            session_id=session_id,
            tool_name=tool_name,
            input_data=json.dumps(arguments, ensure_ascii=False)[:500],
            output_data=json.dumps(result, ensure_ascii=False)[:500],
            success=success,
            error=result.get("error", ""),
        )

    def on_task_start(self, session_id: str, description: str, plan: str = "") -> str:
        """任务开始"""
        task_id = self.memory.create_task(session_id, description, plan)
        self.memory.add_working_memory(
            session_id=session_id,
            content=f"开始任务: {description}",
            category="task",
            importance=0.7,
        )
        return task_id

    def on_task_step(
        self,
        task_id: str,
        step_description: str,
        step_result: str,
        success: bool,
    ) -> None:
        """任务步骤完成"""
        self.memory.update_task(
            task_id,
            step_record={
                "description": step_description,
                "result": step_result[:500],
                "success": success,
                "time": time.time(),
            }
        )

    def on_task_complete(self, task_id: str, result: str) -> None:
        """任务完成，总结经验"""
        task = self.memory.get_task(task_id)
        if task:
            # 总结任务经验，写入长期记忆
            summary = f"完成任务: {task.description}\n结果: {result[:500]}"
            if task.steps_history:
                total = len(task.steps_history)
                failed = sum(1 for s in task.steps_history if not s.get("success", True))
                summary += f"\n共 {total} 步, 失败 {failed} 步"

            self.memory.learn(
                content=summary,
                category="task_summary",
                summary=f"任务完成: {task.description[:100]}",
                importance=0.6,
            )

        self.memory.complete_task(task_id, result)

    def on_task_failed(self, task_id: str, error: str) -> None:
        """任务失败，学习教训"""
        task = self.memory.get_task(task_id)
        if task:
            self.memory.learn_from_error(
                error_description=error,
                context=f"任务: {task.description}",
            )
        self.memory.fail_task(task_id, error)

    def on_error(self, session_id: str, error: str, error_type: str = "", fix: str = "") -> None:
        """错误发生时学习"""
        if fix:
            self.memory.learn_from_error(
                error_description=error,
                error_type=error_type,
                fix=fix,
            )
        else:
            # 先检索是否有类似的教训
            past_errors = self.memory.recall_errors(error[:200])
            if not past_errors:
                self.memory.learn_from_error(
                    error_description=error,
                    error_type=error_type,
                )

    def get_context_for_planning(self, session_id: str, user_request: str) -> str:
        """获取规划所需的上下文"""
        context_parts = []

        # 相关长期记忆
        recall = self.memory.recall(user_request, limit=5)
        if recall:
            context_parts.append("=== 相关经验 ===")
            for item in recall:
                context_parts.append(f"- [{item.category}] {item.content[:300]}")

        # 工作记忆
        working = self.memory.get_working_context(session_id)
        if working:
            context_parts.append(working)

        return "\n\n".join(context_parts)

    def get_conversation_history(self, session_id: str, limit: int = 20) -> List[Dict]:
        """获取对话历史"""
        return self.memory.get_conversation(session_id, limit=limit)

    def learn_user_preference(self, key: str, value: str) -> None:
        """学习用户偏好"""
        self.memory.learn_preference(key, value)


# ============================================================
# 主 Agent - 总指挥
# ============================================================

class MasterAgent:
    """
    主 Agent (总指挥)
    职责:
    - 接收用户请求
    - 任务规划
    - 调度 ToolAgent 和 MemoryAgent
    - 结果汇总与回复
    - 死循环/冲突检测
    """

    # 系统提示词
    SYSTEM_PROMPT = """你是 MyAgent，一个强大的本地 AI 助手。你可以直接执行代码和系统命令来完成任务。

## 你的能力
- 执行 Python / Shell / PowerShell 代码
- 读写文件、搜索文件
- 搜索互联网信息
- 获取系统信息
- 管理进程、环境变量
- 发送 HTTP 请求

## 工作方式
1. 理解用户需求
2. 制定执行计划
3. 逐步执行，每步调用合适的工具
4. 根据结果调整计划
5. 汇总结果并回复

## 重要规则
- 优先使用工具完成实际操作，不要只给出建议
- 每次回复必须包含 JSON 格式的 action
- 如果执行失败，分析原因并重试或换方案
- 保持简洁，避免冗余操作
- 对于复杂任务，先规划再执行

## 回复格式
你的回复必须是合法JSON，格式如下:
{
  "thought": "你的思考过程",
  "action": {
    "type": "reply | plan | execute | finish",
    "content": "回复内容/任务描述/工具调用/完成总结"
  },
  "next_steps": ["后续步骤描述"]
}

action 类型说明:
- reply: 直接回复用户文字
- plan: 制定/展示执行计划
- execute: 调用工具执行操作
- finish: 任务完成，总结结果
"""

    def __init__(
        self,
        llm: Optional[LLMClient] = None,
        tool_agent: Optional[ToolAgent] = None,
        memory_agent: Optional[MemoryAgent] = None,
    ):
        cfg = get_config()
        self.llm = llm or get_llm()
        self.tool_agent = tool_agent or ToolAgent()
        self.memory_agent = memory_agent or MemoryAgent()
        self.max_steps = cfg.get("agent.max_plan_steps", 20)
        self.max_tool_calls = cfg.get("agent.max_tool_calls_per_step", 5)
        self.max_loop = cfg.get("agent.execution_loop_max", 50)
        self.thinking_budget = cfg.get("agent.thinking_budget", 3)

        # 会话管理
        self._sessions: Dict[str, Dict] = {}
        self._session_lock = threading.Lock()

        # 防死循环
        self._recent_actions: List[str] = []
        self._action_repeat_count: Dict[str, int] = {}

        logger.info("MasterAgent 初始化完成")

    def process_message(
        self,
        user_message: str,
        session_id: Optional[str] = None,
        callback: Optional[Callable[[str], None]] = None,
    ) -> str:
        """
        处理用户消息 (主入口)

        参数:
            user_message: 用户消息
            session_id: 会话ID
            callback: 流式回调

        返回:
            助手回复文本
        """
        session_id = session_id or str(uuid.uuid4())

        with self._session_lock:
            if session_id not in self._sessions:
                self._sessions[session_id] = {
                    "created": time.time(),
                    "loop_count": 0,
                    "tool_call_count": 0,
                }

        # 记忆处理
        self.memory_agent.on_user_message(session_id, user_message)

        # 获取上下文
        memory_context = self.memory_agent.get_context_for_planning(session_id, user_message)
        conversation = self.memory_agent.get_conversation_history(session_id)

        # 构建消息
        messages = [Message(role="system", content=self._build_system_prompt(memory_context))]

        # 添加对话历史
        for msg in conversation[-20:]:
            messages.append(Message(role=msg["role"], content=msg["content"]))

        messages.append(Message(role="user", content=user_message))

        # 执行循环
        try:
            response_text = self._execution_loop(
                session_id, messages, callback
            )
        except Exception as e:
            logger.error(f"处理消息异常: {e}", exc_info=True)
            response_text = f"处理消息时发生错误: {str(e)}"
            self.memory_agent.on_error(session_id, str(e))

        # 记录回复
        self.memory_agent.on_assistant_message(session_id, response_text)

        return response_text

    def _build_system_prompt(self, memory_context: str = "") -> str:
        """构建完整的系统提示"""
        prompt = self.SYSTEM_PROMPT

        if memory_context:
            prompt += f"\n\n## 相关记忆与上下文\n{memory_context}"

        # 添加可用工具列表
        tools = self.tool_agent.skill_registry.list_skills()
        if tools:
            prompt += "\n\n## 可用工具\n"
            for tool in tools:
                params = []
                for p in tool.parameters:
                    req = "必填" if p.required else "可选"
                    params.append(f"  - {p.name} ({p.type}, {req}): {p.description}")
                param_str = "\n".join(params) if params else "  无参数"
                prompt += f"\n### {tool.name}\n{tool.description}\n参数:\n{param_str}\n"

        return prompt

    def _execution_loop(
        self,
        session_id: str,
        messages: List[Message],
        callback: Optional[Callable] = None,
    ) -> str:
        """
        核心执行循环
        反复调用 LLM -> 解析 action -> 执行 -> 反馈
        直到 LLM 决定回复用户或达到最大循环次数
        """
        session = self._sessions.get(session_id, {})
        final_response = ""

        for loop_idx in range(self.max_loop):
            session["loop_count"] = loop_idx + 1

            # 调用 LLM
            llm_response = self.llm.chat(messages)

            if not llm_response.content:
                final_response = "抱歉，我无法生成有效的回复。请重试。"
                break

            # 解析 JSON action
            action_data = JSONParser.extract_json(llm_response.content)

            if not action_data:
                # 非 JSON 格式，直接作为文本回复
                final_response = llm_response.content
                break

            # 检查 action 类型
            action = action_data.get("action", {})
            action_type = action.get("type", "reply")
            action_content = action.get("content", "")

            logger.info(f"MasterAgent: loop={loop_idx+1}, action={action_type}")

            # 死循环检测
            action_key = f"{action_type}:{str(action_content)[:100]}"
            self._recent_actions.append(action_key)
            if len(self._recent_actions) > 10:
                self._recent_actions.pop(0)

            self._action_repeat_count[action_key] = \
                self._action_repeat_count.get(action_key, 0) + 1

            if self._action_repeat_count[action_key] > 3:
                logger.warning(f"检测到重复 action: {action_key}, 强制退出循环")
                final_response = f"我注意到陷入了重复操作。让我总结一下当前的情况:\n{action_data.get('thought', '')}"
                break

            if action_type == "reply":
                # 直接回复
                final_response = action_content or action_data.get("thought", "")
                break

            elif action_type == "finish":
                # 任务完成
                final_response = action_content or "任务已完成。"
                break

            elif action_type == "plan":
                # 制定计划 - 回馈给 LLM 继续执行
                messages.append(Message(role="assistant", content=llm_response.content))
                messages.append(Message(
                    role="user",
                    content=f"计划已收到。请按照计划开始执行第一步。"
                ))

                if callback:
                    callback(f"📋 计划: {action_content[:500]}")

            elif action_type == "execute":
                # 执行工具调用
                messages.append(Message(role="assistant", content=llm_response.content))

                # 解析工具调用
                tool_name = action_content.get("tool", "") if isinstance(action_content, dict) else ""
                tool_args = action_content.get("arguments", {}) if isinstance(action_content, dict) else {}

                if not tool_name:
                    # 尝试从 action_data 整体解析
                    tool_name = action_data.get("tool", "")
                    tool_args = action_data.get("arguments", {})

                if tool_name:
                    result = self.tool_agent.execute_tool_call(tool_name, tool_args)

                    # 记忆
                    self.memory_agent.on_execution_result(session_id, tool_name, tool_args, result)

                    # 构建反馈
                    result_str = json.dumps(result, ensure_ascii=False, indent=2)
                    if len(result_str) > 8000:
                        result_str = result_str[:8000] + "\n... (结果已截断)"

                    feedback = f"工具 [{tool_name}] 执行结果:\n```json\n{result_str}\n```"

                    if callback:
                        success = "error" not in result
                        status = "✅" if success else "❌"
                        callback(f"{status} 执行 {tool_name}")

                    messages.append(Message(role="user", content=feedback))
                else:
                    # action_content 可能是描述性文本，要求 LLM 明确工具
                    messages.append(Message(
                        role="user",
                        content="请明确指定要调用的工具名称和参数。格式: {\"tool\": \"tool_name\", \"arguments\": {...}}"
                    ))
            else:
                # 未知 action 类型
                final_response = action_data.get("thought", llm_response.content)
                break

        return final_response

    # --------------------------------------------------------
    # 兼容 OpenAI Function Calling 的执行模式
    # --------------------------------------------------------

    def process_with_tools(
        self,
        user_message: str,
        session_id: Optional[str] = None,
    ) -> str:
        """
        使用 function calling 模式处理消息
        (当 LLM 支持 tool_calls 时使用此方法)
        """
        session_id = session_id or str(uuid.uuid4())

        self.memory_agent.on_user_message(session_id, user_message)

        memory_context = self.memory_agent.get_context_for_planning(session_id, user_message)
        conversation = self.memory_agent.get_conversation_history(session_id)

        messages = [Message(role="system", content=self._build_system_prompt(memory_context))]
        for msg in conversation[-20:]:
            messages.append(Message(role=msg["role"], content=msg["content"]))
        messages.append(Message(role="user", content=user_message))

        # 获取工具定义
        tool_definitions = self.tool_agent.skill_registry.get_all_tool_definitions()
        tools = [ToolDefinition(**td) for td in tool_definitions] if tool_definitions else None

        # Function calling 循环
        max_iterations = 20
        final_content = ""

        for i in range(max_iterations):
            try:
                response = self.llm.chat(messages, tools=tools)
            except Exception as e:
                logger.error(f"LLM 调用失败: {e}")
                return f"LLM 调用失败: {str(e)}"

            # 追加 assistant 消息
            assistant_msg = Message(
                role="assistant",
                content=response.content or "",
            )
            messages.append(assistant_msg)

            # 检查是否有 tool_calls
            if response.tool_calls:
                for tc in response.tool_calls:
                    func = tc.get("function", {})
                    tool_name = func.get("name", "")
                    try:
                        args = json.loads(func.get("arguments", "{}"))
                    except:
                        args = {}

                    logger.info(f"Function call: {tool_name}({json.dumps(args, ensure_ascii=False)[:200]})")

                    # 执行
                    result = self.tool_agent.execute_tool_call(tool_name, args)
                    result_str = json.dumps(result, ensure_ascii=False, ensure_ascii=False)

                    self.memory_agent.on_execution_result(
                        session_id, tool_name, args, result
                    )

                    # 追加 tool 结果消息
                    messages.append(Message(
                        role="tool",
                        content=result_str[:16000],  # 限制长度
                        tool_call_id=tc.get("id", ""),
                        name=tool_name,
                    ))

                continue  # 继续循环，让 LLM 处理工具结果

            # 没有 tool_calls，任务完成
            final_content = response.content
            break

        self.memory_agent.on_assistant_message(session_id, final_content)
        return final_content

    def get_session_info(self, session_id: str) -> Optional[Dict]:
        return self._sessions.get(session_id)

    def get_stats(self) -> Dict[str, Any]:
        return {
            "active_sessions": len(self._sessions),
            "executor_stats": self.tool_agent.executor.get_stats(),
            "tool_calls": len(self.tool_agent.get_call_history()),
        }


# ============================================================
# 统一 Agent 入口
# ============================================================

class AgentController:
    """
    Agent 控制器
    统一管理 MasterAgent / ToolAgent / MemoryAgent
    对外提供简洁 API
    """

    def __init__(self):
        self.memory_agent = MemoryAgent()
        self.tool_agent = ToolAgent()
        self.master_agent = MasterAgent(
            tool_agent=self.tool_agent,
            memory_agent=self.memory_agent,
        )
        self._is_running = False
        self._lock = threading.Lock()

    def chat(
        self,
        message: str,
        session_id: Optional[str] = None,
        callback: Optional[Callable] = None,
    ) -> str:
        """
        对话入口

        参数:
            message: 用户消息
            session_id: 会话ID (空=新会话)
            callback: 流式回调函数

        返回:
            助手回复文本
        """
        with self._lock:
            try:
                # 先尝试 function calling 模式
                response = self.master_agent.process_with_tools(
                    message, session_id
                )
            except Exception as e:
                logger.warning(f"Function calling 模式失败，回退到 action 模式: {e}")
                # 回退到 action 模式
                response = self.master_agent.process_message(
                    message, session_id, callback
                )
            return response

    def get_stats(self) -> Dict[str, Any]:
        """获取统计信息"""
        return self.master_agent.get_stats()

    def get_memory_stats(self) -> Dict[str, Any]:
        """获取记忆统计"""
        return self.memory_agent.memory.maintenance()

    def list_sessions(self) -> List[Dict]:
        """列出所有会话"""
        return self.memory_agent.memory.store.get_sessions()

    def shutdown(self):
        """关闭 Agent"""
        self._is_running = False


# ============================================================
# 全局 Agent 控制器
# ============================================================

_global_agent: Optional[AgentController] = None


def get_agent() -> AgentController:
    """获取全局 Agent 控制器"""
    global _global_agent
    if _global_agent is None:
        _global_agent = AgentController()
    return _global_agent
