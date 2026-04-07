"""
执行引擎模块 - Open Interpreter 风格的本地代码执行能力
=====================================================
支持 Python / Shell / PowerShell / 系统命令
自动捕获错误、自动修复、自动重试
执行结果结构化返回
"""
import os
import sys
import subprocess
import tempfile
import signal
import platform
import threading
import shutil
import time
import re
import json
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from dataclasses import dataclass, field, asdict
from enum import Enum

from config import get_config


# ============================================================
# 执行结果数据结构
# ============================================================

class ExecutionStatus(Enum):
    SUCCESS = "success"
    FAILED = "failed"
    TIMEOUT = "timeout"
    BLOCKED = "blocked"       # 命令被阻止
    ERROR = "error"           # 执行引擎内部错误


@dataclass
class ExecutionResult:
    """结构化执行结果 - 确保 LLM 能稳定理解"""
    success: bool = False
    status: str = ExecutionStatus.FAILED.value
    language: str = ""          # python / shell / powershell / system
    code: str = ""              # 执行的代码
    stdout: str = ""            # 标准输出
    stderr: str = ""            # 标准错误
    exit_code: Optional[int] = None
    error: str = ""             # 错误描述
    error_type: str = ""        # 错误类型 (用于记忆系统学习)
    fix_attempted: bool = False # 是否尝试过自动修复
    fix_applied: str = ""       # 应用的修复内容
    duration_ms: float = 0      # 执行耗时
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_llm_dict(self) -> Dict[str, Any]:
        """转换为 LLM 友好的字典格式"""
        result = {
            "success": self.success,
            "status": self.status,
            "exit_code": self.exit_code,
        }
        if self.stdout:
            result["output"] = self.stdout[:10000]
        if self.stderr:
            result["errors"] = self.stderr[:5000]
        if self.error:
            result["error"] = self.error[:2000]
        if self.fix_attempted:
            result["fix_applied"] = self.fix_applied[:1000]
        result["duration_ms"] = round(self.duration_ms, 1)
        return result

    def to_json(self) -> str:
        return json.dumps(self.to_llm_dict(), ensure_ascii=False, indent=2)


# ============================================================
# 安全检查
# ============================================================

class SecurityChecker:
    """执行安全检查器"""

    def __init__(self):
        cfg = get_config()
        self.blocked_commands = cfg.get("executor.blocked_commands", [])
        self.allowed_commands = cfg.get("executor.allowed_commands", [])
        self.allow_all = len(self.allowed_commands) == 0

        # 危险模式正则
        self.dangerous_patterns = [
            r'rm\s+-rf\s+/', r'del\s+/[fFsS]\s+/[aA]\s+/[qQ]\s+[A-Z]:\\',
            r'mkfs\.', r'format\s+[A-Z]:', r'diskpart',
            r':\(\)\{\s*:\|:&\s*\}\;',  # fork bomb
            r'>\s*/dev/sd[a-z]',
            r'dd\s+if=.*of=/dev/',
            r'chmod\s+-R\s+777\s+/',
            r'shutdown\s+[-/]', r'reboot\s', r'halt\s', r'poweroff',
        ]

    def check(self, code: str, language: str) -> Tuple[bool, str]:
        """
        检查代码是否安全
        返回: (is_safe, reason)
        """
        code_stripped = code.strip()

        # 检查被阻止的命令
        for blocked in self.blocked_commands:
            if blocked.lower() in code_stripped.lower():
                return False, f"命令被阻止: {blocked}"

        # 检查危险模式
        for pattern in self.dangerous_patterns:
            if re.search(pattern, code_stripped, re.IGNORECASE):
                return False, f"检测到危险操作模式: {pattern}"

        # 白名单检查
        if not self.allow_all:
            first_word = code_stripped.split()[0] if code_stripped.split() else ""
            if first_word not in self.allowed_commands:
                return False, f"命令不在白名单中: {first_word}"

        return True, ""


# ============================================================
# 自动修复器
# ============================================================

