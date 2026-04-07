"""
skills/file_skill.py - 文件操作技能
=====================================
提供文件读写、目录操作、文件搜索等功能。
"""
from __future__ import annotations

import os
import shutil
import glob as glob_module
from pathlib import Path
from typing import List, Optional

from core.logger import get_logger
from skills.base import Skill, SkillResult, SkillParameter

logger = get_logger("myagent.skills.file")


class FileReadSkill(Skill):
    """读取文件内容"""
    name = "file_read"
    description = "读取指定文件的内容，支持指定编码和行数限制"
    category = "file"
    parameters = [
        SkillParameter("path", "string", "文件路径(绝对路径或相对路径)", required=True),
        SkillParameter("encoding", "string", "文件编码", required=False, default="utf-8"),
        SkillParameter("offset", "integer", "起始行号(从0开始)", required=False, default=0),
        SkillParameter("limit", "integer", "读取行数限制", required=False, default=500),
    ]

    async def execute(self, path: str = "", encoding: str = "utf-8",
                      offset: int = 0, limit: int = 500, **kwargs) -> SkillResult:
        try:
            file_path = Path(path).expanduser().resolve()
            if not file_path.exists():
                return SkillResult(success=False, error=f"文件不存在: {path}")

            with open(file_path, "r", encoding=encoding, errors="replace") as f:
                lines = f.readlines()

            total_lines = len(lines)
            selected = lines[offset:offset + limit]
            content = "".join(selected)

            return SkillResult(
                success=True,
                data={
                    "path": str(file_path),
                    "content": content,
                    "total_lines": total_lines,
                    "showed_lines": len(selected),
                    "offset": offset,
                },
                message=f"已读取 {file_path.name} ({len(selected)}/{total_lines} 行)",
            )
        except Exception as e:
            return SkillResult(success=False, error=str(e))


class FileWriteSkill(Skill):
    """写入文件内容"""
    name = "file_write"
    description = "将内容写入指定文件，支持创建目录和追加模式"
    category = "file"
    parameters = [
        SkillParameter("path", "string", "文件路径", required=True),
        SkillParameter("content", "string", "要写入的内容", required=True),
        SkillParameter("encoding", "string", "文件编码", required=False, default="utf-8"),
        SkillParameter("append", "boolean", "是否追加模式", required=False, default=False),
    ]
    dangerous = True

    async def execute(self, path: str = "", content: str = "",
                      encoding: str = "utf-8", append: bool = False, **kwargs) -> SkillResult:
        try:
            file_path = Path(path).expanduser().resolve()
            file_path.parent.mkdir(parents=True, exist_ok=True)

            mode = "a" if append else "w"
            with open(file_path, mode, encoding=encoding) as f:
                f.write(content)

            return SkillResult(
                success=True,
                data={"path": str(file_path), "size": len(content)},
                message=f"已写入 {file_path.name} ({len(content)} 字符)",
                files=[str(file_path)],
            )
        except Exception as e:
            return SkillResult(success=False, error=str(e))


class FileListSkill(Skill):
    """列出目录内容"""
    name = "file_list"
    description = "列出指定目录下的文件和子目录"
    category = "file"
    parameters = [
        SkillParameter("path", "string", "目录路径", required=True),
        SkillParameter("pattern", "string", "文件匹配模式(如 *.py)", required=False, default="*"),
        SkillParameter("recursive", "boolean", "是否递归", required=False, default=False),
    ]

    async def execute(self, path: str = "", pattern: str = "*",
                      recursive: bool = False, **kwargs) -> SkillResult:
        try:
            dir_path = Path(path).expanduser().resolve()
            if not dir_path.exists():
                return SkillResult(success=False, error=f"目录不存在: {path}")
            if not dir_path.is_dir():
                return SkillResult(success=False, error=f"不是目录: {path}")

            if recursive:
                items = sorted(dir_path.rglob(pattern))
            else:
                items = sorted(dir_path.glob(pattern))

            result = []
            for item in items:
                try:
                    stat = item.stat()
                    result.append({
                        "name": item.name,
                        "path": str(item),
                        "is_dir": item.is_dir(),
                        "size": stat.st_size,
                    })
                except OSError:
                    result.append({
                        "name": item.name,
                        "path": str(item),
                        "is_dir": item.is_dir(),
                        "size": 0,
                    })

            return SkillResult(
                success=True,
                data={"path": str(dir_path), "items": result, "count": len(result)},
                message=f"共 {len(result)} 个项目",
            )
        except Exception as e:
            return SkillResult(success=False, error=str(e))


class FileDeleteSkill(Skill):
    """删除文件或目录"""
    name = "file_delete"
    description = "删除指定文件或目录"
    category = "file"
    parameters = [
        SkillParameter("path", "string", "文件/目录路径", required=True),
        SkillParameter("recursive", "boolean", "递归删除目录", required=False, default=False),
    ]
    dangerous = True

    async def execute(self, path: str = "", recursive: bool = False, **kwargs) -> SkillResult:
        try:
            target = Path(path).expanduser().resolve()
            if not target.exists():
                return SkillResult(success=False, error=f"不存在: {path}")

            if target.is_dir():
                if recursive:
                    shutil.rmtree(target)
                else:
                    shutil.rmdir(target)
            else:
                target.unlink()

            return SkillResult(
                success=True,
                message=f"已删除: {path}",
            )
        except Exception as e:
            return SkillResult(success=False, error=str(e))


class FileSearchSkill(Skill):
    """搜索文件内容"""
    name = "file_search"
    description = "在文件中搜索包含指定文本的行"
    category = "file"
    parameters = [
        SkillParameter("path", "string", "搜索目录路径", required=True),
        SkillParameter("query", "string", "搜索关键词或正则表达式", required=True),
        SkillParameter("pattern", "string", "文件匹配模式", required=False, default="*"),
        SkillParameter("max_results", "integer", "最大结果数", required=False, default=50),
    ]

    async def execute(self, path: str = "", query: str = "", pattern: str = "*",
                      max_results: int = 50, **kwargs) -> SkillResult:
        try:
            search_dir = Path(path).expanduser().resolve()
            if not search_dir.is_dir():
                return SkillResult(success=False, error=f"不是目录: {path}")

            results = []
            for file_path in search_dir.rglob(pattern):
                if len(results) >= max_results:
                    break
                if not file_path.is_file():
                    continue
                try:
                    with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                        for i, line in enumerate(f):
                            if query.lower() in line.lower():
                                results.append({
                                    "file": str(file_path),
                                    "line": i + 1,
                                    "content": line.strip(),
                                })
                                if len(results) >= max_results:
                                    break
                except (IOError, OSError):
                    continue

            return SkillResult(
                success=True,
                data={"query": query, "results": results, "count": len(results)},
                message=f"找到 {len(results)} 处匹配",
            )
        except Exception as e:
            return SkillResult(success=False, error=str(e))


class FileMoveSkill(Skill):
    """移动/重命名文件"""
    name = "file_move"
    description = "移动或重命名文件/目录"
    category = "file"
    parameters = [
        SkillParameter("source", "string", "源路径", required=True),
        SkillParameter("destination", "string", "目标路径", required=True),
    ]
    dangerous = True

    async def execute(self, source: str = "", destination: str = "", **kwargs) -> SkillResult:
        try:
            src = Path(source).expanduser().resolve()
            dst = Path(destination).expanduser().resolve()
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
            return SkillResult(success=True, message=f"已移动: {source} → {destination}")
        except Exception as e:
            return SkillResult(success=False, error=str(e))
