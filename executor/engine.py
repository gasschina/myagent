"""
executor/engine.py - 执行引擎
================================
Open Interpreter 风格的本地代码执行引擎。
支持 Python / Shell (Bash) / PowerShell / 系统命令。
特性:
  - 安全沙箱执行(超时控制、命令黑名单/正则)
  - 自动错误捕获与修复(12 种 Python 模式 + 4 种 Shell 模式)
  - 结构化结果返回
  - 跨平台兼容 (Windows/macOS/Linux)
  - 启动时缓存 PATH 与 Shell 检测
"""
from __future__ import annotations

import os
import re
import sys
import shutil
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
from core.utils import timestamp, truncate_str, detect_platform

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

    # ── 危险命令模式(正则 + 词边界) ──────────────────────────────────────────
    DANGEROUS_PATTERNS: List[Tuple[str, str]] = [
        # Unix destructive
        (r'\brm\s+(-[^\s]*r[^\s]*f[^\s]*|-[^\s]*f[^\s]*r[^\s]*)\s+/', 'rm -rf /'),
        (r'\brm\s+(-[^\s]*r[^\s]*f[^\s]*|-[^\s]*f[^\s]*r[^\s]*)\s+/\*', 'rm -rf /*'),
        (r'\bmkfs\b', 'mkfs (格式化磁盘)'),
        (r'\bdd\s+if=\s*/dev/', 'dd (直接磁盘写入)'),
        (r':\s*\(\)\s*\{.*\}\s*;', 'fork bomb'),
        (r'\bfork\s+bomb\b', 'fork bomb'),
        # Windows destructive
        (r'\bformat\s+[A-Za-z]:', 'format (格式化磁盘)'),
        (r'\bdel\s+/[fFsS]\s+/[sS]\s+/[qQ]\s+[A-Za-z]:', 'del /f /s /q (删除系统文件)'),
        # System
        (r'\bshutdown\s+-[hHrR]\s+now\b', 'shutdown -h now'),
        (r'\breboot\b', 'reboot'),
        (r'\binit\s+0\b', 'init 0'),
        (r'\bchmod\s+-R\s+777\s+/\s', 'chmod -R 777 / (开放根目录权限)'),
        (r'\bchown\s+-R\b', 'chown -R (递归更改所有权)'),
    ]

    # ── 旧式字符串匹配列表(兼容 & 自定义扩展) ──────────────────────────────
    DANGEROUS_COMMANDS = [
        "rm -rf /", "rm -rf /*", "mkfs", "dd if=/dev/zero",
        ":(){ :|:& };:", "fork bomb",
        "format C:", "del /f /s /q C:\\",
        "shutdown -h now", "reboot",
    ]

    # ── Python 常见拼写错误表 ───────────────────────────────────────────────
    COMMON_MISSPELLINGS: Dict[str, str] = {
        "pritn": "print", "prnit": "print", "pint": "print",
        "improt": "import", "imort": "import",
        "retun": "return", "retrun": "return", "reutrn": "return",
        "defualt": "default", "defautl": "default",
        "ture": "True", "flase": "False", "flase": "False",
        "lenght": "length", "lengh": "length",
        "recieve": "receive", "occured": "occurred",
        "seperator": "separator",
    }

    # ── Shell 命令别名表 ────────────────────────────────────────────────────
    SHELL_COMMAND_ALIASES: Dict[str, List[str]] = {
        "ls": ["dir"],           # Windows: dir instead of ls
        "dir": ["ls"],           # Unix: ls instead of dir
        "cat": ["type"],         # Windows: type instead of cat
        "type": ["cat"],
        "copy": ["cp"],          # Windows: copy instead of cp
        "cp": ["copy"],
        "del": ["rm"],
        "rm": ["del"],
        "move": ["mv"],
        "mv": ["move"],
        "cls": ["clear"],
        "clear": ["cls"],
        "find": ["fd", "rg"],
        "grep": ["findstr", "Select-String"],
        "ipconfig": ["ifconfig"],
        "ifconfig": ["ipconfig"],
        "tasklist": ["ps"],
        "ps": ["tasklist"],
        "echo": ["Write-Output"],
    }

    # ── Python builtins(用于 NameError 建议) ────────────────────────────────
    PYTHON_BUILTINS = set(dir(__builtins__)) if isinstance(__builtins__, dict) else set(dir(__builtins__))  # type: ignore[arg-type]

    def __init__(
        self,
        timeout: int = 300,
        max_retries: int = 2,
        auto_fix: bool = True,
        max_output_length: int = 50000,
        execution_mode: str = "local",
        sandbox_image: str = "python:3.12-slim",
        sandbox_network: bool = False,
        sandbox_memory: str = "512m",
        work_dir: Optional[str] = None,
        extra_blocked: Optional[List[str]] = None,
    ):
        self.timeout = timeout
        self.max_retries = max_retries
        self.auto_fix = auto_fix
        self.max_output_length = max_output_length
        self.execution_mode = execution_mode  # local | sandbox
        self.sandbox_image = sandbox_image
        self.sandbox_network = sandbox_network
        self.sandbox_memory = sandbox_memory
        self.work_dir = work_dir or os.getcwd()

        # 安全: 合并正则模式 + 字符串黑名单
        self._blocked = set(self.DANGEROUS_COMMANDS)
        if extra_blocked:
            self._blocked.update(extra_blocked)

        self._execution_count = 0

        # 沙盒模式: 检查 Docker 可用性
        self._docker_available = False
        if self.execution_mode == "sandbox":
            self._docker_available = self._check_docker()
            if not self._docker_available:
                logger.warning("沙盒模式: Docker 不可用，将回退到本机执行")
                self.execution_mode = "local"

        # ── 启动时缓存: 检测平台 & Shell ────────────────────────────────────
        self._platform = detect_platform()
        self._available_shells: Dict[str, str] = {}
        self._detect_shells()

        # ── 启动时缓存: 基础环境变量(PATH 等) ──────────────────────────────
        self._cached_base_env = self._build_env(None)

    # ======================================================================
    # 启动时一次性检测
    # ======================================================================

    def _check_docker(self) -> bool:
        """检查 Docker 是否可用。"""
        try:
            proc = subprocess.run(
                ["docker", "info"],
                capture_output=True, timeout=10,
            )
            return proc.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False

    def _detect_shells(self) -> None:
        """在 __init__ 中调用一次，探测可用 Shell 并缓存路径。"""
        if self._platform == "windows":
            # Windows: 依次检测 bash (Git Bash / MSYS2 / WSL) / cmd / PowerShell
            candidates = [
                ("bash", shutil.which("bash")),
                ("git_bash", r"C:\Program Files\Git\bin\bash.exe"),
                ("git_bash_x86", r"C:\Program Files (x86)\Git\bin\bash.exe"),
                ("msys2_bash", r"C:\msys64\usr\bin\bash.exe"),
                ("msys2_mingw", r"C:\msys64\mingw64\bin\bash.exe"),
                ("wsl_bash", shutil.which("wsl")),
                ("cmd", "cmd"),
                ("powershell", shutil.which("powershell")),
                ("powershell_x86", r"C:\Windows\SysWOW64\WindowsPowerShell\v1.0\powershell.exe"),
                ("pwsh", shutil.which("pwsh")),
            ]
            for name, path in candidates:
                if path and (shutil.which(path) is not None or path == "cmd"
                             or (isinstance(path, str) and os.path.isfile(path))):
                    self._available_shells[name] = path
        else:
            # macOS / Linux: 检测 bash / zsh / sh / fish
            for shell_name in ("bash", "zsh", "sh", "dash", "fish", "ksh"):
                found = shutil.which(shell_name)
                if found:
                    self._available_shells[shell_name] = found
            # 用户默认 Shell
            user_shell = os.environ.get("SHELL", "")
            if user_shell and os.path.isfile(user_shell):
                self._available_shells.setdefault("default", user_shell)

        logger.debug(f"[引擎初始化] 检测到 Shell: {list(self._available_shells.keys())}")

    # ======================================================================
    # 安全检查(正则 + 词边界)
    # ======================================================================

    @staticmethod
    def _normalize_command(code: str) -> str:
        """
        标准化命令字符串用于安全检测。
        - 合并连续空白为单个空格
        - 去除首尾空白
        """
        normalized = re.sub(r'\s+', ' ', code.strip())
        return normalized

    def _check_safety(self, code: str) -> Tuple[bool, str]:
        """检查代码安全性: 正则模式(词边界) + 旧式字符串匹配。"""
        normalized = self._normalize_command(code)

        # 1) 正则模式匹配(词边界)
        for pattern, description in self.DANGEROUS_PATTERNS:
            try:
                if re.search(pattern, normalized, re.IGNORECASE):
                    return False, f"危险命令被拦截: {description}"
            except re.error:
                logger.warning(f"[安全检查] 无效正则模式: {pattern}")
                continue

        # 2) 旧式字符串匹配(兼容)
        code_lower = normalized.lower()
        for dangerous in self._blocked:
            if dangerous.lower() in code_lower:
                return False, f"危险命令被拦截: {dangerous}"

        return True, ""

    # ======================================================================
    # Shell 选择(使用缓存)
    # ======================================================================

    def _get_shell(self, language: str) -> Tuple[str, List[str]]:
        """获取执行 shell 和参数(利用启动时缓存)。"""
        if language == "python":
            return sys.executable, ["-u", "-c"]

        if language in ("shell", "bash"):
            if self._platform == "windows":
                # 优先 Git Bash / MSYS2 bash
                for key in ("git_bash", "git_bash_x86", "msys2_bash", "msys2_mingw", "bash"):
                    if key in self._available_shells:
                        return self._available_shells[key], ["-c"]
                return "cmd", ["/c"]
            # macOS/Linux: 优先 bash → zsh → sh
            for key in ("bash", "zsh", "sh"):
                if key in self._available_shells:
                    return self._available_shells[key], ["-c"]
            return "sh", ["-c"]

        if language == "powershell":
            if self._platform == "windows":
                path = self._available_shells.get("powershell", "powershell")
                return path, ["-NoProfile", "-Command"]
            path = self._available_shells.get("pwsh", "pwsh")
            return path, ["-NoProfile", "-Command"]

        if language == "cmd":
            return "cmd", ["/c"]

        if language == "system":
            if self._platform == "windows":
                return "cmd", ["/c"]
            for key in ("bash", "zsh", "sh"):
                if key in self._available_shells:
                    return self._available_shells[key], ["-c"]
            return "sh", ["-c"]

        raise ValueError(f"不支持的语言: {language}")

    def _detect_shell_for_code(self, code: str, language: str) -> Optional[Tuple[str, List[str]]]:
        """
        根据代码内容推断应使用的 Shell。
        - Windows 上 .bat/.cmd → cmd.exe /c
        - .ps1 → PowerShell
        - macOS/Linux 上检测 shebang 或脚本扩展名
        """
        if language not in ("shell", "bash", "system"):
            return None

        # Windows 特殊路由
        if self._platform == "windows":
            # 检测 .bat/.cmd 引用
            bat_match = re.search(r'[\w.\-]+\.(bat|cmd)\b', code, re.IGNORECASE)
            if bat_match:
                return "cmd", ["/c"]
            # 检测 .ps1 引用
            ps1_match = re.search(r'[\w.\-]+\.ps1\b', code, re.IGNORECASE)
            if ps1_match:
                ps_path = self._available_shells.get("powershell") or "powershell"
                return ps_path, ["-NoProfile", "-Command"]
        else:
            # macOS/Linux: 检测 shebang
            shebang_match = re.search(r'^#!\s*/(?:usr/(?:local/)?)?(?:bin|env)/(\w+)', code, re.MULTILINE)
            if shebang_match:
                shell_name = shebang_match.group(1)
                if shell_name in self._available_shells:
                    return self._available_shells[shell_name], ["-c"]

        return None

    # ======================================================================
    # 环境变量(使用缓存)
    # ======================================================================

    def _build_env(self, extra_env: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        """构建执行环境变量。"""
        env = os.environ.copy()

        # 确保常见路径
        paths = env.get("PATH", "").split(os.pathsep)
        common_paths: List[str] = []

        if self._platform == "windows":
            common_paths = [
                r"C:\Windows\System32",
                r"C:\Windows",
                r"C:\Program Files\Git\bin",
                r"C:\Program Files (x86)\Git\bin",
                r"C:\msys64\usr\bin",
                r"C:\msys64\mingw64\bin",
            ]
        else:
            common_paths = [
                "/usr/local/bin",
                "/usr/bin",
                "/bin",
                "/usr/sbin",
                "/sbin",
                str(Path.home() / ".local" / "bin"),
                str(Path.home() / ".cargo" / "bin"),
                str(Path.home() / ".npm-global" / "bin"),
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

    def _get_env(self, extra_env: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        """获取环境变量(使用缓存的基础环境 + 额外变量)。"""
        if extra_env:
            env = dict(self._cached_base_env)
            env.update(extra_env)
            return env
        return self._cached_base_env

    # ======================================================================
    # 异步执行
    # ======================================================================

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
                metadata={**metadata, "mode": self.execution_mode},
            )

        self._execution_count += 1
        exec_id = f"exec_{self._execution_count}"

        mode_label = "本地" if self.execution_mode == "local" else "沙盒(Docker)"
        logger.info(f"[{exec_id}] 开始执行 ({language}, mode={mode_label}, timeout={exec_timeout}s)")
        logger.debug(f"[{exec_id}] 代码:\n{code[:500]}")

        # 根据执行模式选择执行方式
        if self.execution_mode == "sandbox" and self._docker_available:
            result = await self._execute_sandbox(language, code, exec_timeout, work_dir, env, exec_id)
        elif language == "python":
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
                env=self._get_env(env),
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
        """
        执行 Shell / PowerShell / 系统命令。

        [BUG FIX] 现在正确使用 shell_cmd + shell_args 进行路由:
          - _get_shell(language) 返回 shell 可执行路径与参数
          - _detect_shell_for_code() 可根据代码内容覆盖(如 .bat/.cmd/.ps1)
          - 使用 create_subprocess_exec 而非 create_subprocess_shell
        """
        start_time = asyncio.get_event_loop().time()

        # 1) 根据语言获取默认 Shell
        shell_cmd, shell_args = self._get_shell(language)

        # 2) 根据代码内容推断是否应覆盖 Shell
        override = self._detect_shell_for_code(code, language)
        if override is not None:
            shell_cmd, shell_args = override
            logger.debug(f"[{exec_id}] Shell 被代码内容覆盖为: {shell_cmd}")

        # 3) 构建命令列表: [shell_cmd, shell_arg..., code_as_string]
        cmd = [shell_cmd] + shell_args + [code]

        logger.debug(f"[{exec_id}] Shell 命令: {shell_cmd} {' '.join(shell_args)}")

        try:
            # [FIX] 使用 create_subprocess_exec + 已计算的 shell_cmd/shell_args
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=work_dir,
                env=self._get_env(env),
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

    # ======================================================================
    # 沙盒执行 (Docker 容器)
    # ======================================================================

    async def _execute_sandbox(
        self,
        language: str,
        code: str,
        timeout: int,
        work_dir: str,
        env: Optional[Dict[str, str]],
        exec_id: str,
    ) -> ExecResult:
        """在 Docker 容器中执行代码（沙盒模式）。"""
        start_time = asyncio.get_event_loop().time()

        # 确定容器内的执行命令
        if language == "python":
            container_cmd = ["python3", "-c", code]
        elif language in ("shell", "bash", "system"):
            container_cmd = ["bash", "-c", code]
        elif language == "powershell":
            return ExecResult(error="PowerShell 不支持沙盒模式")
        else:
            container_cmd = ["sh", "-c", code]

        docker_cmd = [
            "docker", "run", "--rm",
            "--memory", self.sandbox_memory,
            "--cpus", "1",
            "--pids-limit", "64",
            "--workdir", "/workspace",
        ]

        if not self.sandbox_network:
            docker_cmd.append("--network=none")

        # 挂载工作目录为只读
        if os.path.isdir(work_dir):
            docker_cmd.extend(["-v", f"{work_dir}:/workspace:ro"])

        docker_cmd.extend([self.sandbox_image] + container_cmd)

        logger.info(f"[{exec_id}] 沙盒执行: docker run {self.sandbox_image}")

        try:
            process = await asyncio.create_subprocess_exec(
                *docker_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=work_dir,
                env=self._get_env(env),
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(), timeout=timeout
                )
                elapsed = asyncio.get_event_loop().time() - start_time
                return ExecResult(
                    success=process.returncode == 0,
                    stdout=stdout.decode("utf-8", errors="replace"),
                    stderr=stderr.decode("utf-8", errors="replace"),
                    exit_code=process.returncode or -1,
                    error=stderr.decode("utf-8", errors="replace") if process.returncode != 0 else "",
                    execution_time=elapsed,
                    metadata={"mode": "sandbox", "image": self.sandbox_image},
                )
            except asyncio.TimeoutError:
                process.kill()
                elapsed = asyncio.get_event_loop().time() - start_time
                return ExecResult(
                    success=False,
                    error=f"沙盒执行超时 ({timeout}s)",
                    execution_time=elapsed,
                    metadata={"mode": "sandbox"},
                )
        except FileNotFoundError:
            # Docker 不存在，回退到本地执行
            logger.warning(f"[{exec_id}] Docker 不可用，回退到本地执行")
            self.execution_mode = "local"
            if language == "python":
                return await self._execute_python(code, timeout, work_dir, env, exec_id)
            else:
                return await self._execute_shell(language, code, timeout, work_dir, env, exec_id)
        except Exception as e:
            elapsed = asyncio.get_event_loop().time() - start_time
            return ExecResult(
                success=False,
                error=f"沙盒执行异常: {e}",
                execution_time=elapsed,
                metadata={"mode": "sandbox"},
            )

    def set_execution_mode(self, mode: str) -> bool:
        """切换执行模式。返回是否切换成功。"""
        if mode not in ("local", "sandbox"):
            return False
        if mode == "sandbox":
            if not self._check_docker():
                logger.warning("沙盒模式: Docker 不可用")
                return False
            self._docker_available = True
        self.execution_mode = mode
        logger.info(f"执行模式已切换: {mode}")
        return True

    def get_execution_info(self) -> Dict[str, Any]:
        """获取当前执行模式信息。"""
        return {
            "mode": self.execution_mode,
            "docker_available": self._docker_available,
            "sandbox_image": self.sandbox_image,
            "sandbox_network": self.sandbox_network,
            "sandbox_memory": self.sandbox_memory,
            "execution_count": self._execution_count,
        }

    # ======================================================================
    # 自动修复(增强: 12 种 Python + 4 种 Shell)
    # ======================================================================

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

        Python (12 种模式):
          1. ModuleNotFoundError → pip install
          2. ImportError: cannot import name → 建议正确导入路径
          3. NameError → 检查拼写、内置名
          4. SyntaxError: unexpected EOF → 补全括号
          5. TypeError: takes Y arguments → 建议签名
          6. FileNotFoundError → 建议路径
          7. PermissionError → 建议权限(不自动 sudo)
          8. ConnectionError / TimeoutError → 网络建议
          9. UnicodeEncodeError → 编码头
          10. IndentationError → 自动修复缩进
          11. KeyError → 建议可用 key
          12. JSONDecodeError → JSON 校验建议

        Shell (4 种模式):
          1. command not found → 正确命令别名
          2. No such file or directory → 路径建议
          3. Permission denied → chmod 提示(不自动 sudo)
          4. syntax error near unexpected token → 引号匹配
        """
        error_text = error_result.stderr or error_result.error or ""

        if language == "python":
            return await self._auto_fix_python(original_code, error_text, timeout, work_dir, env)
        elif language in ("shell", "bash", "system"):
            return await self._auto_fix_shell(original_code, error_text, timeout, work_dir, env, error_result)

        return None

    # ── Python 自动修复 ────────────────────────────────────────────────────

    async def _auto_fix_python(
        self,
        code: str,
        error_text: str,
        timeout: int,
        work_dir: str,
        env: Optional[Dict[str, str]],
    ) -> Optional[ExecResult]:
        """Python 12 种自动修复模式。"""

        # ── 1. ModuleNotFoundError → pip install ──────────────────────────
        if "ModuleNotFoundError" in error_text:
            module = self._extract_missing_module(error_text)
            if module:
                install_code = f"pip install {module}"
                logger.info(f"[自动修复] 安装缺失模块: {module}")
                install_result = await self._execute_shell(
                    "system", install_code, timeout, work_dir, env, "auto_fix_install"
                )
                if install_result.success:
                    return await self._execute_python(
                        code, timeout, work_dir, env, "retry_module"
                    )
                # 安装失败则继续尝试其他修复策略

        # ── 2. ImportError: cannot import name → 建议正确导入路径 ─────────
        if "ImportError" in error_text and "cannot import name" in error_text:
            match = re.search(r"cannot import name ['\"](\w+)['\"]", error_text)
            if match:
                bad_name = match.group(1)
                # 检查模块是否存在(可能包名不同)
                module_match = re.search(r"from ['\"](.+?)['\"]", error_text)
                module_name = module_match.group(1) if module_match else "unknown"
                logger.info(
                    f"[自动修复] ImportError: '{bad_name}' 不在 '{module_name}' 中。"
                    f"请检查模块版本或尝试: from {module_name} import *"
                )

        # ── 3. NameError: name 'X' is not defined → 检查拼写/内置 ───────
        if "NameError" in error_text:
            match = re.search(r"name '(\w+)' is not defined", error_text)
            if match:
                undefined_name = match.group(1)
                suggestion = self._suggest_name_fix(undefined_name)
                if suggestion:
                    logger.info(f"[自动修复] NameError: '{undefined_name}' → 建议 '{suggestion}'")
                    # 尝试自动替换
                    fixed_code = re.sub(
                        r'\b' + re.escape(undefined_name) + r'\b',
                        suggestion,
                        code,
                        count=1,
                    )
                    if fixed_code != code:
                        return await self._execute_python(
                            fixed_code, timeout, work_dir, env, "fix_name"
                        )

        # ── 4. SyntaxError: unexpected EOF → 补全括号 ──────────────────
        if "SyntaxError" in error_text and "unexpected EOF" in error_text:
            fixed_code = self._fix_unclosed_brackets(code)
            if fixed_code != code:
                return await self._execute_python(
                    fixed_code, timeout, work_dir, env, "fix_eof"
                )

        # ── 5. TypeError: X() takes Y arguments → 建议签名 ─────────────
        if "TypeError" in error_text:
            match = re.search(
                r"(\w+)\(\) (takes|missing) (\d+ (?:positional )?argument|at least \d+)",
                error_text,
            )
            if match:
                func_name = match.group(1)
                logger.info(
                    f"[自动修复] TypeError: {func_name}() 参数数量不匹配。"
                    f"请检查函数签名。"
                )

        # ── 6. FileNotFoundError → 建议路径 ────────────────────────────
        if "FileNotFoundError" in error_text:
            match = re.search(r"No such file or directory: ['\"](.+?)['\"]", error_text)
            if match:
                missing_path = match.group(1)
                dir_part = os.path.dirname(missing_path)
                logger.info(
                    f"[自动修复] FileNotFoundError: '{missing_path}'。"
                    f"请检查路径是否存在。尝试列出目录: ls {dir_part or '.'}"
                )
                # 尝试列出目录内容作为提示
                if dir_part and os.path.isdir(dir_part):
                    listing = os.listdir(dir_part)
                    logger.info(f"  目录内容: {listing[:20]}")

        # ── 7. PermissionError → 建议权限(不自动 sudo) ─────────────────
        if "PermissionError" in error_text:
            match = re.search(r"\[Errno 13\] Permission denied: ['\"](.+?)['\"]", error_text)
            if match:
                denied_path = match.group(1)
                logger.info(
                    f"[自动修复] PermissionError: '{denied_path}'。"
                    f"请手动检查文件权限: ls -la '{denied_path}'"
                )

        # ── 8. ConnectionError / TimeoutError → 网络建议 ────────────────
        if "ConnectionError" in error_text or "TimeoutError" in error_text or "ConnectionRefusedError" in error_text:
            logger.info(
                "[自动修复] 网络连接失败。"
                "请检查网络连接、代理设置或目标服务是否可用。"
            )

        # ── 9. UnicodeEncodeError → 编码头 ──────────────────────────────
        if "UnicodeEncodeError" in error_text:
            fixed_code = (
                "import sys; sys.stdout.reconfigure(encoding='utf-8', errors='replace')\n"
                + code
            )
            return await self._execute_python(
                fixed_code, timeout, work_dir, env, "fix_encoding"
            )

        # ── 10. IndentationError → 自动修复缩进 ─────────────────────────
        if "IndentationError" in error_text:
            fixed_code = self._fix_indentation(code)
            if fixed_code != code:
                return await self._execute_python(
                    fixed_code, timeout, work_dir, env, "fix_indent"
                )

        # ── 11. KeyError → 建议可用 key ────────────────────────────────
        if "KeyError" in error_text:
            match = re.search(r"KeyError: ['\"](.+?)['\"]", error_text)
            if match:
                missing_key = match.group(1)
                logger.info(
                    f"[自动修复] KeyError: '{missing_key}'。"
                    f"字典中不存在该键，请检查键名拼写。"
                )

        # ── 12. json.decoder.JSONDecodeError → JSON 校验建议 ────────────
        if "JSONDecodeError" in error_text:
            logger.info(
                "[自动修复] JSONDecodeError: JSON 格式无效。"
                "请检查 JSON 字符串是否正确(缺少引号、多余逗号、未转义字符等)。"
            )

        return None

    # ── Shell 自动修复 ─────────────────────────────────────────────────────

    async def _auto_fix_shell(
        self,
        code: str,
        error_text: str,
        timeout: int,
        work_dir: str,
        env: Optional[Dict[str, str]],
        error_result: ExecResult,
    ) -> Optional[ExecResult]:
        """Shell 4 种自动修复模式。"""

        # ── 1. command not found → 正确命令别名 ──────────────────────────
        if "command not found" in error_text:
            cmd_name = self._extract_command_name(error_text)
            if cmd_name:
                alias = self._suggest_shell_alias(cmd_name)
                logger.info(
                    f"[自动修复] 命令不存在: '{cmd_name}'。"
                    f"你是否想用: {alias}?"
                )

        # ── 2. No such file or directory → 路径建议 ─────────────────────
        if "No such file or directory" in error_text:
            match = re.search(r"No such file or directory:\s*'(.+?)'", error_text)
            if not match:
                match = re.search(r":\s*(\S+):\s*No such file or directory", error_text)
            if match:
                missing_path = match.group(1)
                logger.info(
                    f"[自动修复] 文件/目录不存在: '{missing_path}'。"
                    f"请检查路径是否正确。尝试: ls {os.path.dirname(missing_path) or '.'}"
                )

        # ── 3. Permission denied → chmod 提示(不自动 sudo) ─────────────
        if "Permission denied" in error_text:
            logger.warning(
                f"[自动修复] 权限不足，已拦截自动 sudo 执行。"
                f"请用户手动以提升权限运行该命令。原始命令: {code[:200]}"
            )
            # [SECURITY FIX] 不自动执行 sudo，返回提示
            return ExecResult(
                success=False,
                error=(
                    "⚠️ 权限不足: 命令需要提升权限。\n"
                    "安全策略: 引擎不会自动使用 sudo。\n"
                    f"请手动执行: sudo {code}"
                ),
                stderr=error_text,
                exit_code=error_result.exit_code,
                metadata={"sudo_blocked": True, "original_code": code},
            )

        # ── 4. syntax error near unexpected token → 引号匹配 ────────────
        if "syntax error near unexpected token" in error_text:
            token_match = re.search(r"syntax error near unexpected token\s+`(.+?)'", error_text)
            token = token_match.group(1) if token_match else "unknown"
            logger.info(
                f"[自动修复] Shell 语法错误，unexpected token: '{token}'。"
                f"请检查引号是否正确匹配，以及是否有多余或缺失的特殊字符。"
            )

        return None

    # ======================================================================
    # 辅助方法
    # ======================================================================

    def _extract_missing_module(self, error_text: str) -> Optional[str]:
        """从错误信息中提取缺失的模块名"""
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
        match = re.search(r"(\w+):\s*command not found", error_text)
        if match:
            return match.group(1)
        return None

    def _suggest_name_fix(self, name: str) -> Optional[str]:
        """
        对 NameError 中的未定义名称提供建议:
          1. 检查常见拼写错误表
          2. 检查 Python 内置名
          3. 使用编辑距离(简单 Levenshtein)查找相近名
        """
        name_lower = name.lower()

        # 1) 精确拼写错误
        if name_lower in self.COMMON_MISSPELLINGS:
            return self.COMMON_MISSPELLINGS[name_lower]

        # 2) 内置名(精确匹配)
        if name in self.PYTHON_BUILTINS:
            return name  # 已是内置名但可能大小写不对

        # 3) 内置名(忽略大小写)
        for builtin in self.PYTHON_BUILTINS:
            if isinstance(builtin, str) and builtin.lower() == name_lower:
                return builtin

        # 4) 简单编辑距离(距离 ≤ 2 视为建议)
        best_match: Optional[str] = None
        best_dist = 3
        for builtin in self.PYTHON_BUILTINS:
            if not isinstance(builtin, str):
                continue
            dist = self._levenshtein_distance(name.lower(), builtin.lower())
            if dist < best_dist and dist <= 2:
                best_dist = dist
                best_match = builtin

        return best_match

    @staticmethod
    def _levenshtein_distance(s1: str, s2: str) -> int:
        """简单 Levenshtein 编辑距离。"""
        if len(s1) < len(s2):
            return ExecutionEngine._levenshtein_distance(s2, s1)
        if len(s2) == 0:
            return len(s1)
        prev_row = list(range(len(s2) + 1))
        for i, c1 in enumerate(s1):
            curr_row = [i + 1]
            for j, c2 in enumerate(s2):
                insertions = prev_row[j + 1] + 1
                deletions = curr_row[j] + 1
                substitutions = prev_row[j] + (c1 != c2)
                curr_row.append(min(insertions, deletions, substitutions))
            prev_row = curr_row
        return prev_row[-1]

    def _fix_unclosed_brackets(self, code: str) -> str:
        """尝试补全未闭合的括号/方括号/花括号。"""
        open_brackets = "([{"
        close_brackets = ")]}"
        stack: List[str] = []
        # 只检查行尾，不解析字符串内容(简化版)
        for char in code:
            if char in open_brackets:
                stack.append(char)
            elif char in close_brackets:
                if stack:
                    stack.pop()
        # 补全未闭合的
        if stack:
            fix_map = {"(": ")", "[": "]", "{": "}"}
            code += "\n" + "".join(fix_map.get(c, "") for c in reversed(stack))
        return code

    def _fix_indentation(self, code: str) -> str:
        """修复常见缩进问题。"""
        # 1) textwrap.dedent 移除整体缩进
        fixed = textwrap.dedent(code)
        # 2) Tab → 4 空格
        fixed = fixed.replace("\t", "    ")
        # 3) 移除行尾空白
        fixed = "\n".join(line.rstrip() for line in fixed.splitlines()) + ("\n" if code.endswith("\n") else "")
        return fixed

    def _suggest_shell_alias(self, cmd_name: str) -> str:
        """根据平台为 command not found 的命令建议正确名称。"""
        aliases = self.SHELL_COMMAND_ALIASES.get(cmd_name, [])
        if aliases:
            return " / ".join(aliases)
        return f"(无已知别名，请检查命令拼写或确认已安装)"

    # ======================================================================
    # 同步执行(修复 asyncio 安全模式)
    # ======================================================================

    def execute_sync(
        self,
        language: str,
        code: str,
        **kwargs,
    ) -> ExecResult:
        """
        同步执行(便捷方法)。

        [BUG FIX] 安全处理 asyncio 事件循环:
          - 如果已有运行中的 loop → 使用 ThreadPoolExecutor 隔离
          - 否则 → 直接 asyncio.run()
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                return pool.submit(
                    asyncio.run, self.execute(language, code, **kwargs)
                ).result()
        else:
            return asyncio.run(self.execute(language, code, **kwargs))

    # ======================================================================
    # 统计信息
    # ======================================================================

    def get_stats(self) -> Dict[str, Any]:
        """获取执行统计"""
        return {
            "total_executions": self._execution_count,
            "timeout": self.timeout,
            "max_retries": self.max_retries,
            "auto_fix_enabled": self.auto_fix,
            "platform": self._platform,
            "available_shells": list(self._available_shells.keys()),
            "env_cached": True,
        }
