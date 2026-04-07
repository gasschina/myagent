"""
skills/base.py - 技能基类
===========================
定义所有技能的统一接口，兼容 OpenClaw 风格的 JSON 结构化调用。
"""
from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class SkillParameter:
    """技能参数定义"""
    name: str
    type: str = "string"       # string | integer | float | boolean | array | object
    description: str = ""
    required: bool = True
    default: Any = None
    enum: Optional[List[str]] = None  # 限制可选值

    def to_dict(self) -> dict:
        result = {
            "name": self.name,
            "type": self.type,
            "description": self.description,
            "required": self.required,
        }
        if self.default is not None:
            result["default"] = self.default
        if self.enum:
            result["enum"] = self.enum
        return result


@dataclass
class SkillResult:
    """技能执行结果"""
    success: bool = True
    data: Any = None             # 返回数据
    message: str = ""            # 人类可读的消息
    error: str = ""              # 错误信息
    files: List[str] = field(default_factory=list)  # 产生的文件列表
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "data": self.data,
            "message": self.message,
            "error": self.error,
            "files": self.files,
            "metadata": self.metadata,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)


class Skill(ABC):
    """
    技能抽象基类。

    所有技能必须继承此类并实现 execute 方法。
    技能定义遵循 OpenClaw 风格的 JSON Schema。

    子类需要设置:
      - name: 技能名称
      - description: 技能描述
      - parameters: 参数列表
    """

    name: str = "base_skill"
    description: str = "基础技能"
    category: str = "general"
    parameters: List[SkillParameter] = []
    dangerous: bool = False  # 标记是否为危险操作

    @abstractmethod
    async def execute(self, **kwargs) -> SkillResult:
        """
        执行技能。

        Args:
            **kwargs: 技能参数

        Returns:
            SkillResult 执行结果
        """
        pass

    def validate_params(self, params: Dict[str, Any]) -> tuple[bool, str]:
        """校验参数是否合法"""
        for p in self.parameters:
            if p.required and p.name not in params:
                return False, f"缺少必需参数: {p.name}"
            if p.name in params and p.enum and params[p.name] not in p.enum:
                return False, f"参数 {p.name} 值无效，可选: {p.enum}"
        return True, ""

    def get_schema(self) -> dict:
        """
        获取技能的 JSON Schema (用于 LLM function calling)。
        兼容 OpenAI function calling 格式。
        """
        properties = {}
        required = []
        for p in self.parameters:
            prop = {
                "type": p.type,
                "description": p.description,
            }
            if p.enum:
                prop["enum"] = p.enum
            if p.default is not None:
                prop["default"] = p.default
            properties[p.name] = prop
            if p.required:
                required.append(p.name)

        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                },
            },
        }

    def to_openclaw_format(self) -> dict:
        """获取 OpenClaw 风格的技能定义"""
        return {
            "name": self.name,
            "description": self.description,
            "category": self.category,
            "parameters": [p.to_dict() for p in self.parameters],
            "dangerous": self.dangerous,
        }

    def __repr__(self):
        return f"<Skill: {self.name}>"
