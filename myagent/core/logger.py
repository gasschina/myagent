"""
core/logger.py - 日志管理模块
==============================
提供统一的日志记录功能，支持文件+控制台双输出。
"""
from __future__ import annotations

import os
import sys
import logging
from pathlib import Path
from typing import Optional
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


def setup_logger(
    name: str = "myagent",
    log_dir: Optional[str] = None,
    level: str = "INFO",
    console: bool = True,
) -> logging.Logger:
    """
    初始化日志系统。

    Args:
        name: 日志器名称
        log_dir: 日志文件目录，默认为 ~/.myagent/logs/
        level: 日志级别
        console: 是否输出到控制台

    Returns:
        配置好的 Logger 实例
    """
    logger = logging.getLogger(name)

    # 避免重复添加 handler
    if logger.handlers:
        return logger

    logger.setLevel(getattr(logging, level.upper(), logging.INFO))

    # 日志格式
    fmt = "[%(asctime)s] %(levelname)s %(name)s - %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"

    # 控制台输出
    if console:
        ch = logging.StreamHandler(sys.stdout)
        ch.setFormatter(ColorFormatter(fmt, datefmt=datefmt))
        logger.addHandler(ch)

    # 文件输出
    if log_dir:
        log_path = Path(log_dir)
        log_path.mkdir(parents=True, exist_ok=True)
        log_file = log_path / f"{name}_{datetime.now().strftime('%Y%m%d')}.log"
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setFormatter(FileFormatter(fmt, datefmt=datefmt))
        logger.addHandler(fh)

    return logger


def get_logger(name: str = "myagent") -> logging.Logger:
    """获取已存在的 Logger，如果不存在则创建默认的"""
    logger = logging.getLogger(name)
    if not logger.handlers:
        return setup_logger(name)
    return logger