class AutoFixer:
    """代码自动修复 - 基于常见错误模式"""

    # 常见 Python 错误修复规则
    PYTHON_FIXES = [
        {
            "pattern": r"NameError: name '(\w+)' is not defined",
            "fix": "变量未定义，请先定义或导入该变量",
            "apply": None,  # 无法自动修复，只给提示
        },
        {
            "pattern": r"ModuleNotFoundError: No module named '(\w+)'",
            "fix": "pip install {0}",
            "apply": lambda m: f"import subprocess\nsubprocess.run(['pip', 'install', '{m.group(1)}'], check=True)\n",
            "install_cmd": True,
        },
        {
            "pattern": r"IndentationError.*",
            "fix": "缩进错误，Python要求严格的缩进对齐",
            "apply": None,
        },
        {
            "pattern": r"SyntaxError.*",
            "fix": "语法错误，请检查代码语法",
            "apply": None,
        },
        {
            "pattern": r"FileNotFoundError.*'(.+?)'",
            "fix": "文件不存在，尝试创建目录或检查路径",
            "apply": lambda m: f"import os\nos.makedirs(os.path.dirname('{m.group(1)}'), exist_ok=True)\n",
        },
        {
            "pattern": r"PermissionError.*",
            "fix": "权限不足，尝试使用管理员权限或修改文件权限",
            "apply": None,
        },
        {
            "pattern": r"UnicodeDecodeError.*",
            "fix": "编码错误，尝试指定 encoding='utf-8' 参数",
            "apply": None,
        },
        {
            "pattern": r"KeyError: '(\w+)'",
            "fix": "字典键不存在，请检查键名或使用 .get() 方法",
            "apply": None,
        },
        {
            "pattern": r"TypeError.*",
            "fix": "类型错误，请检查操作数类型是否匹配",
            "apply": None,
        },
        {
            "pattern": r"IndexError.*",
            "fix": "索引越界，请检查列表/元组长度",
            "apply": None,
        },
    ]

    # Shell/PowerShell 错误修复规则
    SHELL_FIXES = [
        {
            "pattern": r"command not found.*",
            "fix": "命令不存在，请检查是否安装或路径是否正确",
            "apply": None,
        },
        {
            "pattern": r"No such file or directory.*",
            "fix": "文件或目录不存在",
            "apply": None,
        },
        {
            "pattern": r"Permission denied.*",
            "fix": "权限不足",
            "apply": None,
        },
        {
            "pattern": r"not recognized.*",
            "fix": "Windows下命令未识别，请检查命令拼写或安装路径",
            "apply": None,
        },
    ]

    def __init__(self):
        self.security_checker = SecurityChecker()

    def try_fix(
        self,
        code: str,
        error_output: str,
        language: str
    ) -> Tuple[Optional[str], str]:
        """
        尝试自动修复代码
        返回: (fixed_code, fix_description)
        如果无法修复，返回 (None, explanation)
        """
        fixes = self.PYTHON_FIXES if language == "python" else self.SHELL_FIXES

        for rule in fixes:
            match = re.search(rule["pattern"], error_output)
            if match:
                apply_fn = rule.get("apply")
                if apply_fn and callable(apply_fn):
                    # 有自动修复函数
                    fixed_code = apply_fn(match)

                    # 安全检查修复后的代码
                    is_safe, reason = self.security_checker.check(fixed_code, language)
                    if not is_safe:
                        return None, f"自动修复代码未通过安全检查: {reason}"

                    return fixed_code, f"自动修复: {rule['fix'].format(*match.groups()) if match.groups() else rule['fix']}"
                else:
                    return None, rule['fix'].format(*match.groups()) if match.groups() else rule['fix']

        return None, "无法自动修复此错误"


# ============================================================
# 执行引擎
# ============================================================

