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

logger = get_logger("myagent.api")


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
        r.add_post("/api/agents", self.handle_create_agent)
        r.add_get("/api/agents/{name}", self.handle_get_agent)
        r.add_put("/api/agents/{name}", self.handle_update_agent)
        r.add_delete("/api/agents/{name}", self.handle_delete_agent)
        r.add_get("/api/agents/{name}/soul", self.handle_get_soul)
        r.add_put("/api/agents/{name}/soul", self.handle_set_soul)
        r.add_get("/api/agents/{name}/identity", self.handle_get_identity)
        r.add_put("/api/agents/{name}/identity", self.handle_set_identity)
        r.add_get("/api/agents/{name}/user", self.handle_get_user)
        r.add_put("/api/agents/{name}/user", self.handle_set_user)
        r.add_get("/api/platforms", self.handle_list_platforms)
        r.add_put("/api/platforms/{name}", self.handle_update_platform)
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
        ui_dir = Path(__file__).parent / "ui"
        if ui_dir.exists():
            r.add_static("/ui", str(ui_dir))
            r.add_get("/", self.handle_index)

    async def handle_index(self, request):
        raise web.HTTPFound("/ui/index.html")

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

    # --- Agents ---
    def _agents_dir(self):
        d = self.core.config_mgr.data_dir / "agents"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _agent_dir(self, name):
        return self._agents_dir() / name

    async def handle_list_agents(self, request):
        agents = []
        for d in sorted(self._agents_dir().iterdir()):
            if d.is_dir() and (d / "config.json").exists():
                cfg = json.loads((d / "config.json").read_text())
                agents.append({"name": d.name, **cfg})
        if not agents:
            agents.append({"name": "default", "model": self.core.config.llm.model})
        return web.json_response(agents)

    async def handle_create_agent(self, request):
        data = await request.json()
        name = data.get("name", f"agent_{int(time.time())}")
        ad = self._agent_dir(name); ad.mkdir(parents=True, exist_ok=True)
        cfg = {k: v for k, v in data.items() if k != "name"}
        (ad / "config.json").write_text(json.dumps(cfg, indent=2, ensure_ascii=False))
        for fn, default in [("soul.md", f"# {name}\n\n## 性格\n专业AI助手\n"), ("identity.md", f"# {name}\n\n## 身份\nAI助手\n")]:
            if not (ad / fn).exists():
                (ad / fn).write_text(default)
        return web.json_response({"ok": True, "name": name})

    async def handle_get_agent(self, request):
        name = request.match_info["name"]
        ad = self._agent_dir(name)
        if not (ad / "config.json").exists():
            return web.json_response({"error": "not found"}, status=404)
        cfg = json.loads((ad / "config.json").read_text())
        soul = (ad / "soul.md").read_text() if (ad / "soul.md").exists() else ""
        identity = (ad / "identity.md").read_text() if (ad / "identity.md").exists() else ""
        user = (ad / "user.md").read_text() if (ad / "user.md").exists() else ""
        return web.json_response({"name": name, **cfg, "soul": soul, "identity": identity, "user": user})

    async def handle_update_agent(self, request):
        name = request.match_info["name"]; data = await request.json()
        ad = self._agent_dir(name); ad.mkdir(parents=True, exist_ok=True)
        cfg = {k: v for k, v in data.items() if k not in ("soul", "identity")}
        (ad / "config.json").write_text(json.dumps(cfg, indent=2, ensure_ascii=False))
        if "soul" in data: (ad / "soul.md").write_text(data["soul"])
        if "identity" in data: (ad / "identity.md").write_text(data["identity"])
        return web.json_response({"ok": True})

    async def handle_delete_agent(self, request):
        name = request.match_info["name"]; ad = self._agent_dir(name)
        if ad.exists(): shutil.rmtree(ad)
        return web.json_response({"ok": True})

    async def handle_get_soul(self, request):
        p = self._agent_dir(request.match_info["name"]) / "soul.md"
        return web.json_response({"soul": p.read_text() if p.exists() else ""})

    async def handle_set_soul(self, request):
        data = await request.json(); ad = self._agent_dir(request.match_info["name"])
        ad.mkdir(parents=True, exist_ok=True)
        (ad / "soul.md").write_text(data.get("soul", ""))
        return web.json_response({"ok": True})

    async def handle_get_identity(self, request):
        p = self._agent_dir(request.match_info["name"]) / "identity.md"
        return web.json_response({"identity": p.read_text() if p.exists() else ""})

    async def handle_set_identity(self, request):
        data = await request.json(); ad = self._agent_dir(request.match_info["name"])
        ad.mkdir(parents=True, exist_ok=True)
        (ad / "identity.md").write_text(data.get("identity", ""))
        return web.json_response({"ok": True})

    async def handle_get_user(self, request):
        p = self._agent_dir(request.match_info["name"]) / "user.md"
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
        # 实时切换
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
        return web.json_response({"ok": True})

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
        rows = self.core.memory._get_conn().execute(
            "SELECT DISTINCT session_id, COUNT(*) as cnt, MAX(created_at) as last FROM memories "
            "WHERE category='short_term' GROUP BY session_id ORDER BY last DESC LIMIT 100").fetchall()
        return web.json_response([{"id": r["session_id"], "messages": r["cnt"], "last": r["last"]} for r in rows])

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
        cfg_path = self.core.config_mgr._config_file
        cfg_data = json.loads(cfg_path.read_text()) if cfg_path.exists() else {}
        llm = cfg_data.setdefault("llm", {})
        for k in ("provider", "model", "base_url", "temperature", "max_tokens", "timeout", "max_retries"):
            if k in data: llm[k] = data[k]
        if data.get("api_key"): llm["api_key"] = data["api_key"]
        cfg_path.write_text(json.dumps(cfg_data, indent=2, ensure_ascii=False))
        return web.json_response({"ok": True})

    async def handle_test_llm(self, request):
        try:
            msg = await self.core.llm.chat([{"role": "user", "content": "Hi, reply OK"}])
            return web.json_response({"ok": True, "response": msg.choices[0].message.content[:100]})
        except Exception as e:
            return web.json_response({"ok": False, "error": str(e)})

    async def handle_llm_usage(self, request):
        return web.json_response(self.core.llm.get_usage_stats() if self.core.llm else {})

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

    async def start(self, port: int = 8765):
        self._runner = web.AppRunner(self.app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", port)
        await site.start()
        logger.info(f"管理后台: http://127.0.0.1:{port}/ui/")

    async def stop(self):
        if self._runner: await self._runner.cleanup()
