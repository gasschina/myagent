"""
skills/registry.py - 技能注册表
================================
管理所有已注册的技能，提供查找、调用、列表功能。
支持动态注册和热加载。
"""
from __future__ import annotations

import importlib
import inspect
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.logger import get_logger
from skills.base import Skill, SkillResult, SkillParameter

logger = get_logger("myagent.skills")


class SkillRegistry:
    """
    技能注册表。

    使用示例:
        registry = SkillRegistry()
        registry.register(FileReadSkill())

        # 查找技能
        skill = registry.get("file_read")
        result = await skill.execute(path="/tmp/test.txt")

        # 获取所有工具定义(给 LLM 用)
        tools = registry.get_all_schemas()

        # 按名称执行
        result = await registry.execute("file_read", path="/tmp/test.txt")
    """

    def __init__(self):
        self._skills: Dict[str, Skill] = {}

    def register(self, skill: Skill):
        """注册技能"""
        if not isinstance(skill, Skill):
            raise TypeError(f"必须是 Skill 子类，得到: {type(skill)}")
        self._skills[skill.name] = skill
        logger.debug(f"技能已注册: {skill.name}")

    def unregister(self, name: str):
        """注销技能"""
        if name in self._skills:
            del self._skills[name]
            logger.debug(f"技能已注销: {name}")

    def get(self, name: str) -> Optional[Skill]:
        """获取技能"""
        return self._skills.get(name)

    def list_skills(self) -> List[str]:
        """列出所有技能名称"""
        return list(self._skills.keys())

    def list_skills_info(self) -> List[Dict]:
        """列出所有技能的详细信息"""
        return [skill.to_openclaw_format() for skill in self._skills.values()]

    def get_all_schemas(self) -> List[Dict]:
        """获取所有技能的 JSON Schema (用于 LLM function calling)"""
        return [skill.get_schema() for skill in self._skills.values()]

    async def execute(self, name: str, **kwargs) -> SkillResult:
        """
        按名称执行技能。

        Args:
            name: 技能名称
            **kwargs: 技能参数

        Returns:
            SkillResult
        """
        skill = self._skills.get(name)
        if not skill:
            return SkillResult(
                success=False,
                error=f"技能不存在: {name}",
            )

        # 参数校验
        valid, err = skill.validate_params(kwargs)
        if not valid:
            return SkillResult(success=False, error=err)

        try:
            logger.info(f"执行技能: {name} (参数: {list(kwargs.keys())})")
            result = await skill.execute(**kwargs)
            return result
        except Exception as e:
            logger.error(f"技能执行失败 ({name}): {e}")
            return SkillResult(
                success=False,
                error=f"技能执行异常: {name} - {str(e)}",
            )

    def auto_discover(self, package: str = "skills"):
        """
        自动发现并注册 skills/ 目录下的所有技能。

        Args:
            package: 技能包路径
        """
        skills_dir = Path(__file__).parent
        if not skills_dir.exists():
            return

        for file in skills_dir.glob("*_skill.py"):
            if file.name.startswith("_") or file.name == "base.py":
                continue

            module_name = f"{package}.{file.stem}"
            try:
                module = importlib.import_module(module_name)
                # 查找模块中的 Skill 子类
                for attr_name in dir(module):
                    attr = getattr(module, attr_name)
                    if (inspect.isclass(attr)
                            and issubclass(attr, Skill)
                            and attr is not Skill
                            and not attr.__name__.startswith("_")):
                        try:
                            instance = attr()
                            self.register(instance)
                            logger.info(f"自动发现技能: {instance.name}")
                        except Exception as e:
                            logger.warning(f"技能实例化失败 ({attr_name}): {e}")
            except Exception as e:
                logger.warning(f"模块导入失败 ({module_name}): {e}")


# ==============================================================================
# 全局注册表
# ==============================================================================

_global_registry: Optional[SkillRegistry] = None


def get_skill_registry() -> SkillRegistry:
    """获取全局技能注册表"""
    global _global_registry
    if _global_registry is None:
        _global_registry = SkillRegistry()
        _global_registry.auto_discover()
    return _global_registry