class Executor:
    """
    核心执行引擎
    支持 Python / Shell (bash/zsh) / PowerShell / 系统命令
    """

    def __init__(self):
        cfg = get_config()
        self.timeout = cfg.get("executor.timeout", 300)
        self.max_retries = cfg.get("executor.max_retries", 3)
        self.auto_fix_attempts = cfg.get("executor.auto_fix_attempts", 2)
        self.work_dir = cfg.get("executor.work_dir") or os.getcwd()
        self.python_path = cfg.get("executor.python_path", "python3")
        self.max_output_length = cfg.get("executor.max_output_length", 50000)
        self.security_checker = SecurityChecker()
        self.auto_fixer = AutoFixer()

        # 确定平台默认 shell
        self.system = platform.system()
        if self.system == "Windows":
            self.default_shell = "powershell"
            self.shell_cmd = ["powershell", "-NoProfile", "-NonInteractive", "-Command"]
        elif self.system == "Darwin":
            self.default_shell = "shell"
            self.shell_cmd = ["/bin/zsh", "-c"]
        else:
            self.default_shell = "shell"
            self.shell_cmd = ["/bin/bash", "-c"]

        # 执行统计
        self._stats = {
            "total_executions": 0,
            "successful": 0,
            "failed": 0,
            "auto_fixed": 0,
            "blocked": 0,
        }
        self._lock = threading.Lock()

    def execute(
        self,
        code: str,
        language: str = "auto",
        work_dir: Optional[str] = None,
        timeout: Optional[int] = None,
        env: Optional[Dict[str, str]] = None,
    ) -> ExecutionResult:
        """
        执行代码 (主入口)

        参数:
            code: 要执行的代码
            language: python / shell / powershell / system / auto
            work_dir: 工作目录
            timeout: 超时秒数
            env: 环境变量

        返回:
            ExecutionResult 结构化结果
        """
        start_time = time.time()

        # 自动检测语言
        if language == "auto":
            language = self._detect_language(code)

        # 安全检查
        is_safe, reason = self.security_checker.check(code, language)
        if not is_safe:
            with self._lock:
                self._stats["blocked"] += 1
            return ExecutionResult(
                success=False,
                status=ExecutionStatus.BLOCKED.value,
                language=language,
                code=code,
                error=reason,
                duration_ms=(time.time() - start_time) * 1000,
            )

        # 确定工作目录
        exec_dir = work_dir or self.work_dir
        exec_timeout = timeout or self.timeout

        # 带重试的执行
        current_code = code
        result = None

        for attempt in range(self.max_retries + 1):
            if attempt > 0:
                # 重试前等待
                time.sleep(min(2 ** attempt, 10))

            result = self._execute_single(
                current_code, language, exec_dir, exec_timeout, env
            )

            if result.success:
                break

            # 尝试自动修复
            if attempt < self.auto_fix_attempts and result.status == ExecutionStatus.FAILED.value:
                fixed_code, fix_desc = self.auto_fixer.try_fix(
                    current_code, result.stderr + result.error, language
                )
                if fixed_code:
                    current_code = fixed_code
                    result.fix_attempted = True
                    result.fix_applied = fix_desc
                    with self._lock:
                        self._stats["auto_fixed"] += 1
                    continue

            # 超时和阻止不需要重试
            if result.status in (
                ExecutionStatus.TIMEOUT.value,
                ExecutionStatus.BLOCKED.value,
            ):
                break

        # 更新统计
        duration = (time.time() - start_time) * 1000
        if result:
            result.duration_ms = duration
        with self._lock:
            self._stats["total_executions"] += 1
            if result and result.success:
                self._stats["successful"] += 1
            else:
                self._stats["failed"] += 1

        return result or ExecutionResult(
            success=False,
            status=ExecutionStatus.ERROR.value,
            language=language,
            code=code,
            error="执行引擎内部错误",
            duration_ms=duration,
        )

    def _execute_single(
        self,
        code: str,
        language: str,
        work_dir: str,
        timeout: int,
        env: Optional[Dict[str, str]]
    ) -> ExecutionResult:
        """单次执行"""
        try:
            if language == "python":
                return self._exec_python(code, work_dir, timeout, env)
            elif language == "shell":
                return self._exec_shell(code, work_dir, timeout, env)
            elif language == "powershell":
                return self._exec_powershell(code, work_dir, timeout, env)
            elif language == "system":
                return self._exec_system(code, work_dir, timeout, env)
            else:
                return ExecutionResult(
                    success=False,
                    status=ExecutionStatus.ERROR.value,
                    language=language,
                    code=code,
                    error=f"不支持的语言: {language}",
                )
        except Exception as e:
            return ExecutionResult(
                success=False,
                status=ExecutionStatus.ERROR.value,
                language=language,
                code=code,
                error=f"执行引擎异常: {str(e)}",
                stderr=traceback.format_exc(),
            )

    def _exec_python(
        self, code: str, work_dir: str, timeout: int, env: Optional[Dict]
    ) -> ExecutionResult:
        """执行 Python 代码"""
        # 写入临时文件
        tmp_file = None
        try:
            with tempfile.NamedTemporaryFile(
                mode='w', suffix='.py', delete=False, encoding='utf-8'
            ) as f:
                # 添加 UTF-8 声明和安全包装
                f.write("# -*- coding: utf-8 -*-\n")
                f.write("# Auto-generated by MyAgent Executor\n")
                f.write(code)
                tmp_file = f.name

            exec_env = os.environ.copy()
            if env:
                exec_env.update(env)
            # 添加工作目录到 Python path
            if work_dir not in sys.path:
                sys.path.insert(0, work_dir)
            exec_env["PYTHONIOENCODING"] = "utf-8"

            proc = subprocess.run(
                [self.python_path, "-u", tmp_file],
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=work_dir,
                env=exec_env,
                encoding='utf-8',
                errors='replace',
            )

            stdout = self._truncate_output(proc.stdout)
            stderr = self._truncate_output(proc.stderr)

            return ExecutionResult(
                success=proc.returncode == 0,
                status=ExecutionStatus.SUCCESS.value if proc.returncode == 0 else ExecutionStatus.FAILED.value,
                language="python",
                code=code,
                stdout=stdout,
                stderr=stderr,
                exit_code=proc.returncode,
                error=stderr if proc.returncode != 0 else "",
                error_type=self._extract_error_type(stderr),
            )

        except subprocess.TimeoutExpired:
            return ExecutionResult(
                success=False,
                status=ExecutionStatus.TIMEOUT.value,
                language="python",
                code=code,
                error=f"执行超时 ({timeout}秒)",
            )
        finally:
            if tmp_file and os.path.exists(tmp_file):
                try:
                    os.unlink(tmp_file)
                except:
                    pass

    def _exec_shell(
        self, code: str, work_dir: str, timeout: int, env: Optional[Dict]
    ) -> ExecutionResult:
        """执行 Shell 命令 (bash/zsh)"""
        if self.system == "Windows":
            # Windows 上尝试使用 git bash 或 WSL
            git_bash = shutil.which("bash")
            if git_bash:
                cmd = [git_bash, "-c", code]
            else:
                # 回退到 PowerShell
                return self._exec_powershell(code, work_dir, timeout, env)
        else:
            cmd = self.shell_cmd + [code]

        exec_env = os.environ.copy()
        if env:
            exec_env.update(env)

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=work_dir,
                env=exec_env,
                encoding='utf-8',
                errors='replace',
            )

            stdout = self._truncate_output(proc.stdout)
            stderr = self._truncate_output(proc.stderr)

            return ExecutionResult(
                success=proc.returncode == 0,
                status=ExecutionStatus.SUCCESS.value if proc.returncode == 0 else ExecutionStatus.FAILED.value,
                language="shell",
                code=code,
                stdout=stdout,
                stderr=stderr,
                exit_code=proc.returncode,
                error=stderr if proc.returncode != 0 else "",
                error_type=self._extract_error_type(stderr),
            )

        except subprocess.TimeoutExpired:
            return ExecutionResult(
                success=False,
                status=ExecutionStatus.TIMEOUT.value,
                language="shell",
                code=code,
                error=f"执行超时 ({timeout}秒)",
            )

    def _exec_powershell(
        self, code: str, work_dir: str, timeout: int, env: Optional[Dict]
    ) -> ExecutionResult:
        """执行 PowerShell 命令"""
        cmd = ["powershell", "-NoProfile", "-NonInteractive", "-Command", code]

        exec_env = os.environ.copy()
        if env:
            exec_env.update(env)

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=work_dir,
                env=exec_env,
                encoding='utf-8',
                errors='replace',
            )

            stdout = self._truncate_output(proc.stdout)
            stderr = self._truncate_output(proc.stderr)

            return ExecutionResult(
                success=proc.returncode == 0,
                status=ExecutionStatus.SUCCESS.value if proc.returncode == 0 else ExecutionStatus.FAILED.value,
                language="powershell",
                code=code,
                stdout=stdout,
                stderr=stderr,
                exit_code=proc.returncode,
                error=stderr if proc.returncode != 0 else "",
                error_type=self._extract_error_type(stderr),
            )

        except subprocess.TimeoutExpired:
            return ExecutionResult(
                success=False,
                status=ExecutionStatus.TIMEOUT.value,
                language="powershell",
                code=code,
                error=f"执行超时 ({timeout}秒)",
            )

    def _exec_system(
        self, code: str, work_dir: str, timeout: int, env: Optional[Dict]
    ) -> ExecutionResult:
        """执行系统命令 (自动选择 shell)"""
        if self.system == "Windows":
            return self._exec_powershell(code, work_dir, timeout, env)
        else:
            return self._exec_shell(code, work_dir, timeout, env)

    # --------------------------------------------------------
    # 工具方法
    # --------------------------------------------------------

    def _detect_language(self, code: str) -> str:
        """自动检测代码语言"""
        code_stripped = code.strip()

        # Python 特征
        python_indicators = [
            'import ', 'from ', 'def ', 'class ', 'print(',
            'if __name__', 'self.', '#!/usr/bin/env python',
            'pip install', 'python -', '.py',
        ]
        for indicator in python_indicators:
            if indicator in code_stripped[:100]:
                return "python"

        # PowerShell 特征
        ps_indicators = [
            'Get-', 'Set-', 'New-', 'Remove-', 'Invoke-',
            '$', 'Write-Host', 'Read-Host',
        ]
        for indicator in ps_indicators:
            if indicator in code_stripped[:100]:
                return "powershell" if self.system == "Windows" else "shell"

        # 默认
        if self.system == "Windows":
            return "powershell"
        return "shell"

    def _truncate_output(self, output: str) -> str:
        """截断过长输出"""
        if len(output) <= self.max_output_length:
            return output
        return output[:self.max_output_length] + f"\n... (输出已截断，共 {len(output)} 字符)"

    def _extract_error_type(self, stderr: str) -> str:
        """从错误输出中提取错误类型"""
        # Python
        match = re.search(r'^(\w+Error):', stderr, re.MULTILINE)
        if match:
            return match.group(1)

        # Shell
        for etype in ["Permission denied", "command not found", "No such file",
                       "not recognized", "Access denied", "Syntax error"]:
            if etype.lower() in stderr.lower():
                return etype

        return "Unknown"

    def get_stats(self) -> Dict[str, Any]:
        """获取执行统计"""
        with self._lock:
            return dict(self._stats)


# ============================================================
# 全局执行器单例
# ============================================================

_global_executor: Optional[Executor] = None
_executor_lock = threading.Lock()


def get_executor() -> Executor:
    """获取全局执行器"""
    global _global_executor
    with _executor_lock:
        if _global_executor is None:
            _global_executor = Executor()
        return _global_executor


def execute_code(
    code: str,
    language: str = "auto",
    work_dir: Optional[str] = None,
    timeout: Optional[int] = None,
) -> ExecutionResult:
    """便捷执行函数"""
    return get_executor().execute(code, language, work_dir, timeout)
