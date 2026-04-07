"""
技能系统 - OpenClaw 风格 JSON 结构化技能调用
==============================================
支持: 文件操作、搜索、浏览器、系统操作等
使用 JSON Schema 定义技能参数
"""
import os
import json
import time
import platform
import subprocess
import tempfile
import logging
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from dataclasses import dataclass, field

logger = logging.getLogger("myagent.skills")

from config import get_config
from executor import execute_code, ExecutionResult

# ============================================================
# 技能注册系统
# ============================================================

@dataclass
class SkillParameter:
    """技能参数定义"""
    name: str
    type: str             # string / integer / number / boolean / array / object
    description: str
    required: bool = True
    default: Any = None
    enum: Optional[List] = None

    def to_schema(self) -> Dict:
        schema = {
            "type": self.type,
            "description": self.description,
        }
        if self.default is not None:
            schema["default"] = self.default
        if self.enum:
            schema["enum"] = self.enum
        return schema


@dataclass
class SkillDefinition:
    """技能定义"""
    name: str
    description: str
    parameters: List[SkillParameter] = field(default_factory=list)
    category: str = "general"
    requires_confirmation: bool = False  # 是否需要用户确认
    timeout: int = 60

    def to_tool_definition(self) -> Dict:
        """转换为 OpenAI function calling 格式"""
        properties = {}
        required = []

        for param in self.parameters:
            properties[param.name] = param.to_schema()
            if param.required:
                required.append(param.name)

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
            }
        }


class SkillRegistry:
    """技能注册中心"""

    def __init__(self):
        self._skills: Dict[str, SkillDefinition] = {}
        self._handlers: Dict[str, Callable] = {}

    def register(
        self,
        definition: SkillDefinition,
        handler: Callable
    ) -> None:
        """注册一个技能"""
        self._skills[definition.name] = definition
        self._handlers[definition.name] = handler
        logger.debug(f"注册技能: {definition.name}")

    def get(self, name: str) -> Optional[SkillDefinition]:
        return self._skills.get(name)

    def get_handler(self, name: str) -> Optional[Callable]:
        return self._handlers.get(name)

    def list_skills(self, category: Optional[str] = None) -> List[SkillDefinition]:
        skills = list(self._skills.values())
        if category:
            skills = [s for s in skills if s.category == category]
        return skills

    def get_all_tool_definitions(self) -> List[Dict]:
        """获取所有技能的工具定义 (用于 LLM function calling)"""
        return [s.to_tool_definition() for s in self._skills.values()]

    def call_skill(self, name: str, arguments: Dict) -> Any:
        """调用技能"""
        handler = self._handlers.get(name)
        if not handler:
            return {"error": f"技能 '{name}' 不存在"}
        try:
            return handler(arguments)
        except Exception as e:
            logger.error(f"技能调用失败 [{name}]: {e}")
            return {"error": f"技能调用异常: {str(e)}"}


# ============================================================
# 内置技能实现
# ============================================================

