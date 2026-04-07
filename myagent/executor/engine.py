"""
executor/engine.py - 执行引擎
================================
Open Interpreter 风格的本地代码执行引擎。
支持 Python / Shell (Bash) / PowerShell / 系统命令。
特性:
  - 安全沙箱执行(超时控制、命令黑名单)
  - 自动错误捕获与修复
  - 结构化结果返回
  - 跨平台兼容 (Windows/macOS/Linux)
"""
from __future__ import annotations

import os
import sys
import subprocess
import tempfile
import textwrap
import traceback
import asyncio
import signal
import platform
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from core.logger import get_logger
from core.utils import timestamp, truncate_str, format_execution_result, detect_platform

logger = get_logger("myagent.executor")


# ==============================================================================
# 执行结果
# ==============================================================================

@dataclass
class ExecResult:
    """执行结果(结构化)"""
    success: bool = False
    stdout: str = ""
    stderr: str = ""
    exit_code: int = -1
    error: str = ""
    execution_time: float = 0.0
    language: str = ""
    code: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "stdout": truncate_str(self.stdout, 30000),
            "stderr": truncate_str(self.stderr, 10000),
            "exit_code": self.exit_code,
            "error": truncate_str(self.error, 5000),
            "execution_time": round(self.execution_time, 3),
            "language": self.language,
            "metadata": self.metadata,
        }

    def to_llm_message(self) -> str:
        """转换为 LLM 可理解的消息格式"""
        if self.success:
            parts = [f"✅ 执行成功 (耗时: {self.execution_time:.2f}s)"]
            if self.stdout.strip():
                parts.append(f"输出:\n```\n{self.stdout.strip()}\n```")
            if self.stderr.strip():
                parts.append(f"标准错误:\n```\n{self.stderr.strip()}\n```")
            return "\n".join(parts)
        else:
            parts = [f"❌ 执行失败 (退出码: {self.exit_code})"]
            if self.error:
                parts.append(f"错误: {self.error}")
            if self.stdout.strip():
                parts.append(f"输出:\n```\n{self.stdout.strip()}\n```")
            if self.stderr.strip():
                parts.append(f"标准错误:\n```\n{self.stderr.strip()}\n```")
            return "\n".join(parts)


# ==============================================================================
# 执行引擎
# ==============================================================================

