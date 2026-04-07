"""
skills/registry.py - 技能注册表
================================
管理所有已注册的技能，提供查找、调用、列表功能。
支持动态注册、热加载和 OpenClaw 外部技能桥接。
"""
from __future__ import annotations

import importlib
import inspect
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.logger import get_logger
from skills.base import Skill, SkillResult, SkillParameter

logger = get_logger("myagent.skills")


class OpenClawSkillAdapter(Skill):
    """
    OpenClaw 外部技能适配器。

    将 skills/ 目录下的 OpenClaw 格式技能（SKILL.md + scripts/）包装为
    Skill 基类实例，使其可以在 SkillRegistry 中统一管理。

    OpenClaw 技能结构:
      skills/
        skill-name/
          SKILL.md       # 技能描述和指令
          scripts/       # 可执行脚本
          references/    # 参考文档
          skill.json     # 技能元数据（可选）
    """

    def __init__(self, skill_dir: Path):
        super().__init__()
        self._skill_dir = skill_dir
        self._skill_md = ""
        self._scripts: Dict[str, Path] = {}
        self._metadata: Dict[str, Any] = {}
        self._load_skill_info()

    def _load_skill_info(self):
        """加载技能信息"""
        self.name = self._skill_dir.name

        # 读取 SKILL.md
        skill_md_path = self._skill_dir / "SKILL.md"
        if skill_md_path.exists():
            self._skill_md = skill_md_path.read_text(encoding="utf-8", errors="ignore")
            # 从 SKILL.md 第一行提取描述
            first_line = self._skill_md.strip().split("\n")[0]
            self.description = first_line.lstrip("# ").strip()

        # 读取 skill.json（如果有）
        skill_json_path = self._skill_dir / "skill.json"
        if skill_json_path.exists():
            try:
                self._metadata = json.loads(skill_json_path.read_text(encoding="utf-8"))
                if "name" in self._metadata:
                    self.name = self._metadata["name"]
                if "description" in self._metadata:
                    self.description = self._metadata["description"]
            except json.JSONDecodeError:
                pass

        # 发现脚本文件
        scripts_dir = self._skill_dir / "scripts"
        if scripts_dir.exists():
            for script_file in scripts_dir.iterdir():
                if script_file.is_file() and not script_file.name.startswith("_"):
                    # 去除扩展名作为参数名
                    param_name = script_file.stem
                    self._scripts[param_name] = script_file

        # 构建 OpenClaw 技能的通用参数
        self.parameters = [
            SkillParameter(
                name="task",
                type="string",
                description=f"要执行的任务指令（{self.description}）",
                required=True,
            ),
        ]

    async def execute(self, **kwargs) -> SkillResult:
        """
        执行 OpenClaw 技能。

        将 SKILL.md 的内容作为系统提示，
        结合用户任务参数，返回使用指引。
        """
        task = kwargs.get("task", "")

        # 构建使用指引（实际执行由 Agent 的 LLM 解读 SKILL.md 指令完成）
        references_info = ""
        ref_dir = self._skill_dir / "references"
        if ref_dir.exists():
            ref_files = list(ref_dir.glob("*.md"))
            if ref_files:
                references_info = f"\n\n## 参考文档\n可用参考文件: {', '.join(f.name for f in ref_files)}"

        scripts_info = ""
        if self._scripts:
            scripts_info = f"\n\n## 可用脚本\n"
            for name, path in self._scripts.items():
                scripts_info += f"- `{name}` ({path.suffix}): {path}\n"

        result_text = (
            f"## 技能: {self.name}\n\n"
            f"### 描述\n{self.description}\n\n"
            f"### 使用说明\n{self._skill_md[:5000]}"
            f"{references_info}{scripts_info}\n\n"
            f"### 当前任务\n{task}\n\n"
            f"请根据上述技能说明完成当前任务。"
        )

        return SkillResult(
            success=True,
            data={
                "skill_type": "openclaw",
                "skill_name": self.name,
                "skill_dir": str(self._skill_dir),
                "instruction": result_text,
                "has_scripts": bool(self._scripts),
                "scripts": {name: str(path) for name, path in self._scripts.items()},
            },
            output=result_text[:3000],
        )

    def to_openclaw_format(self) -> dict:
        """导出为 OpenClaw 格式"""
        result = super().to_openclaw_format()
        result["source"] = "openclaw_external"
        result["skill_dir"] = str(self._skill_dir)
        return result


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

        # 加载外部 OpenClaw 技能
        registry.load_openclaw_skills("/path/to/skills/")
    """

    def __init__(self):
        self._skills: Dict[str, Skill] = {}
        self._openclaw_skills: Dict[str, OpenClawSkillAdapter] = {}

    def register(self, skill: Skill):
        """注册技能"""
        if not isinstance(skill, Skill):
            raise TypeError(f"必须是 Skill 子类，得到: {type(skill)}")
        self._skills[skill.name] = skill
        logger.debug(f"技能已注册: {skill.name}")

    def unregister(self, name: str) -> bool:
        """注销技能，返回是否成功"""
        if name in self._skills:
            del self._skills[name]
            logger.debug(f"技能已注销: {name}")
            return True
        if name in self._openclaw_skills:
            del self._openclaw_skills[name]
            logger.debug(f"OpenClaw 技能已注销: {name}")
            return True
        return False

    def get(self, name: str) -> Optional[Skill]:
        """获取技能（先查内置，再查 OpenClaw）"""
        skill = self._skills.get(name)
        if skill:
            return skill
        return self._openclaw_skills.get(name)

    def list_skills(self) -> List[str]:
        """列出所有技能名称"""
        builtin = list(self._skills.keys())
        external = [f"[OpenClaw] {name}" for name in self._openclaw_skills.keys()
                    if name not in self._skills]
        return builtin + external

    def list_skills_info(self) -> List[Dict]:
        """列出所有技能的详细信息"""
        results = [skill.to_openclaw_format() for skill in self._skills.values()]
        results.extend(
            skill.to_openclaw_format() for skill in self._openclaw_skills.values()
            if skill.name not in self._skills
        )
        return results

    def get_all_schemas(self) -> List[Dict]:
        """获取所有技能的 JSON Schema (用于 LLM function calling)"""
        all_skills = {**self._skills, **self._openclaw_skills}
        return [skill.get_schema() for skill in all_skills.values()]

    async def execute(self, name: str, **kwargs) -> SkillResult:
        """
        按名称执行技能。

        Args:
            name: 技能名称
            **kwargs: 技能参数

        Returns:
            SkillResult
        """
        skill = self.get(name)
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

    def load_openclaw_skills(self, skills_root: str = ""):
        """
        加载外部 OpenClaw 格式技能。

        扫描指定目录下所有包含 SKILL.md 的子目录，
        为每个技能创建 OpenClawSkillAdapter 并注册。

        Args:
            skills_root: 外部技能根目录（默认为项目根目录下的 skills/）
        """
        if not skills_root:
            skills_root = str(Path(__file__).parent.parent.parent / "skills")

        skills_path = Path(skills_root)
        if not skills_path.exists():
            logger.debug(f"外部技能目录不存在: {skills_path}")
            return

        loaded_count = 0
        for skill_dir in skills_path.iterdir():
            if not skill_dir.is_dir():
                continue
            if skill_dir.name.startswith("_") or skill_dir.name.startswith("."):
                continue

            # 检查是否包含 SKILL.md
            if not (skill_dir / "SKILL.md").exists():
                continue

            skill_name = skill_dir.name

            # 跳过已注册的同名内置技能
            if skill_name in self._skills:
                continue

            try:
                adapter = OpenClawSkillAdapter(skill_dir)
                self._openclaw_skills[skill_name] = adapter
                loaded_count += 1
                logger.info(f"已加载 OpenClaw 技能: {skill_name} - {adapter.description[:80]}")
            except Exception as e:
                logger.warning(f"加载 OpenClaw 技能失败 ({skill_name}): {e}")

        if loaded_count > 0:
            logger.info(f"共加载 {loaded_count} 个 OpenClaw 外部技能")


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
        # 自动加载外部 OpenClaw 技能
        _global_registry.load_openclaw_skills()
    return _global_registry
