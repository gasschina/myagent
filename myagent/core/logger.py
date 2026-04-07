"""
core/logger.py - 日志管理模块
==============================
提供统一的日志记录功能，支持:
  - 文件 + 控制台双输出
  - 按时间/大小自动轮转
  - ANSI 彩色控制台输出
  - JSON 结构化日志输出（可选）
  - 动态日志级别调整
"""
from __future__ import annotations

import os
import sys
import json
import logging
from logging.handlers import RotatingFileHandler, TimedRotatingFileHandler
from pathlib import Path
from typing import Optional, Dict, Any
from datetime import datetime


# ANSI 颜色码
COLORS = {
    "DEBUG": "\033[36m",     # 青色
    "INFO": "\033[32m",      # 绿色
    "WARNING": "\033[33m",   # 黄色
    "ERROR": "\033[31m",     # 红色
    "CRITICAL": "\033[35m",  # 紫色
    "RESET": "\033[0m",
}


class ColorFormatter(logging.Formatter):
    """带颜色的控制台日志格式化器"""

    def format(self, record):
        color = COLORS.get(record.levelname, COLORS["RESET"])
        record.levelname = f"{color}{record.levelname:8s}{COLORS['RESET']}"
        return super().format(record)


class FileFormatter(logging.Formatter):
    """文件日志格式化器(无颜色)"""

    def format(self, record):
        # 去除 ANSI 颜色码
        record.msg = str(record.msg)
        for code in COLORS.values():
            record.msg = record.msg.replace(code, "")
        return super().format(record)


class JsonFormatter(logging.Formatter):
    """
    JSON 结构化日志格式化器。

    输出格式:
    {"timestamp": "...", "level": "INFO", "logger": "...", "message": "...", "extra": {}}
    """

    def format(self, record):
        log_entry: Dict[str, Any] = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
        }

        # 附加异常信息
        if record.exc_info and record.exc_info[0] is not None:
            log_entry["exception"] = {
                "type": record.exc_info[0].__name__,
                "message": str(record.exc_info[1]),
            }

        # 附加自定义字段
        if hasattr(record, "extra_fields"):
            log_entry.update(record.extra_fields)

        return json.dumps(log_entry, ensure_ascii=False, default=str)


class LevelFilter(logging.Filter):
    """只允许指定级别以上的日志通过"""

    def __init__(self, min_level: str = "DEBUG"):
        super().__init__()
        self.min_level = getattr(logging, min_level.upper(), logging.DEBUG)

    def filter(self, record):
        return record.levelno >= self.min_level


# ==============================================================================
# 全局日志器注册表（支持动态级别调整）
# ==============================================================================

_loggers: Dict[str, logging.Logger] = {}
_log_configs: Dict[str, Dict[str, Any]] = {}


def setup_logger(
    name: str = "myagent",
    log_dir: Optional[str] = None,
    level: str = "INFO",
    console: bool = True,
    json_format: bool = False,
    rotation: str = "size",
    max_bytes: int = 10 * 1024 * 1024,  # 10MB
    backup_count: int = 5,
    rotation_when: str = "midnight",
) -> logging.Logger:
    """
    初始化日志系统。

    Args:
        name: 日志器名称
        log_dir: 日志文件目录，默认为 ~/.myagent/logs/
        level: 日志级别 (DEBUG/INFO/WARNING/ERROR/CRITICAL)
        console: 是否输出到控制台
        json_format: 是否使用 JSON 结构化格式输出到文件
        rotation: 轮转策略 "size"(按大小) | "time"(按时间) | "none"(不轮转)
        max_bytes: 按大小轮转时，单个文件最大字节数
        backup_count: 保留的备份文件数量
        rotation_when: 按时间轮转的时间点 ("midnight"/"H"/"D"/"W0" 等)

    Returns:
        配置好的 Logger 实例
    """
    logger = logging.getLogger(name)

    # 避免重复添加 handler
    if logger.handlers:
        return logger

    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    # 防止日志向上层传播（避免重复输出）
    logger.propagate = False

    # 日志格式
    fmt = "[%(asctime)s] %(levelname)s %(name)s - %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"

    # 控制台输出
    if console:
        ch = logging.StreamHandler(sys.stdout)
        ch.setFormatter(ColorFormatter(fmt, datefmt=datefmt))
        ch.setLevel(logging.DEBUG)  # 控制台始终显示所有级别
        logger.addHandler(ch)

    # 文件输出
    if log_dir:
        log_path = Path(log_dir)
        log_path.mkdir(parents=True, exist_ok=True)

        if rotation == "size":
            # 按大小轮转
            log_file = log_path / f"{name}.log"
            fh = RotatingFileHandler(
                log_file,
                maxBytes=max_bytes,
                backupCount=backup_count,
                encoding="utf-8",
            )
            fh.setFormatter(
                JsonFormatter() if json_format
                else FileFormatter(fmt, datefmt=datefmt)
            )
            logger.addHandler(fh)

        elif rotation == "time":
            # 按时间轮转
            log_file = log_path / f"{name}.log"
            fh = TimedRotatingFileHandler(
                log_file,
                when=rotation_when,
                backupCount=backup_count,
                encoding="utf-8",
            )
            fh.setFormatter(
                JsonFormatter() if json_format
                else FileFormatter(fmt, datefmt=datefmt)
            )
            # 日志文件名后缀
            fh.suffix = "%Y%m%d"
            logger.addHandler(fh)

        else:
            # 不轮转，按日期命名
            log_file = log_path / f"{name}_{datetime.now().strftime('%Y%m%d')}.log"
            fh = logging.FileHandler(log_file, encoding="utf-8")
            fh.setFormatter(
                JsonFormatter() if json_format
                else FileFormatter(fmt, datefmt=datefmt)
            )
            logger.addHandler(fh)

    # 保存配置
    _loggers[name] = logger
    _log_configs[name] = {
        "level": level,
        "log_dir": log_dir,
        "console": console,
        "json_format": json_format,
        "rotation": rotation,
    }

    return logger


def get_logger(name: str = "myagent") -> logging.Logger:
    """获取已存在的 Logger，如果不存在则创建默认的"""
    logger = logging.getLogger(name)
    if not logger.handlers:
        return setup_logger(name)
    return logger


def set_level(name: str = "myagent", level: str = "INFO"):
    """
    动态调整日志级别。

    Args:
        name: 日志器名称，"all" 表示调整所有已注册的日志器
        level: 新的日志级别
    """
    if name == "all":
        for logger_name, logger in _loggers.items():
            logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    else:
        logger = logging.getLogger(name)
        logger.setLevel(getattr(logging, level.upper(), logging.INFO))


def get_log_config(name: str = "myagent") -> Dict[str, Any]:
    """获取日志配置"""
    return _log_configs.get(name, {})
