"""
main.py - MyAgent 主入口
==========================
本地桌面端执行型AI助手。
支持:
  - 系统托盘后台运行 (pystray)
  - 交互式命令行
  - 多聊天平台接入
  - Windows / macOS / Linux
"""
from __future__ import annotations

import asyncio
import os
import sys
import signal
import threading
from pathlib import Path

# 确保项目根目录在 Python 路径中
PROJECT_ROOT = Path(__file__).parent.resolve()
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config import get_config, ConfigManager
from core.logger import setup_logger, get_logger
from core.llm import LLMClient, Message
from core.task_queue import TaskQueue
from memory.manager import MemoryManager
from executor.engine import ExecutionEngine
from skills.registry import SkillRegistry
from agents.main_agent import MainAgent
from agents.tool_agent import ToolAgent
from agents.memory_agent import MemoryAgent
from chatbot.base import ChatMessage, ChatResponse
from chatbot.manager import ChatBotManager
from core.utils import timestamp, detect_platform


# ==============================================================================
# MyAgent 应用主类
# ==============================================================================

class MyAgentApp:
    """
    MyAgent 应用主类。

    管理:
      - LLM 客户端
      - 记忆系统
      - 执行引擎
      - 技能系统
      - Agent 集群
      - 聊天平台
      - 系统托盘
      - 任务队列
    """

    def __init__(self):
        self.config_mgr = get_config()
        self.config = self.config_mgr.config
        self.logger = None
        self._running = False

        # 核心组件
        self.memory: MemoryManager | None = None
        self.executor: ExecutionEngine | None = None
        self.skill_registry: SkillRegistry | None = None
        self.llm: LLMClient | None = None
        self.task_queue: TaskQueue | None = None

        # Agent
        self.main_agent: MainAgent | None = None
        self.tool_agent: ToolAgent | None = None
        self.memory_agent: MemoryAgent | None = None

        # 聊天平台
        self.chat_manager: ChatBotManager | None = None

        # 交互式会话
        self._session_id = "cli_default"

    async def initialize(self):
        """初始化所有组件"""
        # 1. 日志
        self.logger = setup_logger(
            "myagent",
            log_dir=str(self.config_mgr.logs_dir),
            level=self.config.log_level,
        )
        self.logger.info("=" * 60)
        self.logger.info("MyAgent 正在启动...")
        self.logger.info(f"平台: {detect_platform()}")
        self.logger.info(f"数据目录: {self.config_mgr.data_dir}")
        self.logger.info("=" * 60)

        # 2. LLM 客户端
        llm_cfg = self.config.llm
        self.llm = LLMClient(
            provider=llm_cfg.provider,
            api_key=llm_cfg.api_key,
            base_url=llm_cfg.base_url,
            model=llm_cfg.model,
            temperature=llm_cfg.temperature,
            max_tokens=llm_cfg.max_tokens,
            timeout=llm_cfg.timeout,
            max_retries=llm_cfg.max_retries,
            anthropic_api_key=llm_cfg.anthropic_api_key,
        )
        self.logger.info(f"LLM: {llm_cfg.provider}/{llm_cfg.model}")

        # 3. 记忆系统
        mem_cfg = self.config.memory
        self.memory = MemoryManager(db_path=mem_cfg.db_path)
        self.memory.initialize()
        mem_stats = self.memory.get_stats()
        self.logger.info(f"记忆系统: {mem_stats['total_count']} 条记录")

        # 4. 执行引擎
        exe_cfg = self.config.executor
        self.executor = ExecutionEngine(
            timeout=exe_cfg.timeout,
            max_retries=exe_cfg.max_retries,
            auto_fix=exe_cfg.auto_fix,
            max_output_length=exe_cfg.max_output_length,
        )
        self.logger.info(f"执行引擎: timeout={exe_cfg.timeout}s, auto_fix={exe_cfg.auto_fix}")

        # 5. 技能系统
        self.skill_registry = SkillRegistry()
        # 注册内置技能
        self._register_builtin_skills()
        skills = self.skill_registry.list_skills()
        self.logger.info(f"技能系统: {len(skills)} 个技能已注册 - {skills}")

        # 6. 任务队列
        self.task_queue = TaskQueue(max_workers=self.config.agent.max_parallel)
        await self.task_queue.start()
        self.logger.info(f"任务队列: workers={self.config.agent.max_parallel}")

        # 7. 创建 Agent
        self.memory_agent = MemoryAgent(
            llm=self.llm,
            memory_manager=self.memory,
            config=self.config,
        )
        self.tool_agent = ToolAgent(
            llm=self.llm,
            memory_manager=self.memory,
            executor=self.executor,
            skill_registry=self.skill_registry,
            task_queue=self.task_queue,
            config=self.config,
        )
        self.main_agent = MainAgent(
            llm=self.llm,
            memory_manager=self.memory,
            executor=self.executor,
            skill_registry=self.skill_registry,
            task_queue=self.task_queue,
            config=self.config,
            tool_agent=self.tool_agent,
            memory_agent=self.memory_agent,
        )
        self.logger.info("Agent 集群已初始化 (主/工具/记忆)")

        # 8. 聊天平台
        enabled_platforms = self.config_mgr.get_enabled_platforms()
        if enabled_platforms:
            self.chat_manager = ChatBotManager()
            self.chat_manager.setup_platforms(
                enabled_platforms,
                message_handler=self._handle_chat_message,
            )
            self.logger.info(f"聊天平台: {[p.platform for p in enabled_platforms]}")
        else:
            self.logger.info("聊天平台: 未配置(仅CLI模式)")

        self._running = True
        self.logger.info("✅ MyAgent 启动完成!")

    def _register_builtin_skills(self):
        """注册内置技能"""
        from skills.file_skill import (
            FileReadSkill, FileWriteSkill, FileListSkill,
            FileDeleteSkill, FileSearchSkill, FileMoveSkill,
        )
        from skills.search_skill import WebSearchSkill, WebReadSkill, URLReadSkill
        from skills.system_skill import (
            SystemInfoSkill, ProcessListSkill, CommandRunSkill,
            EnvironmentGetSkill, PathExpandSkill,
        )
        from skills.browser_skill import (
            BrowserOpenSkill, BrowserClickSkill, BrowserFillSkill,
        )

        # 文件技能
        for skill_cls in [
            FileReadSkill, FileWriteSkill, FileListSkill,
            FileDeleteSkill, FileSearchSkill, FileMoveSkill,
        ]:
            self.skill_registry.register(skill_cls())

        # 搜索技能
        for skill_cls in [WebSearchSkill, WebReadSkill, URLReadSkill]:
            self.skill_registry.register(skill_cls())

        # 系统技能
        for skill_cls in [
            SystemInfoSkill, ProcessListSkill, CommandRunSkill,
            EnvironmentGetSkill, PathExpandSkill,
        ]:
            self.skill_registry.register(skill_cls())

        # 浏览器技能
        for skill_cls in [BrowserOpenSkill, BrowserClickSkill, BrowserFillSkill]:
            self.skill_registry.register(skill_cls())

    async def process_message(
        self,
        user_message: str,
        session_id: str = "",
    ) -> str:
        """
        处理用户消息并返回回复。

        Args:
            user_message: 用户消息
            session_id: 会话 ID

        Returns:
            助手回复文本
        """
        if not self.main_agent:
            return "⚠️ MyAgent 尚未初始化"

        session_id = session_id or self._session_id

        from agents.base import AgentContext
        context = AgentContext(
            session_id=session_id,
            user_message=user_message,
        )

        try:
            result_context = await self.main_agent.process(context)
            response = result_context.working_memory.get(
                "final_response", "⚠️ 未能生成回复"
            )
            return response
        except Exception as e:
            self.logger.error(f"处理消息异常: {e}", exc_info=True)
            return f"❌ 处理失败: {str(e)}"

    async def _handle_chat_message(self, message: ChatMessage, bot):
        """
        处理来自聊天平台的消息。

        Args:
            message: 聊天消息
            bot: 发送消息的机器人实例
        """
        # 生成会话 ID
        session_id = bot._generate_session_id(message)

        # 处理特殊命令
        if message.text == "__cmd_clear__":
            if self.memory:
                self.memory.clear_conversation(session_id)
            await bot.send_message(ChatResponse(
                chat_id=message.chat_id,
                text="🗑️ 对话历史已清除。",
            ))
            return

        # 处理消息
        response_text = await self.process_message(message.text, session_id)

        # 发送回复
        await bot.send_message(ChatResponse(
            chat_id=message.chat_id,
            user_id=message.user_id,
            text=response_text,
        ))

    async def run_cli(self):
        """运行交互式命令行"""
        if not self.main_agent:
            await self.initialize()

        print()
        print("=" * 60)
        print("  🤖 MyAgent - 本地桌面端执行型AI助手")
        print(f"  LLM: {self.config.llm.provider}/{self.config.llm.model}")
        print(f"  技能: {len(self.skill_registry.list_skills())} 个已注册")
        print(f"  平台: {'CLI' + (' + ' + ','.join(self.chat_manager.get_active_platforms()) if self.chat_manager else '')}")
        print("  输入 'help' 查看命令 | 'quit' 退出")
        print("=" * 60)
        print()

        while self._running:
            try:
                # 读取用户输入
                user_input = input("👤 你: ").strip()

                if not user_input:
                    continue

                # 内置命令
                if user_input.lower() in ("quit", "exit", "q"):
                    print("👋 再见!")
                    break
                elif user_input.lower() == "help":
                    self._print_help()
                    continue
                elif user_input.lower() == "status":
                    self._print_status()
                    continue
                elif user_input.lower() == "skills":
                    self._print_skills()
                    continue
                elif user_input.lower() == "memory":
                    self._print_memory_stats()
                    continue
                elif user_input.lower() == "sessions":
                    self._print_sessions()
                    continue
                elif user_input.lower().startswith("session "):
                    new_session = user_input[8:].strip()
                    if new_session:
                        self._session_id = new_session
                        print(f"📝 已切换到会话: {self._session_id}")
                    continue
                elif user_input.lower() == "clear":
                    if self.memory:
                        self.memory.clear_conversation(self._session_id)
                    print("🗑️ 对话历史已清除")
                    continue

                # 处理消息
                print("⏳ 思考中...", end="", flush=True)
                response = await self.process_message(user_input, self._session_id)
                print(f"\r🤖 助手: {response}")
                print()

            except KeyboardInterrupt:
                print("\n\n👋 再见!")
                break
            except EOFError:
                break
            except Exception as e:
                print(f"\n❌ 错误: {e}")
                print()

    def _print_help(self):
        print("""
📋 可用命令:
  help          - 显示帮助
  status        - 查看系统状态
  skills        - 列出所有技能
  memory        - 查看记忆统计
  sessions      - 查看会话列表
  session <id>  - 切换会话
  clear         - 清除当前对话历史
  quit/exit     - 退出

💡 使用方式:
  直接输入自然语言描述你的需求。
  示例:
    - "帮我创建一个Python文件"
    - "搜索一下最新的AI新闻"
    - "查看系统信息"
    - "!system_info" (直接调用技能)
""")

    def _print_status(self):
        print(f"""
📊 系统状态:
  运行状态: {'✅ 运行中' if self._running else '❌ 已停止'}
  LLM: {self.config.llm.provider}/{self.config.llm.model}
  当前会话: {self._session_id}
  执行引擎: timeout={self.config.executor.timeout}s
  任务队列: {self.task_queue.get_stats() if self.task_queue else 'N/A'}
  记忆: {self.memory.get_stats() if self.memory else 'N/A'}
  聊天平台: {self.chat_manager.get_stats() if self.chat_manager else 'N/A'}
""")

    def _print_skills(self):
        print("\n🛠️ 已注册技能:")
        if self.skill_registry:
            for info in self.skill_registry.list_skills_info():
                danger = " ⚠️" if info.get("dangerous") else ""
                print(f"  • {info['name']}: {info['description']}{danger}")
        print()

    def _print_memory_stats(self):
        if self.memory:
            stats = self.memory.get_stats()
            print(f"\n🧠 记忆系统:")
            print(f"  短期记忆: {stats.get('short_term_count', 0)} 条")
            print(f"  工作记忆: {stats.get('working_count', 0)} 条")
            print(f"  长期记忆: {stats.get('long_term_count', 0)} 条")
            print(f"  总计: {stats.get('total_count', 0)} 条")
            print(f"  会话数: {stats.get('session_count', 0)}")
            print()

    def _print_sessions(self):
        # TODO: 从记忆系统中获取活跃会话列表
        print(f"\n📂 当前会话: {self._session_id}")
        print("  (提示: 使用 'session <名称>' 切换会话)")
        print()

    async def shutdown(self):
        """关闭所有组件"""
        self.logger.info("MyAgent 正在关闭...")
        self._running = False

        if self.chat_manager:
            await self.chat_manager.stop_all()

        if self.task_queue:
            await self.task_queue.stop()

        if self.memory:
            self.memory.close()

        self.logger.info("MyAgent 已关闭")


