"""
skills/system_skill.py - 系统操作技能
========================================
提供系统信息查询、进程管理、环境操作等功能。
"""
from __future__ import annotations

import asyncio
import os
import sys
import subprocess
import platform
import shutil
from datetime import datetime
from typing import Optional, List, Dict

from core.logger import get_logger
from skills.base import Skill, SkillResult, SkillParameter

logger = get_logger("myagent.skills.system")


class SystemInfoSkill(Skill):
    """获取系统信息"""
    name = "system_info"
    description = "获取当前系统的详细信息(操作系统、CPU、内存、磁盘等)"
    category = "system"
    parameters = []

    async def execute(self, **kwargs) -> SkillResult:
        try:
            import psutil

            info = {
                "platform": platform.platform(),
                "system": platform.system(),
                "release": platform.release(),
                "version": platform.version(),
                "machine": platform.machine(),
                "processor": platform.processor(),
                "hostname": platform.node(),
                "python_version": sys.version,
                "cpu_count": psutil.cpu_count(),
                "cpu_percent": psutil.cpu_percent(interval=1),
                "memory": {
                    "total_gb": round(psutil.virtual_memory().total / (1024**3), 2),
                    "available_gb": round(psutil.virtual_memory().available / (1024**3), 2),
                    "percent": psutil.virtual_memory().percent,
                },
                "disk": {
                    "total_gb": round(psutil.disk_usage("/").total / (1024**3), 2),
                    "free_gb": round(psutil.disk_usage("/").free / (1024**3), 2),
                    "percent": psutil.disk_usage("/").percent,
                },
                "current_user": os.getlogin(),
                "cwd": os.getcwd(),
                "home": str(os.path.expanduser("~")),
                "pid": os.getpid(),
            }

            # 环境变量(常用)
            important_env = [
                "PATH", "HOME", "USER", "SHELL",
                "LANG", "TERM", "EDITOR",
                "PYTHONPATH", "VIRTUAL_ENV", "CONDA_DEFAULT_ENV",
            ]
            env_vars = {}
            for key in important_env:
                val = os.environ.get(key)
                if val:
                    env_vars[key] = val
            info["env"] = env_vars

            return SkillResult(
                success=True,
                data=info,
                message=f"系统: {platform.system()} {platform.release()} | "
                        f"CPU: {info['cpu_count']}核 | "
                        f"内存: {info['memory']['available_gb']}GB可用",
            )
        except ImportError:
            # 回退: 不用 psutil
            info = {
                "platform": platform.platform(),
                "system": platform.system(),
                "python_version": sys.version,
                "cwd": os.getcwd(),
                "pid": os.getpid(),
            }
            return SkillResult(
                success=True,
                data=info,
                message="psutil 未安装，仅返回基本信息。建议: pip install psutil",
            )
        except Exception as e:
            return SkillResult(success=False, error=str(e))


class ProcessListSkill(Skill):
    """列出进程"""
    name = "process_list"
    description = "列出系统当前运行的进程"
    category = "system"
    parameters = [
        SkillParameter("filter", "string", "进程名过滤", required=False, default=""),
        SkillParameter("limit", "integer", "返回数量限制", required=False, default=20),
    ]

    async def execute(self, filter: str = "", limit: int = 20, **kwargs) -> SkillResult:
        try:
            import psutil

            processes = []
            for proc in psutil.process_iter(["pid", "name", "cpu_percent", "memory_percent", "status"]):
                try:
                    info = proc.info
                    if filter and filter.lower() not in info["name"].lower():
                        continue
                    processes.append(info)
                    if len(processes) >= limit:
                        break
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue

            # 按内存排序
            processes.sort(key=lambda x: x.get("memory_percent", 0) or 0, reverse=True)

            return SkillResult(
                success=True,
                data={"processes": processes, "count": len(processes)},
                message=f"找到 {len(processes)} 个进程",
            )
        except ImportError:
            return SkillResult(success=False, error="请安装 psutil: pip install psutil")
        except Exception as e:
            return SkillResult(success=False, error=str(e))


class CommandRunSkill(Skill):
    """执行系统命令"""
    name = "command_run"
    description = "执行系统命令并返回结果(Shell/Bash/PowerShell)"
    category = "system"
    parameters = [
        SkillParameter("command", "string", "要执行的命令", required=True),
        SkillParameter("timeout", "integer", "超时秒数", required=False, default=60),
        SkillParameter("work_dir", "string", "工作目录", required=False, default=""),
    ]
    dangerous = True

    async def execute(self, command: str = "", timeout: int = 60,
                      work_dir: str = "", **kwargs) -> SkillResult:
        try:
            work_dir = work_dir or os.getcwd()
            process = await asyncio.wait_for(
                self._subprocess_exec(command, work_dir),
                timeout=timeout,
            )
            return process
        except asyncio.TimeoutError:
            return SkillResult(success=False, error=f"命令执行超时 ({timeout}s)")
        except Exception as e:
            return SkillResult(success=False, error=str(e))

    async def _subprocess_exec(self, command: str, work_dir: str) -> SkillResult:
        """执行子进程"""
        process = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=work_dir,
        )
        stdout, stderr = await process.communicate()
        stdout_str = stdout.decode("utf-8", errors="replace")
        stderr_str = stderr.decode("utf-8", errors="replace")

        return SkillResult(
            success=process.returncode == 0,
            data={
                "exit_code": process.returncode,
                "stdout": stdout_str,
                "stderr": stderr_str,
            },
            message=stdout_str[:2000] if process.returncode == 0 else f"失败: {stderr_str[:500]}",
            error=stderr_str if process.returncode != 0 else "",
        )


class EnvironmentGetSkill(Skill):
    """获取环境变量"""
    name = "env_get"
    description = "获取系统环境变量"
    category = "system"
    parameters = [
        SkillParameter("key", "string", "环境变量名(空=全部)", required=False, default=""),
    ]

    async def execute(self, key: str = "", **kwargs) -> SkillResult:
        try:
            if key:
                value = os.environ.get(key)
                if value is None:
                    return SkillResult(success=False, error=f"环境变量不存在: {key}")
                return SkillResult(success=True, data={"key": key, "value": value})
            else:
                # 返回所有(过滤敏感信息)
                sensitive = {"PASSWORD", "SECRET", "TOKEN", "KEY", "CREDENTIAL", "API_KEY"}
                env = {}
                for k, v in os.environ.items():
                    if not any(s in k.upper() for s in sensitive):
                        env[k] = v
                return SkillResult(
                    success=True,
                    data={"count": len(env), "variables": env},
                    message=f"共 {len(env)} 个环境变量(已过滤敏感信息)",
                )
        except Exception as e:
            return SkillResult(success=False, error=str(e))


class PathExpandSkill(Skill):
    """路径操作"""
    name = "path_info"
    description = "获取路径的详细信息(绝对路径、父目录、扩展名等)"
    category = "system"
    parameters = [
        SkillParameter("path", "string", "文件/目录路径", required=True),
    ]

    async def execute(self, path: str = "", **kwargs) -> SkillResult:
        try:
            p = Path(path).expanduser().resolve()
            return SkillResult(
                success=True,
                data={
                    "original": path,
                    "absolute": str(p),
                    "name": p.name,
                    "stem": p.stem,
                    "suffix": p.suffix,
                    "parent": str(p.parent),
                    "exists": p.exists(),
                    "is_file": p.is_file(),
                    "is_dir": p.is_dir(),
                    "size": p.stat().st_size if p.exists() else 0,
                },
            )
        except Exception as e:
            return SkillResult(success=False, error=str(e))