class ExecutionEngine:
    """
    本地代码执行引擎。

    支持:
      - python: Python 3 脚本执行
      - shell / bash: Unix Shell 命令
      - powershell: Windows PowerShell
      - cmd: Windows 命令提示符
      - system: 自动检测平台执行系统命令

    使用示例:
        engine = ExecutionEngine(timeout=60)
        result = await engine.execute("python", "print('Hello, World!')")
        print(result.to_dict())
    """

    # 危险命令黑名单
    DANGEROUS_COMMANDS = [
        "rm -rf /", "rm -rf /*", "mkfs", "dd if=/dev/zero",
        ":(){ :|:& };:", "fork bomb",
        "format C:", "del /f /s /q C:\\",
        "shutdown -h now", "reboot",
    ]

    def __init__(
        self,
        timeout: int = 300,
        max_retries: int = 2,
        auto_fix: bool = True,
        max_output_length: int = 50000,
        work_dir: Optional[str] = None,
        extra_blocked: Optional[List[str]] = None,
    ):
        self.timeout = timeout
        self.max_retries = max_retries
        self.auto_fix = auto_fix
        self.max_output_length = max_output_length
        self.work_dir = work_dir or os.getcwd()
        self._blocked = set(self.DANGEROUS_COMMANDS)
        if extra_blocked:
            self._blocked.update(extra_blocked)

        self._execution_count = 0

    def _check_safety(self, code: str) -> Tuple[bool, str]:
        """检查代码安全性"""
        code_lower = code.lower().strip()
        for dangerous in self._blocked:
            if dangerous.lower() in code_lower:
                return False, f"危险命令被拦截: {dangerous}"
        return True, ""

    def _get_shell(self, language: str) -> Tuple[str, List[str]]:
        """获取执行 shell 和参数"""
        system = detect_platform()

        if language == "python":
            return sys.executable, ["-u", "-c"]
        elif language in ("shell", "bash"):
            if system == "windows":
                # Windows 上尝试 git bash 或 WSL
                for shell in ["bash", "C:\\Program Files\\Git\\bin\\bash.exe",
                              "C:\\msys64\\usr\\bin\\bash.exe"]:
                    if os.path.exists(shell) or shell == "bash":
                        return shell, ["-c"]
                # 回退到 cmd
                return "cmd", ["/c"]
            return "bash", ["-c"]
        elif language == "powershell":
            if system == "windows":
                return "powershell", ["-NoProfile", "-Command"]
            return "pwsh", ["-NoProfile", "-Command"]
        elif language == "cmd":
            return "cmd", ["/c"]
        elif language == "system":
            if system == "windows":
                return "cmd", ["/c"]
            return "bash", ["-c"]
        else:
            raise ValueError(f"不支持的语言: {language}")

    async def execute(
        self,
        language: str,
        code: str,
        timeout: Optional[int] = None,
        work_dir: Optional[str] = None,
        env: Optional[Dict[str, str]] = None,
        metadata: Optional[Dict] = None,
    ) -> ExecResult:
        """
        执行代码。

        Args:
            language: 编程语言 (python/shell/bash/powershell/cmd/system)
            code: 要执行的代码
            timeout: 超时时间(秒)，默认使用引擎配置
            work_dir: 工作目录
            env: 环境变量
            metadata: 附加元数据

        Returns:
            ExecResult 结构化结果
        """
        exec_timeout = timeout or self.timeout
        work_dir = work_dir or self.work_dir
        metadata = metadata or {}

        # 安全检查
        safe, reason = self._check_safety(code)
        if not safe:
            return ExecResult(
                success=False,
                error=reason,
                language=language,
                code=code,
                metadata=metadata,
            )

        self._execution_count += 1
        exec_id = f"exec_{self._execution_count}"

        logger.info(f"[{exec_id}] 开始执行 ({language}, timeout={exec_timeout}s)")
        logger.debug(f"[{exec_id}] 代码:\n{code[:500]}")

        # 对于 Python，使用临时文件执行(更好的错误追踪)
        if language == "python":
            result = await self._execute_python(code, exec_timeout, work_dir, env, exec_id)
        else:
            result = await self._execute_shell(language, code, exec_timeout, work_dir, env, exec_id)

        result.language = language
        result.code = code
        result.metadata = metadata

        if result.success:
            logger.info(f"[{exec_id}] 执行成功 (耗时: {result.execution_time:.2f}s)")
        else:
            logger.warning(f"[{exec_id}] 执行失败: {result.error[:200]}")

        # 自动修复
        if not result.success and self.auto_fix and self.max_retries > 0:
            fix_result = await self._auto_fix(language, code, result, exec_timeout, work_dir, env)
            if fix_result and fix_result.success:
                logger.info(f"[{exec_id}] 自动修复成功!")
                fix_result.language = language
                fix_result.code = code
                fix_result.metadata = {**metadata, "auto_fixed": True}
                return fix_result

        return result

    async def _execute_python(
        self,
        code: str,
        timeout: int,
        work_dir: str,
        env: Optional[Dict[str, str]],
        exec_id: str,
    ) -> ExecResult:
        """执行 Python 代码"""
        start_time = asyncio.get_event_loop().time()

        # 写入临时文件
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                suffix=".py",
                dir=work_dir,
                delete=False,
                encoding="utf-8",
            ) as f:
                # 添加标准输出重定向以确保实时输出
                f.write("import sys; sys.stdout.reconfigure(encoding='utf-8')\n")
                f.write(code)
                temp_file = f.name
        except Exception as e:
            return ExecResult(error=f"创建临时文件失败: {e}")

        try:
            process = await asyncio.create_subprocess_exec(
                sys.executable, "-u", temp_file,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=work_dir,
                env=self._build_env(env),
            )

            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=timeout,
                )
                elapsed = asyncio.get_event_loop().time() - start_time
                stdout_str = stdout.decode("utf-8", errors="replace")
                stderr_str = stderr.decode("utf-8", errors="replace")

                return ExecResult(
                    success=process.returncode == 0,
                    stdout=stdout_str,
                    stderr=stderr_str,
                    exit_code=process.returncode or -1,
                    error=stderr_str if process.returncode != 0 else "",
                    execution_time=elapsed,
                )
            except asyncio.TimeoutError:
                process.kill()
                elapsed = asyncio.get_event_loop().time() - start_time
                return ExecResult(
                    success=False,
                    error=f"执行超时 ({timeout}s)",
                    execution_time=elapsed,
                )
        finally:
            try:
                os.unlink(temp_file)
            except OSError:
                pass

    async def _execute_shell(
        self,
        language: str,
        code: str,
        timeout: int,
        work_dir: str,
        env: Optional[Dict[str, str]],
        exec_id: str,
    ) -> ExecResult:
        """执行 Shell / PowerShell / 系统命令"""
        start_time = asyncio.get_event_loop().time()

        shell_cmd, shell_args = self._get_shell(language)
        cmd = [shell_cmd] + shell_args + [code]

        try:
            process = await asyncio.create_subprocess_shell(
                code,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=work_dir,
                env=self._build_env(env),
                shell=True,
            )

            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=timeout,
                )
                elapsed = asyncio.get_event_loop().time() - start_time
                stdout_str = stdout.decode("utf-8", errors="replace")
                stderr_str = stderr.decode("utf-8", errors="replace")

                return ExecResult(
                    success=process.returncode == 0,
                    stdout=stdout_str,
                    stderr=stderr_str,
                    exit_code=process.returncode or -1,
                    error=stderr_str if process.returncode != 0 else "",
                    execution_time=elapsed,
                )
            except asyncio.TimeoutError:
                process.kill()
                elapsed = asyncio.get_event_loop().time() - start_time
                return ExecResult(
                    success=False,
                    error=f"执行超时 ({timeout}s)",
                    execution_time=elapsed,
                )
        except Exception as e:
            elapsed = asyncio.get_event_loop().time() - start_time
            return ExecResult(
                success=False,
                error=str(e),
                execution_time=elapsed,
            )

    def _build_env(self, extra_env: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        """构建执行环境变量"""
        env = os.environ.copy()
        # 确保常见路径
        paths = env.get("PATH", "").split(os.pathsep)
        common_paths = [
            "/usr/local/bin", "/usr/bin", "/bin",
            str(Path.home() / ".local/bin"),
        ]
        for p in common_paths:
            if p not in paths:
                paths.insert(0, p)
        env["PATH"] = os.pathsep.join(paths)

        # Python 相关
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONDONTWRITEBYTECODE"] = "1"

        if extra_env:
            env.update(extra_env)

        return env

    async def _auto_fix(
        self,
        language: str,
        original_code: str,
        error_result: ExecResult,
        timeout: int,
        work_dir: str,
        env: Optional[Dict[str, str]],
    ) -> Optional[ExecResult]:
        """
        自动修复常见错误。

        支持的修复策略:
          - Python: ImportError → 自动 pip install
          - Python: IndentationError → 自动修复缩进
          - Shell: command not found → 提示安装
          - 通用: 截断过长输出
        """
        error_text = error_result.stderr or error_result.error or ""

        if language == "python":
            # 策略 1: 自动安装缺失的包
            if "ModuleNotFoundError" in error_text or "ImportError" in error_text:
                module = self._extract_missing_module(error_text)
                if module:
                    install_code = f"pip install {module}"
                    logger.info(f"[自动修复] 安装缺失模块: {module}")
                    install_result = await self._execute_shell(
                        "system", install_code, timeout, work_dir, env, "auto_fix"
                    )
                    if install_result.success:
                        # 安装成功，重新执行
                        return await self._execute_python(
                            original_code, timeout, work_dir, env, "retry"
                        )

            # 策略 2: 修复编码问题
            if "UnicodeEncodeError" in error_text:
                fixed_code = (
                    "import sys; sys.stdout.reconfigure(encoding='utf-8', errors='replace')\n"
                    + original_code
                )
                return await self._execute_python(
                    fixed_code, timeout, work_dir, env, "fix_encoding"
                )

            # 策略 3: 修复缩进
            if "IndentationError" in error_text:
                fixed_code = textwrap.dedent(original_code)
                if fixed_code != original_code:
                    return await self._execute_python(
                        fixed_code, timeout, work_dir, env, "fix_indent"
                    )

        elif language in ("shell", "bash", "system"):
            # Shell: 尝试添加 sudo
            if "Permission denied" in error_text:
                fixed_code = f"sudo {original_code}"
                return await self._execute_shell(
                    "system", fixed_code, timeout, work_dir, env, "fix_perm"
                )

            # Shell: command not found
            if "command not found" in error_text:
                cmd_name = self._extract_command_name(error_text)
                if cmd_name:
                    logger.info(f"[自动修复] 命令不存在: {cmd_name}")

        return None

    def _extract_missing_module(self, error_text: str) -> Optional[str]:
        """从错误信息中提取缺失的模块名"""
        import re
        # ModuleNotFoundError: No module named 'xxx'
        match = re.search(r"No module named ['\"](.+?)['\"]", error_text)
        if match:
            return match.group(1)
        # ImportError: cannot import name 'xxx' from 'yyy'
        match = re.search(r"cannot import name .+? from ['\"](.+?)['\"]", error_text)
        if match:
            return match.group(1)
        return None

    def _extract_command_name(self, error_text: str) -> Optional[str]:
        """从错误信息中提取命令名"""
        import re
        match = re.search(r"(\w+): command not found", error_text)
        if match:
            return match.group(1)
        return None

    def execute_sync(
        self,
        language: str,
        code: str,
        **kwargs,
    ) -> ExecResult:
        """同步执行(便捷方法)"""
        return asyncio.get_event_loop().run_until_complete(
            self.execute(language, code, **kwargs)
        )

    def get_stats(self) -> Dict[str, Any]:
        """获取执行统计"""
        return {
            "total_executions": self._execution_count,
            "timeout": self.timeout,
            "max_retries": self.max_retries,
            "auto_fix_enabled": self.auto_fix,
        }