# ==============================================================================
# 系统托盘
# ==============================================================================

def create_tray_icon(app: MyAgentApp, web_port: int = 8765):
    """
    创建系统托盘图标。

    提供菜单:
      - 打开管理后台
      - 显示状态
      - 打开日志
      - 打开工作目录
      - 设置开机自启
      - 后台运行
      - 退出
    """
    try:
        import pystray
        from PIL import Image, ImageDraw
    except ImportError:
        return None

    # 生成默认图标 (简单的机器人图标)
    def create_icon_image():
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        # 画一个简单的圆形头像
        draw.ellipse([8, 4, 56, 52], fill="#4A90D9")
        # 画眼睛
        draw.ellipse([20, 20, 30, 30], fill="white")
        draw.ellipse([34, 20, 44, 30], fill="white")
        draw.ellipse([23, 23, 27, 27], fill="#333")
        draw.ellipse([37, 23, 41, 27], fill="#333")
        # 画嘴巴
        draw.arc([22, 28, 42, 42], 0, 180, fill="white", width=2)
        return img

    def open_web_ui(icon, item):
        """打开管理后台 Web UI"""
        import webbrowser
        url = f"http://127.0.0.1:{web_port}/ui/"
        webbrowser.open(url)

    def open_logs(icon, item):
        import subprocess
        import platform
        log_dir = str(app.config_mgr.logs_dir)
        system = platform.system()
        if system == "Windows":
            os.startfile(log_dir)  # type: ignore
        elif system == "Darwin":
            subprocess.Popen(["open", log_dir])
        else:
            subprocess.Popen(["xdg-open", log_dir])

    def open_workdir(icon, item):
        import subprocess
        import platform
        wd = str(app.config_mgr.data_dir / "workspace")
        Path(wd).mkdir(parents=True, exist_ok=True)
        system = platform.system()
        if system == "Windows":
            os.startfile(wd)  # type: ignore
        elif system == "Darwin":
            subprocess.Popen(["open", wd])
        else:
            subprocess.Popen(["xdg-open", wd])

    def show_status(icon, item):
        if app.main_agent:
            stats = app.main_agent.get_stats()
            print(f"\n📊 Agent 状态: {stats}")
        if app.memory:
            print(f"🧠 记忆: {app.memory.get_stats()}")
        if app.task_queue:
            print(f"📋 队列: {app.task_queue.get_stats()}")

    def toggle_autostart(icon, item):
        setup_auto_start(not item.checked)

    def on_quit(icon, item):
        icon.stop()
        if app._running:
            asyncio.run(app.shutdown())

    icon_image = create_icon_image()
    menu = pystray.Menu(
        pystray.MenuItem("🤖 MyAgent - 运行中", None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("🖥️  打开管理后台", open_web_ui,
                          default=True),  # 双击托盘图标打开
        pystray.MenuItem("📋 显示状态", show_status),
        pystray.MenuItem("📁 打开工作目录", open_workdir),
        pystray.MenuItem("📄 打开日志目录", open_logs),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("🔄 开机自启", toggle_autostart, checked=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("⏸️  最小化到后台", None, enabled=False),
        pystray.MenuItem("❌ 退出", on_quit),
    )

    return pystray.Icon("myagent", icon_image, "MyAgent AI 助手", menu)


def run_with_tray(app: MyAgentApp, web_port: int = 8765):
    """在系统托盘中运行 MyAgent"""
    tray = create_tray_icon(app, web_port)
    if tray is None:
        app.logger.warning("pystray 未安装，跳过系统托盘")
        return

    # 在后台线程运行托盘
    tray_thread = threading.Thread(target=tray.run, daemon=True)
    tray_thread.start()
    app.logger.info(f"系统托盘已启动 (管理后台: http://127.0.0.1:{web_port}/ui/)")


# ==============================================================================
# 开机自启设置
# ==============================================================================

def setup_auto_start(enable: bool = True):
    """
    设置开机自启。

    Windows: 添加到启动文件夹
    macOS: 使用 launchd (plist)
    Linux: 使用 systemd user service
    """
    import platform
    system = platform.system()

    try:
        if system == "Windows":
            _setup_autostart_windows(enable)
        elif system == "Darwin":
            _setup_autostart_macos(enable)
        elif system == "Linux":
            _setup_autostart_linux(enable)
    except Exception as e:
        print(f"⚠️ 设置开机自启失败: {e}")


def _setup_autostart_windows(enable: bool):
    """Windows: 启动文件夹快捷方式"""
    import winreg
    import os

    key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0,
                         winreg.KEY_SET_VALUE)

    if enable:
        exe_path = os.path.abspath(sys.argv[0])
        winreg.SetValueEx(key, "MyAgent", 0, winreg.REG_SZ, exe_path)
        print("✅ 已设置 Windows 开机自启")
    else:
        try:
            winreg.DeleteValue(key, "MyAgent")
            print("✅ 已取消 Windows 开机自启")
        except FileNotFoundError:
            print("ℹ️ 未找到开机自启项")

    winreg.CloseKey(key)


def _setup_autostart_macos(enable: bool):
    """macOS: LaunchAgent plist"""
    import plistlib

    plist_dir = Path.home() / "Library" / "LaunchAgents"
    plist_dir.mkdir(parents=True, exist_ok=True)
    plist_path = plist_dir / "com.myagent.plist"

    if enable:
        plist_data = {
            "Label": "com.myagent",
            "ProgramArguments": [sys.executable, str(PROJECT_ROOT / "main.py"), "--tray"],
            "RunAtLoad": True,
            "KeepAlive": False,
        }
        with open(plist_path, "wb") as f:
            plistlib.dump(plist_data, f)
        print(f"✅ 已设置 macOS 开机自启: {plist_path}")
    else:
        if plist_path.exists():
            plist_path.unlink()
            print("✅ 已取消 macOS 开机自启")


def _setup_autostart_linux(enable: bool):
    """Linux: systemd user service"""
    service_dir = Path.home() / ".config" / "systemd" / "user"
    service_dir.mkdir(parents=True, exist_ok=True)
    service_path = service_dir / "myagent.service"

    if enable:
        service_content = f"""[Unit]
Description=MyAgent AI Assistant
After=network.target

[Service]
Type=simple
ExecStart={sys.executable} {PROJECT_ROOT / 'main.py'} --tray
WorkingDirectory={PROJECT_ROOT}
Restart=on-failure

[Install]
WantedBy=default.target
"""
        with open(service_path, "w") as f:
            f.write(service_content)
        print(f"✅ 已创建 systemd service: {service_path}")
        print("   启用命令: systemctl --user enable myagent.service")
    else:
        if service_path.exists():
            service_path.unlink()
            print("✅ 已取消 Linux 开机自启")


# ==============================================================================
# 主函数
# ==============================================================================

def main():
    """主入口"""
    import argparse

    parser = argparse.ArgumentParser(description="MyAgent - 本地桌面端执行型AI助手")
    parser.add_argument("--tray", action="store_true", help="以系统托盘模式运行")
    parser.add_argument("--web", type=int, nargs="?", const=8765, default=None,
                        help="启动管理后台 Web UI (可选端口，默认8765)")
    parser.add_argument("--port", type=int, default=8765, help="Web UI 端口")
    parser.add_argument("--autostart", action="store_true", help="设置开机自启")
    parser.add_argument("--no-autostart", action="store_true", help="取消开机自启")
    parser.add_argument("--config", type=str, help="指定配置文件路径")
    parser.add_argument("--debug", action="store_true", help="调试模式")
    args = parser.parse_args()

    # 配置
    config_mgr = get_config()
    if args.debug:
        config_mgr.config.log_level = "DEBUG"
    if args.config:
        config_mgr._config_file = Path(args.config)
        config_mgr.load()

    # 开机自启
    if args.autostart:
        setup_auto_start(True)
        return
    if args.no_autostart:
        setup_auto_start(False)
        return

    # 创建应用
    app = MyAgentApp()
    web_port = args.web if args.web else args.port if args.tray else None

    # 信号处理
    def signal_handler(sig, frame):
        print("\n🛑 正在关闭...")
        if app._running:
            asyncio.run(app.shutdown())
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # 启动
    async def run():
        await app.initialize()

        # Web 管理后台
        api_server = None
        if web_port:
            from web.api_server import ApiServer
            api_server = ApiServer(app)
            await api_server.start(port=web_port)
            app.logger.info(f"管理后台: http://127.0.0.1:{web_port}/ui/")

        # 系统托盘 (默认开启管理后台)
        if args.tray or web_port:
            run_with_tray(app, web_port)

        # 启动聊天平台(后台)
        if app.chat_manager:
            asyncio.create_task(app.chat_manager.start_all())

        if args.tray:
            # 托盘模式: 后台等待
            app.logger.info("后台运行中... (Ctrl+C 退出)")
            while app._running:
                await asyncio.sleep(1)
        else:
            # CLI 模式
            await app.run_cli()

        await app.shutdown()
        if api_server:
            await api_server.stop()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
    finally:
        if app._running:
            asyncio.run(app.shutdown())


if __name__ == "__main__":
    main()