class BuiltinSkills:
    """内置技能集合"""

    def __init__(self, registry: SkillRegistry):
        self.registry = registry
        self._register_all()

    def _register_all(self):
        """注册所有内置技能"""
        self._register_file_ops()
        self._register_search()
        self._register_system_ops()
        self._register_code_execution()
        self._register_text_processing()
        self._register_network()

    # --------------------------------------------------------
    # 文件操作
    # --------------------------------------------------------
    def _register_file_ops(self):
        """文件操作技能"""

        def read_file(args: Dict) -> Dict:
            path = args.get("path", "")
            encoding = args.get("encoding", "utf-8")
            offset = args.get("offset", 0)
            limit = args.get("limit", 2000)

            try:
                p = Path(path)
                if not p.exists():
                    return {"error": f"文件不存在: {path}"}
                if not p.is_file():
                    return {"error": f"不是文件: {path}"}

                with open(p, 'r', encoding=encoding, errors='replace') as f:
                    lines = f.readlines()

                total_lines = len(lines)
                selected = lines[offset:offset + limit]

                return {
                    "content": "".join(selected),
                    "total_lines": total_lines,
                    "show_lines": len(selected),
                    "offset": offset,
                    "path": str(p.resolve()),
                }
            except Exception as e:
                return {"error": str(e)}

        def write_file(args: Dict) -> Dict:
            path = args.get("path", "")
            content = args.get("content", "")
            encoding = args.get("encoding", "utf-8")
            append = args.get("append", False)

            try:
                p = Path(path)
                p.parent.mkdir(parents=True, exist_ok=True)

                mode = 'a' if append else 'w'
                with open(p, mode, encoding=encoding) as f:
                    f.write(content)

                return {
                    "success": True,
                    "path": str(p.resolve()),
                    "size_bytes": len(content.encode(encoding)),
                    "mode": "append" if append else "write",
                }
            except Exception as e:
                return {"error": str(e)}

        def list_directory(args: Dict) -> Dict:
            path = args.get("path", ".")
            pattern = args.get("pattern", "*")
            recursive = args.get("recursive", False)

            try:
                p = Path(path)
                if not p.exists():
                    return {"error": f"目录不存在: {path}"}

                if recursive:
                    items = sorted(p.rglob(pattern))
                else:
                    items = sorted(p.glob(pattern))

                entries = []
                for item in items[:1000]:  # 限制返回数量
                    entry = {
                        "name": item.name,
                        "path": str(item),
                        "type": "directory" if item.is_dir() else "file",
                    }
                    if item.is_file():
                        try:
                            entry["size"] = item.stat().st_size
                        except:
                            pass
                    entries.append(entry)

                return {
                    "entries": entries,
                    "total": len(items),
                    "path": str(p.resolve()),
                }
            except Exception as e:
                return {"error": str(e)}

        def delete_file(args: Dict) -> Dict:
            path = args.get("path", "")
            try:
                p = Path(path)
                if p.is_file():
                    p.unlink()
                    return {"success": True, "deleted": str(p)}
                elif p.is_dir():
                    import shutil
                    shutil.rmtree(p)
                    return {"success": True, "deleted": str(p)}
                else:
                    return {"error": f"路径不存在: {path}"}
            except Exception as e:
                return {"error": str(e)}

        def move_file(args: Dict) -> Dict:
            src = args.get("source", "")
            dst = args.get("destination", "")
            try:
                s = Path(src)
                d = Path(dst)
                d.parent.mkdir(parents=True, exist_ok=True)
                s.rename(d)
                return {"success": True, "from": str(s), "to": str(d)}
            except Exception as e:
                return {"error": str(e)}

        def search_in_files(args: Dict) -> Dict:
            directory = args.get("directory", ".")
            pattern = args.get("pattern", "")
            file_pattern = args.get("file_pattern", "*")
            max_results = args.get("max_results", 50)

            try:
                results = []
                dir_path = Path(directory)
                for file_path in dir_path.rglob(file_pattern):
                    if len(results) >= max_results:
                        break
                    if not file_path.is_file():
                        continue
                    try:
                        content = file_path.read_text(encoding='utf-8', errors='replace')
                        lines = content.split('\n')
                        for i, line in enumerate(lines):
                            if pattern.lower() in line.lower():
                                results.append({
                                    "file": str(file_path),
                                    "line": i + 1,
                                    "content": line.strip()[:200],
                                })
                                if len(results) >= max_results:
                                    break
                    except:
                        continue

                return {"matches": results, "total": len(results)}
            except Exception as e:
                return {"error": str(e)}

        # 注册技能
        skills = [
            (SkillDefinition(
                name="read_file",
                description="读取文件内容，支持指定偏移和行数限制",
                parameters=[
                    SkillParameter("path", "string", "文件路径"),
                    SkillParameter("encoding", "string", "文件编码", required=False, default="utf-8"),
                    SkillParameter("offset", "integer", "起始行号", required=False, default=0),
                    SkillParameter("limit", "integer", "最大行数", required=False, default=2000),
                ],
                category="file",
            ), read_file),

            (SkillDefinition(
                name="write_file",
                description="写入文件内容，支持追加模式",
                parameters=[
                    SkillParameter("path", "string", "文件路径"),
                    SkillParameter("content", "string", "要写入的内容"),
                    SkillParameter("encoding", "string", "文件编码", required=False, default="utf-8"),
                    SkillParameter("append", "boolean", "追加模式", required=False, default=False),
                ],
                category="file",
            ), write_file),

            (SkillDefinition(
                name="list_directory",
                description="列出目录内容，支持通配符和递归",
                parameters=[
                    SkillParameter("path", "string", "目录路径", required=False, default="."),
                    SkillParameter("pattern", "string", "文件匹配模式", required=False, default="*"),
                    SkillParameter("recursive", "boolean", "递归列出", required=False, default=False),
                ],
                category="file",
            ), list_directory),

            (SkillDefinition(
                name="delete_file",
                description="删除文件或目录",
                parameters=[
                    SkillParameter("path", "string", "要删除的路径"),
                ],
                category="file",
                requires_confirmation=True,
            ), delete_file),

            (SkillDefinition(
                name="move_file",
                description="移动或重命名文件/目录",
                parameters=[
                    SkillParameter("source", "string", "源路径"),
                    SkillParameter("destination", "string", "目标路径"),
                ],
                category="file",
            ), move_file),

            (SkillDefinition(
                name="search_in_files",
                description="在目录中搜索文件内容",
                parameters=[
                    SkillParameter("directory", "string", "搜索目录", required=False, default="."),
                    SkillParameter("pattern", "string", "搜索关键词"),
                    SkillParameter("file_pattern", "string", "文件匹配模式", required=False, default="*"),
                    SkillParameter("max_results", "integer", "最大结果数", required=False, default=50),
                ],
                category="file",
            ), search_in_files),
        ]

        for defn, handler in skills:
            self.registry.register(defn, handler)

    # --------------------------------------------------------
    # 搜索技能
    # --------------------------------------------------------
    def _register_search(self):
        """搜索技能"""

        def web_search(args: Dict) -> Dict:
            query = args.get("query", "")
            num = args.get("num", 10)

            try:
                # 尝试使用 duckduckgo 搜索
                try:
                    from urllib.parse import quote_plus, urlencode
                    import urllib.request

                    # DuckDuckGo Lite (不需要 API key)
                    url = f"https://lite.duckduckgo.com/lite/?q={quote_plus(query)}"
                    req = urllib.request.Request(
                        url,
                        headers={'User-Agent': 'Mozilla/5.0 (compatible; MyAgent/1.0)'}
                    )
                    with urllib.request.urlopen(req, timeout=30) as resp:
                        html = resp.read().decode('utf-8', errors='replace')

                    # 简单解析 HTML 结果
                    results = self._parse_ddg_lite(html, num)
                    if results:
                        return {"results": results}

                except Exception as e:
                    logger.warning(f"DuckDuckGo 搜索失败: {e}")

                # 回退: 使用系统命令
                result = execute_code(
                    f'curl -s "https://api.duckduckgo.com/?q={query}&format=json&no_html=1" 2>/dev/null || echo "search_failed"',
                    language="shell",
                    timeout=15
                )
                if result.success and result.stdout:
                    try:
                        data = json.loads(result.stdout)
                        abstract = data.get("Abstract", "")
                        related = data.get("RelatedTopics", [])[:num]
                        results = []
                        if abstract:
                            results.append({
                                "title": data.get("Heading", query),
                                "snippet": abstract[:300],
                                "url": data.get("AbstractURL", ""),
                            })
                        for topic in related:
                            if isinstance(topic, dict) and "Text" in topic:
                                results.append({
                                    "title": topic.get("FirstURL", "").split("/")[-1] or query,
                                    "snippet": topic["Text"][:300],
                                    "url": topic.get("FirstURL", ""),
                                })
                            if len(results) >= num:
                                break
                        return {"results": results}
                    except:
                        pass

                return {"results": [], "note": "搜索无结果，可尝试使用其他搜索方式"}

            except Exception as e:
                return {"error": str(e)}

        def _parse_ddg_lite(self, html: str, num: int) -> List[Dict]:
            """解析 DuckDuckGo Lite HTML 结果"""
            import re
            results = []
            # 简单提取链接和文本
            links = re.findall(r'<a[^>]+class="result-link"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', html, re.DOTALL)
            snippets = re.findall(r'<td[^>]+class="result-snippet"[^>]*>(.*?)</td>', html, re.DOTALL)

            for i, (link, title) in enumerate(links[:num]):
                clean_title = re.sub(r'<[^>]+>', '', title).strip()
                snippet = snippets[i] if i < len(snippets) else ""
                clean_snippet = re.sub(r'<[^>]+>', '', snippet).strip()
                results.append({
                    "title": clean_title,
                    "url": link,
                    "snippet": clean_snippet[:300],
                })
            return results

        def url_fetch(args: Dict) -> Dict:
            url = args.get("url", "")
            method = args.get("method", "GET")
            headers = args.get("headers", {})

            try:
                from urllib.request import Request, urlopen
                req = Request(url, method=method)
                req.add_header('User-Agent', 'Mozilla/5.0 (compatible; MyAgent/1.0)')
                for k, v in headers.items():
                    req.add_header(k, v)

                with urlopen(req, timeout=30) as resp:
                    body = resp.read().decode('utf-8', errors='replace')
                    return {
                        "status": resp.status,
                        "content": body[:50000],
                        "headers": dict(resp.headers),
                        "url": resp.url,
                    }
            except Exception as e:
                return {"error": str(e)}

        self.registry.register(
            SkillDefinition(
                name="web_search",
                description="搜索互联网，返回搜索结果列表",
                parameters=[
                    SkillParameter("query", "string", "搜索关键词"),
                    SkillParameter("num", "integer", "结果数量", required=False, default=10),
                ],
                category="search",
                timeout=30,
            ),
            web_search
        )

        self.registry.register(
            SkillDefinition(
                name="url_fetch",
                description="获取网页内容",
                parameters=[
                    SkillParameter("url", "string", "网页URL"),
                    SkillParameter("method", "string", "HTTP方法", required=False, default="GET"),
                ],
                category="search",
                timeout=30,
            ),
            url_fetch
        )

    # --------------------------------------------------------
    # 系统操作
    # --------------------------------------------------------
    def _register_system_ops(self):
        """系统操作技能"""

        def get_system_info(args: Dict) -> Dict:
            return {
                "platform": platform.platform(),
                "system": platform.system(),
                "release": platform.release(),
                "version": platform.version(),
                "machine": platform.machine(),
                "processor": platform.processor(),
                "hostname": platform.node(),
                "python_version": platform.python_version(),
                "home": str(Path.home()),
                "cwd": os.getcwd(),
            }

        def get_env(args: Dict) -> Dict:
            name = args.get("name", "")
            if name:
                return {"name": name, "value": os.environ.get(name, "")}
            return {k: v for k, v in list(os.environ.items())[:100]}

        def set_env(args: Dict) -> Dict:
            name = args.get("name", "")
            value = args.get("value", "")
            os.environ[name] = value
            return {"success": True, "name": name}

        def process_list(args: Dict) -> Dict:
            try:
                if platform.system() == "Windows":
                    result = execute_code("Get-Process | Select-Object Id,ProcessName,CPU,WorkingSet -First 50 | ConvertTo-Json", "powershell")
                else:
                    result = execute_code("ps aux --sort=-%mem | head -50", "shell")
                return {"output": result.stdout}
            except Exception as e:
                return {"error": str(e)}

        def disk_usage(args: Dict) -> Dict:
            path = args.get("path", "/")
            try:
                usage = os.statvfs(path) if platform.system() != "Windows" else None
                if usage:
                    total = usage.f_blocks * usage.f_frsize
                    free = usage.f_bavail * usage.f_frsize
                    return {
                        "path": path,
                        "total_bytes": total,
                        "free_bytes": free,
                        "used_percent": round((1 - free / total) * 100, 1),
                    }
                else:
                    # Windows
                    result = execute_code(
                        f"Get-PSDrive -Name {path[0]} | ConvertTo-Json",
                        "powershell"
                    )
                    return {"output": result.stdout}
            except Exception as e:
                return {"error": str(e)}

        self.registry.register(
            SkillDefinition(
                name="get_system_info",
                description="获取系统信息",
                parameters=[],
                category="system",
            ),
            get_system_info
        )

        self.registry.register(
            SkillDefinition(
                name="get_env",
                description="获取环境变量",
                parameters=[
                    SkillParameter("name", "string", "变量名(空=全部)", required=False, default=""),
                ],
                category="system",
            ),
            get_env
        )

        self.registry.register(
            SkillDefinition(
                name="set_env",
                description="设置环境变量",
                parameters=[
                    SkillParameter("name", "string", "变量名"),
                    SkillParameter("value", "string", "变量值"),
                ],
                category="system",
            ),
            set_env
        )

        self.registry.register(
            SkillDefinition(
                name="process_list",
                description="列出系统进程",
                parameters=[],
                category="system",
                timeout=15,
            ),
            process_list
        )

        self.registry.register(
            SkillDefinition(
                name="disk_usage",
                description="获取磁盘使用情况",
                parameters=[
                    SkillParameter("path", "string", "路径", required=False, default="/"),
                ],
                category="system",
            ),
            disk_usage
        )

    # --------------------------------------------------------
    # 代码执行
    # --------------------------------------------------------
    def _register_code_execution(self):
        """代码执行技能"""

        def run_code(args: Dict) -> Dict:
            code = args.get("code", "")
            language = args.get("language", "auto")
            work_dir = args.get("work_dir", "")
            timeout = args.get("timeout", 300)

            result = execute_code(
                code=code,
                language=language,
                work_dir=work_dir or None,
                timeout=timeout,
            )
            return result.to_llm_dict()

        def run_python(args: Dict) -> Dict:
            return run_code({
                "code": args.get("code", ""),
                "language": "python",
                "work_dir": args.get("work_dir", ""),
                "timeout": args.get("timeout", 300),
            })

        def run_shell(args: Dict) -> Dict:
            return run_code({
                "code": args.get("code", ""),
                "language": "shell",
                "work_dir": args.get("work_dir", ""),
                "timeout": args.get("timeout", 300),
            })

        def run_powershell(args: Dict) -> Dict:
            return run_code({
                "code": args.get("code", ""),
                "language": "powershell",
                "work_dir": args.get("work_dir", ""),
                "timeout": args.get("timeout", 300),
            })

        self.registry.register(
            SkillDefinition(
                name="run_code",
                description="执行代码 (自动检测语言: Python/Shell/PowerShell)",
                parameters=[
                    SkillParameter("code", "string", "要执行的代码"),
                    SkillParameter("language", "string", "编程语言", required=False, default="auto",
                                   enum=["auto", "python", "shell", "powershell", "system"]),
                    SkillParameter("work_dir", "string", "工作目录", required=False, default=""),
                    SkillParameter("timeout", "integer", "超时秒数", required=False, default=300),
                ],
                category="execution",
                timeout=310,
            ),
            run_code
        )

        self.registry.register(
            SkillDefinition(
                name="run_python",
                description="执行 Python 代码",
                parameters=[
                    SkillParameter("code", "string", "Python代码"),
                    SkillParameter("work_dir", "string", "工作目录", required=False, default=""),
                    SkillParameter("timeout", "integer", "超时秒数", required=False, default=300),
                ],
                category="execution",
                timeout=310,
            ),
            run_python
        )

        self.registry.register(
            SkillDefinition(
                name="run_shell",
                description="执行 Shell/Bash 命令",
                parameters=[
                    SkillParameter("code", "string", "Shell命令"),
                    SkillParameter("work_dir", "string", "工作目录", required=False, default=""),
                    SkillParameter("timeout", "integer", "超时秒数", required=False, default=300),
                ],
                category="execution",
                timeout=310,
            ),
            run_shell
        )

        self.registry.register(
            SkillDefinition(
                name="run_powershell",
                description="执行 PowerShell 命令",
                parameters=[
                    SkillParameter("code", "string", "PowerShell命令"),
                    SkillParameter("work_dir", "string", "工作目录", required=False, default=""),
                    SkillParameter("timeout", "integer", "超时秒数", required=False, default=300),
                ],
                category="execution",
                timeout=310,
            ),
            run_powershell
        )

    # --------------------------------------------------------
    # 文本处理
    # --------------------------------------------------------
    def _register_text_processing(self):
        """文本处理技能"""

        def text_analyze(args: Dict) -> Dict:
            text = args.get("text", "")
            return {
                "length": len(text),
                "characters": len(text),
                "words": len(text.split()),
                "lines": text.count('\n') + 1,
                "bytes": len(text.encode('utf-8')),
            }

        def text_transform(args: Dict) -> Dict:
            text = args.get("text", "")
            operation = args.get("operation", "uppercase")
            ops = {
                "uppercase": lambda t: t.upper(),
                "lowercase": lambda t: t.lower(),
                "capitalize": lambda t: t.capitalize(),
                "strip": lambda t: t.strip(),
                "reverse": lambda t: t[::-1],
                "trim_lines": lambda t: '\n'.join(l.strip() for l in t.split('\n')),
                "remove_empty_lines": lambda t: '\n'.join(l for l in t.split('\n') if l.strip()),
                "count_lines": lambda t: str(t.count('\n') + 1),
                "word_count": lambda t: str(len(t.split())),
                "base64_encode": lambda t: __import__('base64').b64encode(t.encode()).decode(),
                "base64_decode": lambda t: __import__('base64').b64decode(t.encode()).decode(),
            }
            fn = ops.get(operation)
            if fn:
                return {"result": fn(text)}
            return {"error": f"未知操作: {operation}"}

        self.registry.register(
            SkillDefinition(
                name="text_analyze",
                description="分析文本统计信息",
                parameters=[
                    SkillParameter("text", "string", "要分析的文本"),
                ],
                category="text",
            ),
            text_analyze
        )

        self.registry.register(
            SkillDefinition(
                name="text_transform",
                description="文本转换操作",
                parameters=[
                    SkillParameter("text", "string", "输入文本"),
                    SkillParameter("operation", "string", "操作类型",
                                   enum=["uppercase", "lowercase", "capitalize", "strip",
                                         "reverse", "trim_lines", "remove_empty_lines",
                                         "count_lines", "word_count", "base64_encode", "base64_decode"]),
                ],
                category="text",
            ),
            text_transform
        )

    # --------------------------------------------------------
    # 网络
    # --------------------------------------------------------
    def _register_network(self):
        """网络技能"""

        def http_request(args: Dict) -> Dict:
            url = args.get("url", "")
            method = args.get("method", "GET").upper()
            headers = args.get("headers", {})
            body = args.get("body", "")

            try:
                from urllib.request import Request, urlopen
                data = body.encode('utf-8') if body else None
                req = Request(url, data=data, method=method)
                req.add_header('User-Agent', 'MyAgent/1.0')
                for k, v in headers.items():
                    req.add_header(k, v)

                with urlopen(req, timeout=30) as resp:
                    resp_body = resp.read().decode('utf-8', errors='replace')
                    return {
                        "status": resp.status,
                        "body": resp_body[:50000],
                        "url": resp.url,
                    }
            except Exception as e:
                return {"error": str(e)}

        def check_connectivity(args: Dict) -> Dict:
            host = args.get("host", "8.8.8.8")
            port = args.get("port", 53)
            try:
                import socket
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(5)
                result = sock.connect_ex((host, port))
                sock.close()
                return {
                    "reachable": result == 0,
                    "host": host,
                    "port": port,
                }
            except Exception as e:
                return {"error": str(e)}

        self.registry.register(
            SkillDefinition(
                name="http_request",
                description="发送 HTTP 请求",
                parameters=[
                    SkillParameter("url", "string", "请求URL"),
                    SkillParameter("method", "string", "HTTP方法", required=False, default="GET"),
                    SkillParameter("headers", "object", "请求头", required=False),
                    SkillParameter("body", "string", "请求体", required=False, default=""),
                ],
                category="network",
                timeout=35,
            ),
            http_request
        )

        self.registry.register(
            SkillDefinition(
                name="check_connectivity",
                description="检查网络连通性",
                parameters=[
                    SkillParameter("host", "string", "目标主机", required=False, default="8.8.8.8"),
                    SkillParameter("port", "integer", "目标端口", required=False, default=53),
                ],
                category="network",
                timeout=10,
            ),
            check_connectivity
        )


# ============================================================
# 全局技能注册中心
# ============================================================

_global_registry: Optional[SkillRegistry] = None


def get_skill_registry() -> SkillRegistry:
    """获取全局技能注册中心"""
    global _global_registry
    if _global_registry is None:
        _global_registry = SkillRegistry()
        BuiltinSkills(_global_registry)
    return _global_registry


def call_skill(name: str, arguments: Dict) -> Any:
    """便捷技能调用函数"""
    return get_skill_registry().call_skill(name, arguments)
