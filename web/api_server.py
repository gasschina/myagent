"""
web/api_server.py - 管理后台 HTTP API
======================================
为管理 UI 提供 RESTful API。
"""
from __future__ import annotations
import asyncio, json, os, time, shutil
from pathlib import Path
from typing import Optional
from aiohttp import web
from core.logger import get_logger
from core.llm import Message
from config import ModelEntry
import datetime

logger = get_logger("myagent.api")


def _agent_color(name: str) -> str:
    """Generate a consistent color for an agent based on its name."""
    colors = ['#4f46e5','#7c3aed','#ec4899','#ef4444','#f59e0b','#10b981',
              '#06b6d4','#3b82f6','#8b5cf6','#f97316','#14b8a6','#6366f1']
    h = sum(ord(c) for c in name)
    return colors[h % len(colors)]


class ApiServer:
    def __init__(self, app_core):
        self.core = app_core
        self.app = web.Application()
        self._setup_routes()
        self._runner: Optional[web.AppRunner] = None

    def _setup_routes(self):
        r = self.app.router
        r.add_get("/api/status", self.handle_status)
        r.add_post("/api/shutdown", self.handle_shutdown)
        r.add_get("/api/agents", self.handle_list_agents)
        r.add_get("/api/agents/tree", self.handle_agents_tree)
        r.add_post("/api/agents", self.handle_create_agent)
        r.add_get("/api/agents/{name:[a-zA-Z0-9_/-]+}", self.handle_get_agent)
        r.add_put("/api/agents/{name:[a-zA-Z0-9_/-]+}", self.handle_update_agent)
        r.add_delete("/api/agents/{name:[a-zA-Z0-9_/-]+}", self.handle_delete_agent)
        r.add_get("/api/agents/{name:[a-zA-Z0-9_/-]+}/soul", self.handle_get_soul)
        r.add_put("/api/agents/{name:[a-zA-Z0-9_/-]+}/soul", self.handle_set_soul)
        r.add_get("/api/agents/{name:[a-zA-Z0-9_/-]+}/identity", self.handle_get_identity)
        r.add_put("/api/agents/{name:[a-zA-Z0-9_/-]+}/identity", self.handle_set_identity)
        r.add_get("/api/agents/{name:[a-zA-Z0-9_/-]+}/user", self.handle_get_user)
        r.add_put("/api/agents/{name:[a-zA-Z0-9_/-]+}/user", self.handle_set_user)
        r.add_get("/api/agents/{name:[a-zA-Z0-9_/-]+}/sessions", self.handle_agent_sessions)
        r.add_get("/api/agents/{name:[a-zA-Z0-9_/-]+}/children", self.handle_list_children)
        r.add_post("/api/agents/{name:[a-zA-Z0-9_/-]+}/children", self.handle_create_child)
        r.add_get("/api/platforms", self.handle_list_platforms)
        r.add_put("/api/platforms/{name}", self.handle_update_platform)
        # ── 模型库 CRUD ──
        r.add_get("/api/models", self.handle_list_models)
        r.add_post("/api/models", self.handle_add_model)
        r.add_put("/api/models/{model_id}", self.handle_update_model)
        r.add_delete("/api/models/{model_id}", self.handle_delete_model)
        # ── Agent 绑定查询 ──
        r.add_get("/api/agents/{name:[a-zA-Z0-9_/-]+}/bindings", self.handle_agent_bindings)
        r.add_get("/api/sessions", self.handle_list_sessions)
        r.add_get("/api/sessions/{sid}/messages", self.handle_get_messages)
        r.add_delete("/api/sessions/{sid}", self.handle_clear_session)
        r.add_get("/api/memory/stats", self.handle_memory_stats)
        r.add_get("/api/memory/search", self.handle_memory_search)
        r.add_get("/api/memory/long-term", self.handle_list_long_term)
        r.add_delete("/api/memory/long-term/{mid}", self.handle_delete_long_term)
        r.add_post("/api/memory/cleanup", self.handle_memory_cleanup)
        r.add_get("/api/llm", self.handle_get_llm)
        r.add_put("/api/llm", self.handle_update_llm)
        r.add_post("/api/llm/test", self.handle_test_llm)
        r.add_get("/api/llm/usage", self.handle_llm_usage)
        r.add_get("/api/skills", self.handle_list_skills)
        r.add_get("/api/skills/{name}", self.handle_get_skill)
        r.add_get("/api/executor", self.handle_get_executor)
        r.add_put("/api/executor", self.handle_update_executor)
        r.add_get("/api/workdir", self.handle_get_workdir)
        r.add_put("/api/workdir", self.handle_set_workdir)
        r.add_get("/api/workdir/files", self.handle_list_workdir)
        r.add_get("/api/logs", self.handle_get_logs)
        r.add_get("/api/logs/stream", self.handle_log_stream)
        r.add_post("/api/chat", self.handle_chat)
        r.add_get("/chat", self.handle_chat_page)
        # ── 配置管理 (热重载/导入/导出) ──
        r.add_get("/api/config", self.handle_get_config)
        r.add_post("/api/config/reload", self.handle_reload_config)
        r.add_post("/api/config/export", self.handle_export_config)
        r.add_post("/api/config/import", self.handle_import_config)
        ui_dir = Path(__file__).parent / "ui"
        if ui_dir.exists():
            r.add_static("/ui", str(ui_dir))
            r.add_get("/", self.handle_index)

    async def handle_index(self, request):
        raise web.HTTPFound("/ui/chat.html")

    # --- Chat ---
    async def handle_chat(self, request):
        """POST /api/chat - 聊天消息处理"""
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON"}, status=400)

        message = data.get("message", "").strip()
        if not message:
            return web.json_response({"error": "message is required"}, status=400)

        agent_name = data.get("agent_name", "default") or "default"
        # 支持 path 格式 (如 "coder/python-expert")
        agent_path = data.get("agent_path", agent_name)
        raw_session_id = data.get("session_id", "") or "web_default"
        session_id = f"{agent_path}_{raw_session_id}"

        try:
            # 检查 Agent 是否指定了特定模型
            agent_cfg = self._read_agent_config(agent_path)
            if agent_cfg and (agent_cfg.get("model_id") or agent_cfg.get("model")):
                model_id = agent_cfg.get("model_id")
                model_cfg_override = None
                if model_id:
                    # 从模型库查找
                    for me in self.core.config.models_library:
                        if me.id == model_id:
                            model_cfg_override = {
                                "provider": me.provider or self.core.config.llm.provider,
                                "model": me.model or model_id,
                                "base_url": me.base_url or self.core.config.llm.base_url,
                                "api_key": me.api_key or self.core.config.llm.api_key,
                                "temperature": me.temperature,
                                "max_tokens": me.max_tokens,
                            }
                            break
                if not model_cfg_override and agent_cfg.get("model"):
                    # 兼容旧的 model 字段
                    model_cfg_override = {
                        "model": agent_cfg["model"],
                    }
                if model_cfg_override and self.core.llm:
                    # 临时切换模型参数用于本次请求
                    orig_provider = self.core.llm.provider
                    orig_model = self.core.llm.model
                    orig_base_url = self.core.llm.base_url
                    orig_api_key = self.core.llm.api_key
                    orig_temp = self.core.llm.temperature
                    orig_max_tokens = self.core.llm.max_tokens
                    try:
                        if "provider" in model_cfg_override:
                            self.core.llm.provider = model_cfg_override["provider"]
                        if "model" in model_cfg_override:
                            self.core.llm.model = model_cfg_override["model"]
                        if "base_url" in model_cfg_override:
                            self.core.llm.base_url = model_cfg_override["base_url"]
                        if "api_key" in model_cfg_override:
                            self.core.llm.api_key = model_cfg_override["api_key"]
                        if "temperature" in model_cfg_override:
                            self.core.llm.temperature = model_cfg_override["temperature"]
                        if "max_tokens" in model_cfg_override:
                            self.core.llm.max_tokens = model_cfg_override["max_tokens"]
                        response = await self.core.process_message(message, session_id)
                    finally:
                        # 恢复原始模型配置
                        self.core.llm.provider = orig_provider
                        self.core.llm.model = orig_model
                        self.core.llm.base_url = orig_base_url
                        self.core.llm.api_key = orig_api_key
                        self.core.llm.temperature = orig_temp
                        self.core.llm.max_tokens = orig_max_tokens
                else:
                    response = await self.core.process_message(message, session_id)
            else:
                response = await self.core.process_message(message, session_id)

            # 保存到记忆
            if self.core.memory:
                self.core.memory.add_short_term(
                    session_id=session_id, role="user", content=message,
                )
                self.core.memory.add_short_term(
                    session_id=session_id, role="assistant", content=response,
                )

            return web.json_response({"response": response, "session_id": session_id, "agent_name": agent_path, "agent_path": agent_path})
        except Exception as e:
            logger.error(f"Chat error: {e}", exc_info=True)
            return web.json_response({"error": str(e)}, status=500)

    async def handle_chat_page(self, request):
        """GET /chat - 重定向到聊天页面"""
        raise web.HTTPFound("/ui/chat.html")

    # --- System ---
    async def handle_status(self, request):
        c = self.core
        return web.json_response({
            "running": c._running, "provider": c.config.llm.provider,
            "model": c.config.llm.model, "session": c._session_id,
            "memory": c.memory.get_stats() if c.memory else {},
            "queue": c.task_queue.get_stats() if c.task_queue else {},
            "skills": len(c.skill_registry.list_skills()) if c.skill_registry else 0,
        })

    async def handle_shutdown(self, request):
        self.core._running = False
        asyncio.create_task(self.core.shutdown())
        return web.json_response({"ok": True})

    # --- Agents (层级体系) ---
    # 目录结构: agents/default/{config.json, soul.md, ...}
    #           agents/coder/{config.json, soul.md, ...}
    #           agents/coder/python-expert/{config.json, soul.md, ...}
    # agent path = 相对于 agents/ 的路径, 如 "default", "coder", "coder/python-expert"

    def _agents_dir(self):
        d = self.core.config_mgr.data_dir / "agents"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _agent_dir(self, path: str) -> Path:
        """根据 agent path 返回目录 (path 如 'coder/python-expert')"""
        return self._agents_dir() / path

    def _ensure_default_agent(self):
        """确保默认 agent 存在"""
        ad = self._agent_dir("default")
        if not (ad / "config.json").exists():
            ad.mkdir(parents=True, exist_ok=True)
            cfg = {
                "name": "default",
                "description": "默认助手 - 本机运行模式",
                "avatar_color": _agent_color("default"),
                "avatar_emoji": "🤖",
                "execution_mode": "local",
                "enabled": True,
                "system_prompt": "你是 MyAgent 默认助手，运行在本机模式。请用友好、专业的方式回答用户的问题。",
            }
            (ad / "config.json").write_text(json.dumps(cfg, indent=2, ensure_ascii=False))
            for fn, default in [
                ("soul.md", "# Default Agent\n\n## 性格\n专业、友好的AI助手\n"),
                ("identity.md", "# Default Agent\n\n## 身份\nMyAgent 默认AI助手\n"),
                ("user.md", "# 用户信息\n\n## 用户偏好\n<!-- 在此处记录用户偏好 -->\n"),
            ]:
                if not (ad / fn).exists():
                    (ad / fn).write_text(default)
            logger.info("已创建默认 Agent")

    def _read_agent_config(self, path: str) -> dict | None:
        """读取 agent 配置"""
        cfg_file = self._agent_dir(path) / "config.json"
        if not cfg_file.exists():
            return None
        return json.loads(cfg_file.read_text())

    def _write_agent_config(self, path: str, cfg: dict):
        """写入 agent 配置"""
        ad = self._agent_dir(path)
        ad.mkdir(parents=True, exist_ok=True)
        (ad / "config.json").write_text(json.dumps(cfg, indent=2, ensure_ascii=False))

    def _scan_agents_flat(self, base_dir: Path = None, prefix: str = "") -> list[dict]:
        """递归扫描所有 agent，返回扁平列表"""
        if base_dir is None:
            base_dir = self._agents_dir()
        agents = []
        if not base_dir.exists():
            return agents
        for d in sorted(base_dir.iterdir()):
            if not d.is_dir():
                continue
            cfg_file = d / "config.json"
            if not cfg_file.exists():
                continue
            agent_path = f"{prefix}{d.name}" if not prefix else f"{prefix}/{d.name}"
            cfg = json.loads(cfg_file.read_text())
            agent = {"path": agent_path, "name": d.name, **cfg}
            agent["avatar_color"] = cfg.get("avatar_color") or _agent_color(d.name)
            agent["depth"] = agent_path.count("/")
            agents.append(agent)
            # 递归子目录
            agents.extend(self._scan_agents_flat(d, agent_path))
        return agents

    def _build_agent_tree(self, agents_flat: list[dict]) -> list[dict]:
        """将扁平 agent 列表构建为树结构"""
        # 按 path 索引
        by_path = {a["path"]: {**a, "children": []} for a in agents_flat}
        roots = []
        for a in agents_flat:
            path = a["path"]
            parent_path = "/".join(path.split("/")[:-1]) if "/" in path else None
            node = by_path[path]
            if parent_path and parent_path in by_path:
                by_path[parent_path]["children"].append(node)
            else:
                roots.append(node)
        return roots

    async def handle_list_agents(self, request):
        """GET /api/agents - 返回扁平 agent 列表"""
        self._ensure_default_agent()
        agents = self._scan_agents_flat()
        # 统计会话数
        if self.core.memory:
            rows = self.core.memory._get_conn().execute(
                "SELECT session_id, COUNT(*) as cnt FROM memories "
                "WHERE category='short_term' GROUP BY session_id").fetchall()
            session_counts = {}
            for r in rows:
                sid = r["session_id"]
                for ap in [sid.split("_")[0], sid]:
                    session_counts[ap] = session_counts.get(ap, 0) + r["cnt"]
            for a in agents:
                a["session_count"] = session_counts.get(a["path"], 0)
        return web.json_response(agents)

    async def handle_agents_tree(self, request):
        """GET /api/agents/tree - 返回树形结构"""
        self._ensure_default_agent()
        agents_flat = self._scan_agents_flat()
        # 统计会话数
        if self.core.memory:
            rows = self.core.memory._get_conn().execute(
                "SELECT session_id, COUNT(*) as cnt FROM memories "
                "WHERE category='short_term' GROUP BY session_id").fetchall()
            session_counts = {}
            for r in rows:
                sid = r["session_id"]
                for ap in [sid.split("_")[0], sid]:
                    session_counts[ap] = session_counts.get(ap, 0) + r["cnt"]
            for a in agents_flat:
                a["session_count"] = session_counts.get(a["path"], 0)
        tree = self._build_agent_tree(agents_flat)
        return web.json_response(tree)

    async def handle_create_agent(self, request):
        """POST /api/agents - 创建顶级 agent"""
        self._ensure_default_agent()
        data = await request.json()
        name = data.get("name", "").strip()
        if not name:
            name = f"agent_{int(time.time())}"
        # 安全校验
        if "/" in name or "\\" in name or name == "default":
            return web.json_response({"error": "invalid name (no slashes, cannot be 'default')"}, status=400)

        ad = self._agent_dir(name)
        if (ad / "config.json").exists():
            return web.json_response({"error": f"Agent '{name}' already exists"}, status=409)

        cfg = {
            "name": name,
            "description": data.get("description", ""),
            "avatar_color": data.get("avatar_color") or _agent_color(name),
            "avatar_emoji": data.get("avatar_emoji", ""),
            "execution_mode": data.get("execution_mode", "sandbox"),
            "enabled": True,
            "system_prompt": data.get("system_prompt") or data.get("soul") or f"你是{name}，一个专业的AI助手。",
        }
        if "model" in data:
            cfg["model"] = data["model"]
        # 平台绑定和模型库引用
        if data.get("platform"):
            cfg["platform"] = data["platform"]
        if data.get("platform_token"):
            cfg["platform_token"] = data["platform_token"]
        if data.get("platform_app_id"):
            cfg["platform_app_id"] = data["platform_app_id"]
        if data.get("platform_app_secret"):
            cfg["platform_app_secret"] = data["platform_app_secret"]
        if data.get("model_id"):
            cfg["model_id"] = data["model_id"]

        ad.mkdir(parents=True, exist_ok=True)
        (ad / "config.json").write_text(json.dumps(cfg, indent=2, ensure_ascii=False))
        for fn, default in [
            ("soul.md", f"# {name}\n\n## 性格\n专业AI助手\n"),
            ("identity.md", f"# {name}\n\n## 身份\nAI助手\n"),
            ("user.md", f"# {name} 用户信息\n\n## 用户偏好\n<!-- 在此处记录用户偏好 -->\n"),
        ]:
            if not (ad / fn).exists():
                (ad / fn).write_text(default)

        logger.info(f"创建 Agent: {name} (sandbox模式)")
        return web.json_response({"ok": True, "path": name, "name": name, "avatar_color": cfg["avatar_color"]})

    async def handle_create_child(self, request):
        """POST /api/agents/{parent}/children - 创建子 agent"""
        parent_path = request.match_info["name"]
        self._ensure_default_agent()

        parent_cfg = self._read_agent_config(parent_path)
        if not parent_cfg:
            return web.json_response({"error": f"Parent agent '{parent_path}' not found"}, status=404)

        data = await request.json()
        name = data.get("name", "").strip()
        if not name:
            name = f"agent_{int(time.time())}"
        if "/" in name or "\\" in name:
            return web.json_response({"error": "invalid name (no slashes)"}, status=400)

        child_path = f"{parent_path}/{name}"
        ad = self._agent_dir(child_path)
        if (ad / "config.json").exists():
            return web.json_response({"error": f"Agent '{child_path}' already exists"}, status=409)

        cfg = {
            "name": name,
            "parent": parent_path,
            "description": data.get("description", ""),
            "avatar_color": data.get("avatar_color") or _agent_color(name),
            "avatar_emoji": data.get("avatar_emoji", ""),
            "execution_mode": data.get("execution_mode", "sandbox"),
            "enabled": True,
            "system_prompt": data.get("system_prompt") or data.get("soul") or f"你是{name}，{parent_path}的子Agent。请用专业的方式完成你的任务。",
        }
        if "model" in data:
            cfg["model"] = data["model"]
        # 平台绑定和模型库引用
        if data.get("platform"):
            cfg["platform"] = data["platform"]
        if data.get("platform_token"):
            cfg["platform_token"] = data["platform_token"]
        if data.get("platform_app_id"):
            cfg["platform_app_id"] = data["platform_app_id"]
        if data.get("platform_app_secret"):
            cfg["platform_app_secret"] = data["platform_app_secret"]
        if data.get("model_id"):
            cfg["model_id"] = data["model_id"]

        ad.mkdir(parents=True, exist_ok=True)
        (ad / "config.json").write_text(json.dumps(cfg, indent=2, ensure_ascii=False))
        for fn, default in [
            ("soul.md", f"# {name}\n\n## 上级: {parent_path}\n## 性格\n专业AI助手\n"),
            ("identity.md", f"# {name}\n\n## 身份\n{parent_path} 的子 Agent\n"),
            ("user.md", f"# {name} 用户信息\n\n## 用户偏好\n<!-- 在此处记录用户偏好 -->\n"),
        ]:
            if not (ad / fn).exists():
                (ad / fn).write_text(default)

        logger.info(f"创建子 Agent: {child_path} (sandbox模式)")
        return web.json_response({"ok": True, "path": child_path, "name": name, "parent": parent_path, "avatar_color": cfg["avatar_color"]})

    async def handle_list_children(self, request):
        """GET /api/agents/{parent}/children - 列出子 agent"""
        parent_path = request.match_info["name"]
        parent_dir = self._agent_dir(parent_path)
        if not (parent_dir / "config.json").exists():
            return web.json_response({"error": f"Agent '{parent_path}' not found"}, status=404)

        children = []
        if parent_dir.exists():
            for d in sorted(parent_dir.iterdir()):
                if d.is_dir() and (d / "config.json").exists():
                    cfg = json.loads((d / "config.json").read_text())
                    child_path = f"{parent_path}/{d.name}"
                    child = {"path": child_path, "name": d.name, "parent": parent_path, **cfg}
                    child["avatar_color"] = cfg.get("avatar_color") or _agent_color(d.name)
                    child["depth"] = child_path.count("/")
                    children.append(child)
        return web.json_response(children)

    async def handle_get_agent(self, request):
        """GET /api/agents/{path} - 获取 agent 详情"""
        path = request.match_info["name"]
        ad = self._agent_dir(path)
        if not (ad / "config.json").exists():
            return web.json_response({"error": "not found"}, status=404)
        cfg = json.loads((ad / "config.json").read_text())
        soul = (ad / "soul.md").read_text() if (ad / "soul.md").exists() else ""
        identity = (ad / "identity.md").read_text() if (ad / "identity.md").exists() else ""
        user = (ad / "user.md").read_text() if (ad / "user.md").exists() else ""
        # 列出子 agent
        children = []
        for d in sorted(ad.iterdir()):
            if d.is_dir() and (d / "config.json").exists():
                children.append(d.name)
        # 如果有 model_id，解析为完整模型信息
        model_info = None
        model_id = cfg.get("model_id", "")
        if model_id:
            for me in self.core.config.models_library:
                if me.id == model_id:
                    model_info = {"id": me.id, "name": me.name, "provider": me.provider,
                                 "model": me.model, "base_url": me.base_url, "enabled": me.enabled}
                    break
        result = {"path": path, **cfg, "soul": soul, "identity": identity, "user": user, "children": children}
        if model_info:
            result["model_info"] = model_info
        return web.json_response(result)

    async def handle_update_agent(self, request):
        """PUT /api/agents/{path} - 更新 agent 配置"""
        path = request.match_info["name"]
        data = await request.json()
        ad = self._agent_dir(path)
        if not (ad / "config.json").exists():
            return web.json_response({"error": "not found"}, status=404)
        cfg = json.loads((ad / "config.json").read_text())
        # 更新允许的字段
        for k in ("description", "avatar_color", "avatar_emoji", "model", "system_prompt",
                   "execution_mode", "enabled", "sandbox_image", "sandbox_network", "sandbox_memory",
                   "platform", "platform_token", "platform_app_id", "platform_app_secret", "model_id"):
            if k in data:
                cfg[k] = data[k]
        (ad / "config.json").write_text(json.dumps(cfg, indent=2, ensure_ascii=False))
        if "soul" in data: (ad / "soul.md").write_text(data["soul"])
        if "identity" in data: (ad / "identity.md").write_text(data["identity"])
        if "user" in data: (ad / "user.md").write_text(data["user"])
        logger.info(f"更新 Agent: {path}")
        return web.json_response({"ok": True})

    async def handle_delete_agent(self, request):
        """DELETE /api/agents/{path} - 删除 agent 及其所有子 agent"""
        path = request.match_info["name"]
        if path == "default":
            return web.json_response({"error": "cannot delete default agent"}, status=403)
        ad = self._agent_dir(path)
        if not (ad / "config.json").exists():
            return web.json_response({"error": "not found"}, status=404)
        if ad.exists():
            shutil.rmtree(ad)
        logger.info(f"删除 Agent: {path}")
        return web.json_response({"ok": True})

    async def handle_get_soul(self, request):
        path = request.match_info["name"]
        p = self._agent_dir(path) / "soul.md"
        if not p.parent.exists() or not (p.parent / "config.json").exists():
            return web.json_response({"error": "not found"}, status=404)
        return web.json_response({"soul": p.read_text() if p.exists() else ""})

    async def handle_set_soul(self, request):
        data = await request.json(); ad = self._agent_dir(request.match_info["name"])
        ad.mkdir(parents=True, exist_ok=True)
        (ad / "soul.md").write_text(data.get("soul", ""))
        return web.json_response({"ok": True})

    async def handle_get_identity(self, request):
        path = request.match_info["name"]
        p = self._agent_dir(path) / "identity.md"
        if not p.parent.exists() or not (p.parent / "config.json").exists():
            return web.json_response({"error": "not found"}, status=404)
        return web.json_response({"identity": p.read_text() if p.exists() else ""})

    async def handle_set_identity(self, request):
        data = await request.json(); ad = self._agent_dir(request.match_info["name"])
        ad.mkdir(parents=True, exist_ok=True)
        (ad / "identity.md").write_text(data.get("identity", ""))
        return web.json_response({"ok": True})

    async def handle_get_user(self, request):
        path = request.match_info["name"]
        p = self._agent_dir(path) / "user.md"
        if not p.parent.exists() or not (p.parent / "config.json").exists():
            return web.json_response({"error": "not found"}, status=404)
        return web.json_response({"user": p.read_text() if p.exists() else ""})

    async def handle_set_user(self, request):
        data = await request.json(); ad = self._agent_dir(request.match_info["name"])
        ad.mkdir(parents=True, exist_ok=True)
        (ad / "user.md").write_text(data.get("user", ""))
        return web.json_response({"ok": True})

    # --- Executor ---
    async def handle_get_executor(self, request):
        info = self.core.executor.get_execution_info() if self.core.executor else {}
        cfg = self.core.config.executor if self.core.config else None
        return web.json_response({
            **info,
            "timeout": cfg.timeout if cfg else 300,
            "auto_fix": cfg.auto_fix if cfg else True,
            "max_output_length": cfg.max_output_length if cfg else 50000,
        })

    async def handle_update_executor(self, request):
        data = await request.json()
        cfg_path = self.core.config_mgr._config_file
        cfg_data = json.loads(cfg_path.read_text()) if cfg_path.exists() else {}
        exe = cfg_data.setdefault("executor", {})
        for k in ("execution_mode", "timeout", "auto_fix", "max_output_length",
                   "sandbox_image", "sandbox_network", "sandbox_memory"):
            if k in data:
                exe[k] = data[k]
        cfg_path.write_text(json.dumps(cfg_data, indent=2, ensure_ascii=False))
        # 热更新内存配置
        update_keys = {k: v for k, v in data.items() if hasattr(self.core.config_mgr.config.executor, k)}
        self.core.config_mgr.update_executor(**update_keys)
        # 实时切换执行引擎
        if self.core.executor and "execution_mode" in data:
            mode = data["execution_mode"]
            ok = self.core.executor.set_execution_mode(mode)
            if "sandbox_image" in data:
                self.core.executor.sandbox_image = data["sandbox_image"]
            if "sandbox_network" in data:
                self.core.executor.sandbox_network = data["sandbox_network"]
            if "sandbox_memory" in data:
                self.core.executor.sandbox_memory = data["sandbox_memory"]
            if not ok:
                return web.json_response({"ok": False, "error": f"切换到 {mode} 失败(Docker 不可用)"})
        logger.info(f"执行引擎配置已热更新: mode={data.get('execution_mode')}")
        return web.json_response({"ok": True, "hot_reload": True})

    # --- Agent Bindings ---
    async def handle_agent_bindings(self, request):
        """GET /api/agents/{name}/bindings - 获取 Agent 的聊天平台绑定"""
        path = request.match_info["name"]
        cfg = self._read_agent_config(path)
        if not cfg:
            return web.json_response({"error": "not found"}, status=404)
        bindings = {}
        if cfg.get("platform"):
            bindings["platform"] = cfg["platform"]
        if cfg.get("platform_token"):
            bindings["platform_token"] = cfg["platform_token"]
        if cfg.get("platform_app_id"):
            bindings["platform_app_id"] = cfg["platform_app_id"]
        if cfg.get("platform_app_secret"):
            bindings["platform_app_secret"] = cfg["platform_app_secret"]
        # 模型绑定
        if cfg.get("model_id"):
            model_info = None
            for me in self.core.config.models_library:
                if me.id == cfg["model_id"]:
                    model_info = {"id": me.id, "name": me.name, "provider": me.provider,
                                 "model": me.model, "base_url": me.base_url, "enabled": me.enabled}
                    break
            if model_info:
                bindings["model"] = model_info
            else:
                bindings["model_id"] = cfg["model_id"]
                bindings["model"] = None
        elif cfg.get("model"):
            bindings["model"] = {"model": cfg["model"]}
        return web.json_response(bindings)

    # --- Platforms ---
    async def handle_list_platforms(self, request):
        platforms = []
        for pname in ["telegram", "discord", "feishu", "qq", "wechat"]:
            pcfg = getattr(self.core.config, pname, None)
            platforms.append({"name": pname, "enabled": bool(pcfg and (pcfg.token or pcfg.app_id))})
        return web.json_response(platforms)

    async def handle_update_platform(self, request):
        name = request.match_info["name"]; data = await request.json()
        cfg_path = self.core.config_mgr._config_file
        cfg_data = json.loads(cfg_path.read_text()) if cfg_path.exists() else {}
        cfg_data.setdefault("platforms", {})[name] = data
        cfg_path.write_text(json.dumps(cfg_data, indent=2, ensure_ascii=False))
        return web.json_response({"ok": True})

    # --- Sessions ---
    async def handle_list_sessions(self, request):
        if not self.core.memory: return web.json_response([])
        agent = request.query.get("agent", "")
        if agent:
            prefix = f"{agent}_"
            rows = self.core.memory._get_conn().execute(
                "SELECT DISTINCT session_id, COUNT(*) as cnt, MAX(created_at) as last FROM memories "
                "WHERE category='short_term' AND session_id LIKE ? GROUP BY session_id ORDER BY last DESC LIMIT 100",
                (prefix + "%",)).fetchall()
        else:
            rows = self.core.memory._get_conn().execute(
                "SELECT DISTINCT session_id, COUNT(*) as cnt, MAX(created_at) as last FROM memories "
                "WHERE category='short_term' GROUP BY session_id ORDER BY last DESC LIMIT 100").fetchall()
        return web.json_response([{"id": r["session_id"], "messages": r["cnt"], "last": r["last"]} for r in rows])

    async def handle_agent_sessions(self, request):
        """GET /api/agents/{name}/sessions - Convenience endpoint for agent-scoped sessions."""
        name = request.match_info["name"]
        if not self.core.memory:
            return web.json_response({"agent": name, "sessions": []})
        prefix = f"{name}_"
        rows = self.core.memory._get_conn().execute(
            "SELECT DISTINCT session_id, COUNT(*) as cnt, MAX(created_at) as last FROM memories "
            "WHERE category='short_term' AND session_id LIKE ? GROUP BY session_id ORDER BY last DESC LIMIT 100",
            (prefix + "%",)).fetchall()
        sessions = [{"id": r["session_id"], "messages": r["cnt"], "last": r["last"]} for r in rows]
        # Agent info
        ad = self._agent_dir(name)
        agent_info = {"name": name, "avatar_color": _agent_color(name)}
        if (ad / "config.json").exists():
            agent_info.update(json.loads((ad / "config.json").read_text()))
        return web.json_response({**agent_info, "sessions": sessions})

    async def handle_get_messages(self, request):
        sid = request.match_info["sid"]
        if not self.core.memory: return web.json_response([])
        entries = self.core.memory.get_conversation(sid, limit=100)
        return web.json_response([{"role": e.role, "content": e.content[:500], "time": e.created_at} for e in entries])

    async def handle_clear_session(self, request):
        if self.core.memory: self.core.memory.clear_conversation(request.match_info["sid"])
        return web.json_response({"ok": True})

    # --- Memory ---
    async def handle_memory_stats(self, request):
        return web.json_response(self.core.memory.get_stats() if self.core.memory else {})

    async def handle_memory_search(self, request):
        q = request.query.get("q", ""); cat = request.query.get("category", "")
        if not self.core.memory: return web.json_response([])
        results = self.core.memory.search(q, category=cat, limit=20)
        return web.json_response([{"id": e.id, "key": e.key, "content": e.content[:300],
            "category": e.category, "importance": e.importance} for e in results])

    async def handle_list_long_term(self, request):
        if not self.core.memory: return web.json_response([])
        entries = self.core.memory.get_long_term(limit=50)
        return web.json_response([{"id": e.id, "key": e.key, "content": e.content[:300],
            "summary": e.summary, "importance": e.importance} for e in entries])

    async def handle_delete_long_term(self, request):
        if self.core.memory:
            self.core.memory._get_conn().execute("DELETE FROM memories WHERE id=?", (request.match_info["mid"],))
            self.core.memory._get_conn().commit()
        return web.json_response({"ok": True})

    async def handle_memory_cleanup(self, request):
        return web.json_response({"cleaned": self.core.memory.cleanup_expired() if self.core.memory else 0})

    # --- LLM ---
    async def handle_get_llm(self, request):
        c = self.core.config.llm
        return web.json_response({"provider": c.provider, "model": c.model, "base_url": c.base_url,
            "temperature": c.temperature, "max_tokens": c.max_tokens, "timeout": c.timeout,
            "max_retries": c.max_retries, "api_key_set": bool(c.api_key)})

    async def handle_update_llm(self, request):
        data = await request.json()
        # 1. 写入文件
        cfg_path = self.core.config_mgr._config_file
        cfg_data = json.loads(cfg_path.read_text()) if cfg_path.exists() else {}
        llm = cfg_data.setdefault("llm", {})
        for k in ("provider", "model", "base_url", "temperature", "max_tokens", "timeout", "max_retries"):
            if k in data: llm[k] = data[k]
        if data.get("api_key"): llm["api_key"] = data["api_key"]
        cfg_path.write_text(json.dumps(cfg_data, indent=2, ensure_ascii=False))
        # 2. 热更新内存中的配置
        update_keys = {k: v for k, v in data.items() if k != "api_key" or data.get("api_key")}
        self.core.config_mgr.update_llm(**update_keys)
        # 3. 实时更新 LLM 客户端
        if self.core.llm:
            new_cfg = self.core.config_mgr.config.llm
            self.core.llm.provider = new_cfg.provider
            self.core.llm.model = new_cfg.model
            self.core.llm.base_url = new_cfg.base_url
            self.core.llm.temperature = new_cfg.temperature
            self.core.llm.max_tokens = new_cfg.max_tokens
            self.core.llm.timeout = new_cfg.timeout
            self.core.llm.max_retries = new_cfg.max_retries
            if data.get("api_key"):
                self.core.llm.api_key = data["api_key"]
        logger.info(f"LLM 配置已热更新: provider={data.get('provider')}, model={data.get('model')}")
        return web.json_response({"ok": True, "hot_reload": True})

    async def handle_test_llm(self, request):
        try:
            msg = await self.core.llm.chat([Message(role="user", content="Hi, reply OK")])
            return web.json_response({"ok": True, "response": msg.content[:100] if msg.content else ""})
        except Exception as e:
            return web.json_response({"ok": False, "error": str(e)})

    async def handle_llm_usage(self, request):
        return web.json_response(self.core.llm.get_usage_stats() if self.core.llm else {})

    # --- Models Library ---
    async def handle_list_models(self, request):
        """GET /api/models - 列出模型库中所有模型"""
        models = []
        for m in self.core.config.models_library:
            models.append({
                "id": m.id, "name": m.name, "provider": m.provider,
                "model": m.model, "base_url": m.base_url,
                "max_tokens": m.max_tokens, "temperature": m.temperature,
                "enabled": m.enabled,
                "has_api_key": bool(m.api_key),
            })
        return web.json_response(models)

    async def handle_add_model(self, request):
        """POST /api/models - 添加模型到库"""
        data = await request.json()
        model_id = data.get("id", "").strip()
        if not model_id:
            return web.json_response({"error": "模型 ID 不能为空"}, status=400)
        # 检查重复
        for m in self.core.config.models_library:
            if m.id == model_id:
                return web.json_response({"error": f"模型 '{model_id}' 已存在"}, status=409)
        entry = ModelEntry(
            id=model_id,
            name=data.get("name", model_id),
            provider=data.get("provider", "openai"),
            model=data.get("model", model_id),
            base_url=data.get("base_url", ""),
            api_key=data.get("api_key", ""),
            max_tokens=data.get("max_tokens", 4096),
            temperature=data.get("temperature", 0.1),
            enabled=data.get("enabled", True),
        )
        self.core.config.models_library.append(entry)
        self.core.config_mgr.save()
        logger.info(f"添加模型到库: {model_id}")
        return web.json_response({"ok": True, "id": model_id})

    async def handle_update_model(self, request):
        """PUT /api/models/{model_id} - 更新模型配置"""
        model_id = request.match_info["model_id"]
        data = await request.json()
        found = False
        for m in self.core.config.models_library:
            if m.id == model_id:
                for k in ("name", "provider", "model", "base_url", "max_tokens", "temperature", "enabled"):
                    if k in data:
                        setattr(m, k, data[k])
                if data.get("api_key"):
                    m.api_key = data["api_key"]
                found = True
                break
        if not found:
            return web.json_response({"error": f"模型 '{model_id}' 未找到"}, status=404)
        self.core.config_mgr.save()
        logger.info(f"更新模型库: {model_id}")
        return web.json_response({"ok": True})

    async def handle_delete_model(self, request):
        """DELETE /api/models/{model_id} - 从库中删除模型"""
        model_id = request.match_info["model_id"]
        original_len = len(self.core.config.models_library)
        self.core.config.models_library = [
            m for m in self.core.config.models_library if m.id != model_id
        ]
        if len(self.core.config.models_library) == original_len:
            return web.json_response({"error": f"模型 '{model_id}' 未找到"}, status=404)
        self.core.config_mgr.save()
        logger.info(f"从模型库删除: {model_id}")
        return web.json_response({"ok": True})

    # --- Skills ---
    async def handle_list_skills(self, request):
        return web.json_response(self.core.skill_registry.list_skills_info() if self.core.skill_registry else [])

    async def handle_get_skill(self, request):
        s = self.core.skill_registry.get(request.match_info["name"]) if self.core.skill_registry else None
        if not s: return web.json_response({"error": "not found"}, status=404)
        return web.json_response(s.to_openclaw_format())

    # --- Workdir ---
    async def handle_get_workdir(self, request):
        return web.json_response({"path": str(self.core.config_mgr.data_dir / "workspace")})

    async def handle_set_workdir(self, request):
        data = await request.json(); path = data.get("path", "")
        if path: Path(path).mkdir(parents=True, exist_ok=True)
        cfg_path = self.core.config_mgr._config_file
        cfg_data = json.loads(cfg_path.read_text()) if cfg_path.exists() else {}
        cfg_data["workspace"] = path
        cfg_path.write_text(json.dumps(cfg_data, indent=2, ensure_ascii=False))
        return web.json_response({"ok": True})

    async def handle_list_workdir(self, request):
        wd = self.core.config_mgr.data_dir / "workspace"
        if not wd.exists(): return web.json_response([])
        items = []
        for f in sorted(wd.iterdir())[:200]:
            try: items.append({"name": f.name, "type": "dir" if f.is_dir() else "file", "size": f.stat().st_size if f.is_file() else 0})
            except: pass
        return web.json_response(items)

    # --- Logs ---
    async def handle_get_logs(self, request):
        log_dir = self.core.config_mgr.logs_dir
        lines = int(request.query.get("lines", "200"))
        level = request.query.get("level", "").upper()
        logs = []
        for lf in sorted(log_dir.glob("myagent*.log"), reverse=True):
            try:
                text = lf.read_text(encoding="utf-8", errors="ignore")
                for line in text.strip().split("\n")[-lines:]:
                    if level and level not in line: continue
                    logs.append(line)
                if len(logs) >= lines: break
            except: pass
        return web.json_response(logs[-lines:])

    async def handle_log_stream(self, request):
        resp = web.StreamResponse()
        resp.content_type = "text/event-stream"
        resp.headers["Cache-Control"] = "no-cache"
        await resp.prepare(request)
        log_file = self.core.config_mgr.logs_dir / "myagent.log"
        last_pos = 0
        try:
            while True:
                try:
                    if log_file.exists():
                        size = log_file.stat().st_size
                        if size > last_pos:
                            with open(log_file, "r", encoding="utf-8", errors="ignore") as f:
                                f.seek(last_pos); new_data = f.read(); last_pos = size
                            if new_data: await resp.write(f"data: {json.dumps(new_data.strip())}\n\n")
                except: pass
                await asyncio.sleep(0.5)
        except asyncio.CancelledError: pass
        return resp

    # ── 配置管理 (热重载 / 导入 / 导出) ──
    async def handle_get_config(self, request):
        """GET /api/config - 获取完整配置（敏感字段脱敏）"""
        cfg = self.core.config_mgr.get_full_config()
        return web.json_response(cfg)

    async def handle_reload_config(self, request):
        """POST /api/config/reload - 从配置文件热重载（无需重启）"""
        try:
            old_provider = self.core.config_mgr.config.llm.provider
            old_model = self.core.config_mgr.config.llm.model

            # 重新加载配置文件
            new_config = self.core.config_mgr.reload()
            logger.info("配置已热重载")

            # 更新运行中的组件
            changes = []
            if self.core.llm:
                llm_cfg = new_config.llm
                self.core.llm.provider = llm_cfg.provider
                self.core.llm.model = llm_cfg.model
                self.core.llm.base_url = llm_cfg.base_url
                self.core.llm.api_key = llm_cfg.api_key
                self.core.llm.temperature = llm_cfg.temperature
                self.core.llm.max_tokens = llm_cfg.max_tokens
                self.core.llm.timeout = llm_cfg.timeout
                self.core.llm.max_retries = llm_cfg.max_retries
                self.core.llm.anthropic_api_key = llm_cfg.anthropic_api_key
                if llm_cfg.provider != old_provider or llm_cfg.model != old_model:
                    changes.append(f"LLM: {old_provider}/{old_model} -> {llm_cfg.provider}/{llm_cfg.model}")

            if self.core.executor:
                exe_cfg = new_config.executor
                self.core.executor.timeout = exe_cfg.timeout
                self.core.executor.max_retries = exe_cfg.max_retries
                self.core.executor.auto_fix = exe_cfg.auto_fix
                self.core.executor.max_output_length = exe_cfg.max_output_length
                if exe_cfg.sandbox_image:
                    self.core.executor.sandbox_image = exe_cfg.sandbox_image
                if exe_cfg.execution_mode:
                    self.core.executor.set_execution_mode(exe_cfg.execution_mode)

            # 更新 app 引用
            self.core.config = new_config

            logger.info(f"热重载完成，变更: {changes or '无显著变更'}")
            return web.json_response({
                "ok": True,
                "message": "配置已热重载",
                "changes": changes,
                "config": self.core.config_mgr.get_full_config(),
            })
        except Exception as e:
            logger.error(f"热重载失败: {e}", exc_info=True)
            return web.json_response({"ok": False, "error": str(e)}, status=500)

    async def handle_export_config(self, request):
        """POST /api/config/export - 导出配置为 JSON 文件下载"""
        try:
            data = await request.json()
        except Exception:
            data = {}
        include_secrets = data.get("include_secrets", False)

        try:
            export_data = self.core.config_mgr.export_config(include_secrets=include_secrets)
            filename = f"myagent_config_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.json"

            resp = web.Response(
                body=json.dumps(export_data, ensure_ascii=False, indent=2),
                content_type="application/json",
                headers={
                    "Content-Disposition": f"attachment; filename=\"{filename}\"",
                },
            )
            logger.info(f"配置已导出: {filename} (secrets={include_secrets})")
            return resp
        except Exception as e:
            logger.error(f"导出配置失败: {e}", exc_info=True)
            return web.json_response({"ok": False, "error": str(e)}, status=500)

    async def handle_import_config(self, request):
        """POST /api/config/import - 从上传的 JSON 导入配置"""
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "无效的 JSON 数据"}, status=400)

        overwrite = data.get("_overwrite", False) if isinstance(data, dict) else False

        try:
            result = self.core.config_mgr.import_config(data, overwrite=overwrite)
            if not result["ok"]:
                return web.json_response(result, status=400)

            # 热重载运行中的组件
            new_config = self.core.config_mgr.config
            if self.core.llm:
                llm_cfg = new_config.llm
                self.core.llm.provider = llm_cfg.provider
                self.core.llm.model = llm_cfg.model
                self.core.llm.base_url = llm_cfg.base_url
                self.core.llm.api_key = llm_cfg.api_key
                self.core.llm.temperature = llm_cfg.temperature
                self.core.llm.max_tokens = llm_cfg.max_tokens
                self.core.llm.timeout = llm_cfg.timeout
                self.core.llm.max_retries = llm_cfg.max_retries
            self.core.config = new_config

            logger.info(f"配置已导入: {result['message']}")
            return web.json_response(result)
        except Exception as e:
            logger.error(f"导入配置失败: {e}", exc_info=True)
            return web.json_response({"ok": False, "error": str(e)}, status=500)

    async def start(self, port: int = 8765):
        self._runner = web.AppRunner(self.app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", port)
        await site.start()
        logger.info(f"管理后台: http://127.0.0.1:{port}/ui/")

    async def stop(self):
        if self._runner: await self._runner.cleanup()
