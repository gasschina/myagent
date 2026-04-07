"""
MyAgent - 本地桌面端执行型 AI 助手
====================================
主入口: 系统托盘后台运行 + CLI 交互
支持 Windows / macOS 双平台
"""
import os
import sys
import json
import time
import signal
import logging
import threading
import platform
from pathlib import Path
from typing import Optional

# 确保项目根目录在 Python 路径中
PROJECT_ROOT = Path(__file__).parent.resolve()
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config import init_config, get_config, Config
from memory import MemoryManager, MemoryStore
from executor import Executor
from skills import SkillRegistry, BuiltinSkills
from llm import LLMClient, init_llm, get_llm
from agent import AgentController, MasterAgent, ToolAgent, MemoryAgent
from chatbot import ChatBotManager
from task_queue import TaskQueue


# ============================================================
# 日志配置
# ============================================================

def setup_logging(config: Config) -> logging.Logger:
    """配置日志系统"""
    log_level = config.get("app.log_level", "INFO")
    log_file = config.get("app.log_file", "logs/myagent.log")

    # 确保日志目录存在
    Path(log_file).parent.mkdir(parents=True, exist_ok=True)

    # 配置根日志
    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, log_level.upper(), logging.INFO))

    # 文件处理器
    from logging.handlers import RotatingFileHandler
    max_size = config.get("app.max_log_size_mb", 50) * 1024 * 1024
    backup_count = config.get("app.log_backup_count", 5)

    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=max_size,
        backupCount=backup_count,
        encoding='utf-8',
    )
    file_handler.setFormatter(logging.Formatter(
        '%(asctime)s [%(levelname)s] [%(name)s] %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    ))
    root_logger.addHandler(file_handler)

    # 控制台处理器
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(logging.Formatter(
        '[%(levelname)s] %(message)s'
    ))
    root_logger.addHandler(console_handler)

    logger = logging.getLogger("myagent")
    logger.info(f"MyAgent v{config.get('app.version', '1.0.0')} 启动")
    logger.info(f"平台: {platform.system()} {platform.release()}")
    logger.info(f"Python: {platform.python_version()}")
    logger.info(f"工作目录: {os.getcwd()}")

    return logger


# ============================================================
# 系统托盘
# ============================================================

class TrayApp:
    """
    系统托盘应用
    使用 pystray 实现后台运行
    """

    def __init__(self, agent: AgentController, chatbot_manager: ChatBotManager, logger_instance):
        self.agent = agent
        self.chatbot_manager = chatbot_manager
        self.logger = logger_instance
        self._tray = None
        self._is_running = True
        self._status = "运行中"

    def create_icon(self):
        """创建托盘图标 (程序化生成)"""
        try:
            from PIL import Image, ImageDraw, ImageFont

            # 创建一个简单的图标
            size = 64
            img = Image.new('RGBA', (size, size), (0, 0, 0, 0))
            draw = ImageDraw.Draw(img)

            # 背景圆
            draw.ellipse([4, 4, 60, 60], fill=(52, 152, 219, 255))

            # "M" 字母
            try:
                font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc" if platform.system() == "Darwin" else "arial.ttf", 32)
            except:
                font = ImageFont.load_default()

            draw.text((12, 12), "M", fill=(255, 255, 255, 255), font=font)

            return img

        except ImportError:
            self.logger.warning("PIL 未安装，使用默认图标")
            return None
        except Exception as e:
            self.logger.warning(f"创建图标失败: {e}")
            return None

    def _get_status_text(self) -> str:
        """获取状态文本"""
        stats = self.agent.get_stats()
        active_platforms = self.chatbot_manager.get_active_platforms()

        lines = [
            f"状态: {self._status}",
            f"活跃会话: {stats.get('active_sessions', 0)}",
            f"工具调用: {stats.get('tool_calls', 0)}",
        ]

        if active_platforms:
            lines.append(f"聊天平台: {', '.join(active_platforms)}")
        else:
            lines.append("聊天平台: 未启用")

        return "\n".join(lines)

    def _open_logs(self):
        """打开日志目录"""
        log_file = get_config().get("app.log_file", "logs/myagent.log")
        log_path = Path(log_file).parent.resolve()
        if log_path.exists():
            if platform.system() == "Windows":
                os.startfile(str(log_path))
            elif platform.system() == "Darwin":
                os.system(f'open "{log_path}"')
            else:
                os.system(f'xdg-open "{log_path}"')

    def _open_data_dir(self):
        """打开数据目录"""
        data_dir = Path(get_config().get("app.data_dir", "data")).resolve()
        if data_dir.exists():
            if platform.system() == "Windows":
                os.startfile(str(data_dir))
            elif platform.system() == "Darwin":
                os.system(f'open "{data_dir}"')
            else:
                os.system(f'xdg-open "{data_dir}"')

    def _show_stats(self):
        """显示统计信息"""
        stats = self.agent.get_stats()
        memory_stats = self.agent.get_memory_stats()
        chatbot_stats = self.chatbot_manager.get_stats()

        info = f"""
╔══════════════════════════════════╗
║     MyAgent 运行状态             ║
╠══════════════════════════════════╣
║ Agent 统计:
║   活跃会话: {stats.get('active_sessions', 0)}
║   工具调用: {stats.get('tool_calls', 0)}
║   执行统计: {json.dumps(stats.get('executor_stats', {}), ensure_ascii=False)}
╠══════════════════════════════════╣
║ 记忆统计: {json.dumps(memory_stats.get('stats', {}), ensure_ascii=False)}
╠══════════════════════════════════╣
║ 聊天平台: {json.dumps(chatbot_stats, ensure_ascii=False)}
╚══════════════════════════════════╝
"""
        self.logger.info(info)

    def _restart(self):
        """重启"""
        self.logger.info("重启 MyAgent...")
        python = sys.executable
        os.execl(python, python, *sys.argv)

    def _quit(self, icon=None, item=None):
        """退出"""
        self.logger.info("MyAgent 正在关闭...")
        self._is_running = False
        self.chatbot_manager.stop_all()
        self.agent.shutdown()
        if self._tray:
            self._tray.stop()
        os._exit(0)

    def run(self):
        """启动系统托盘"""
        try:
            import pystray
            from pystray import MenuItem, Menu
        except ImportError:
            self.logger.error("pystray 未安装，请运行: pip install pystray")
            return

        icon_image = self.create_icon()

        menu = Menu(
            MenuItem(
                "MyAgent - " + self._status,
                None,
                enabled=False,
            ),
            Menu.SEPARATOR,
            MenuItem(
                "状态信息",
                lambda icon, item: self._show_stats(),
            ),
            MenuItem(
                "打开日志",
                lambda icon, item: self._open_logs(),
            ),
            MenuItem(
                "打开数据目录",
                lambda icon, item: self._open_data_dir(),
            ),
            Menu.SEPARATOR,
            MenuItem(
                "重启",
                lambda icon, item: self._restart(),
            ),
            MenuItem(
                "退出",
                lambda icon, item: self._quit(icon, item),
            ),
        )

        self._tray = pystray.Icon(
            name="MyAgent",
            icon=icon_image,
            title="MyAgent AI 助手",
            menu=menu,
        )

        self.logger.info("系统托盘已启动，右键点击图标可操作")
        self._tray.run()


# ============================================================
# CLI 交互模式
# ============================================================

class CLIInterface:
    """命令行交互界面"""

    def __init__(self, agent: AgentController, logger_instance):
        self.agent = agent
        self.logger = logger_instance
        self._current_session = None
        self._running = True

    def run(self):
        """运行 CLI 交互"""
        print()
        print("=" * 50)
        print("  MyAgent - 本地执行型 AI 助手")
        print("  输入消息开始对话，输入 /help 查看帮助")
        print("=" * 50)
        print()

        while self._running:
            try:
                user_input = input("你> ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\n再见！")
                break

            if not user_input:
                continue

            # 处理命令
            if user_input.startswith("/"):
                self._handle_command(user_input)
                continue

            # 发送给 Agent
            try:
                print("思考中...", end="", flush=True)
                response = self.agent.chat(
                    message=user_input,
                    session_id=self._current_session,
                    callback=lambda msg: print(f"\r[执行] {msg}", end="", flush=True),
                )
                print(f"\r助手> {response}")
                print()

                if not self._current_session:
                    # 从 agent 获取 session
                    pass

            except Exception as e:
                print(f"\n错误: {e}")
                self.logger.error(f"处理消息异常: {e}", exc_info=True)

    def _handle_command(self, cmd: str):
        """处理斜杠命令"""
        parts = cmd.split(maxsplit=1)
        command = parts[0].lower()
        args = parts[1] if len(parts) > 1 else ""

        if command == "/help":
            print("""
可用命令:
  /help          显示帮助
  /new           新建会话
  /session       显示当前会话ID
  /stats         显示统计信息
  /memory        显示记忆统计
  /clear         清除当前会话
  /quit          退出
  /exec <code>   直接执行代码
  /tool <name>   调用技能
""")
        elif command == "/new":
            self._current_session = None
            print("已新建会话")
        elif command == "/session":
            print(f"当前会话: {self._current_session or '(新会话)'}")
        elif command == "/stats":
            stats = self.agent.get_stats()
            print(f"统计: {json.dumps(stats, ensure_ascii=False, indent=2)}")
        elif command == "/memory":
            mem_stats = self.agent.get_memory_stats()
            print(f"记忆统计: {json.dumps(mem_stats, ensure_ascii=False, indent=2)}")
        elif command == "/clear":
            self._current_session = None
            print("会话已清除")
        elif command == "/quit":
            self._running = False
        elif command == "/exec":
            if args:
                from executor import execute_code
                result = execute_code(args)
                print(result.to_json())
        elif command == "/tool":
            print("技能列表:")
            from skills import get_skill_registry
            registry = get_skill_registry()
            for skill in registry.list_skills():
                print(f"  - {skill.name}: {skill.description}")
        else:
            print(f"未知命令: {command}，输入 /help 查看帮助")


# ============================================================
# 应用主类
# ============================================================

class MyAgentApp:
    """
    MyAgent 应用主类
    初始化所有组件，协调运行
    """

    def __init__(self):
        self.config: Optional[Config] = None
        self.logger: Optional[logging.Logger] = None
        self.agent: Optional[AgentController] = None
        self.chatbot_manager: Optional[ChatBotManager] = None
        self.task_queue: Optional[TaskQueue] = None
        self._components_initialized = False

    def initialize(self, config_path: Optional[str] = None) -> bool:
        """初始化所有组件"""
        try:
            # 1. 配置
            self.config = init_config(config_path)

            # 2. 日志
            self.logger = setup_logging(self.config)

            # 3. LLM
            try:
                init_llm()
                llm = get_llm()
                self.logger.info(f"LLM 初始化完成: {llm.model}")
            except Exception as e:
                self.logger.error(f"LLM 初始化失败: {e}")

            # 4. 技能系统
            try:
                from skills import get_skill_registry
                registry = get_skill_registry()
                self.logger.info(f"技能系统初始化完成: {len(registry.list_skills())} 个技能")
            except Exception as e:
                self.logger.error(f"技能系统初始化失败: {e}")

            # 5. Agent
            try:
                self.agent = AgentController()
                self.logger.info("Agent 初始化完成")
            except Exception as e:
                self.logger.error(f"Agent 初始化失败: {e}")

            # 6. 聊天平台
            try:
                self.chatbot_manager = ChatBotManager(
                    on_message_handler=self._chat_message_handler
                )
                self.chatbot_manager.setup()
            except Exception as e:
                self.logger.error(f"聊天平台初始化失败: {e}")

            # 7. 任务队列
            try:
                self.task_queue = TaskQueue(max_workers=3)
                self.task_queue.start()
            except Exception as e:
                self.logger.error(f"任务队列初始化失败: {e}")

            self._components_initialized = True
            self.logger.info("所有组件初始化完成")
            return True

        except Exception as e:
            print(f"初始化失败: {e}")
            if self.logger:
                self.logger.critical(f"初始化失败: {e}", exc_info=True)
            return False

    def _chat_message_handler(self, message: str, session_id: str) -> str:
        """聊天平台消息处理回调"""
        if self.agent:
            return self.agent.chat(message, session_id=session_id)
        return "Agent 未初始化"

    def run_cli(self):
        """以 CLI 模式运行"""
        if not self._components_initialized:
            self.initialize()

        self.logger.info("启动 CLI 模式")

        # 启动聊天平台 (后台)
        if self.chatbot_manager:
            self.chatbot_manager.start_all()

        # 运行 CLI
        cli = CLIInterface(self.agent, self.logger)
        try:
            cli.run()
        finally:
            self.shutdown()

    def run_tray(self):
        """以系统托盘模式运行"""
        if not self._components_initialized:
            self.initialize()

        self.logger.info("启动系统托盘模式")

        # 启动聊天平台 (后台)
        if self.chatbot_manager:
            self.chatbot_manager.start_all()

        # 启动托盘
        tray = TrayApp(self.agent, self.chatbot_manager, self.logger)
        try:
            tray.run()
        finally:
            self.shutdown()

    def run_server(self, host: str = "127.0.0.1", port: int = 8080):
        """以 HTTP API 模式运行"""
        if not self._components_initialized:
            self.initialize()

        self.logger.info(f"启动 HTTP API 模式: {host}:{port}")

        # 启动聊天平台 (后台)
        if self.chatbot_manager:
            self.chatbot_manager.start_all()

        # 启动 HTTP 服务
        from http.server import HTTPServer, BaseHTTPRequestHandler
        import urllib.parse

        app = self

        class APIHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                """处理 POST 请求"""
                if self.path == "/api/chat":
                    content_length = int(self.headers.get('Content-Length', 0))
                    body = self.rfile.read(content_length)
                    try:
                        data = json.loads(body)
                        message = data.get("message", "")
                        session_id = data.get("session_id", "")

                        if not message:
                            self._send_json({"error": "message 不能为空"}, 400)
                            return

                        response = app.agent.chat(message, session_id=session_id)
                        self._send_json({"response": response})

                    except json.JSONDecodeError:
                        self._send_json({"error": "无效的 JSON"}, 400)
                    except Exception as e:
                        self._send_json({"error": str(e)}, 500)
                else:
                    self._send_json({"error": "Not Found"}, 404)

            def do_GET(self):
                """处理 GET 请求"""
                if self.path == "/api/stats":
                    stats = app.agent.get_stats()
                    stats.update(app.agent.get_memory_stats())
                    self._send_json(stats)
                elif self.path == "/api/health":
                    self._send_json({"status": "ok", "version": app.config.get("app.version", "1.0.0")})
                else:
                    self._send_json({"error": "Not Found"}, 404)

            def _send_json(self, data: dict, status: int = 200):
                self.send_response(status)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.end_headers()
                self.wfile.write(json.dumps(data, ensure_ascii=False).encode('utf-8'))

            def log_message(self, format, *args):
                app.logger.debug(f"HTTP: {format % args}")

        server = HTTPServer((host, port), APIHandler)
        self.logger.info(f"HTTP API 已启动: http://{host}:{port}")

        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
            self.shutdown()

    def shutdown(self):
        """关闭所有组件"""
        self.logger.info("正在关闭...")
        if self.chatbot_manager:
            self.chatbot_manager.stop_all()
        if self.agent:
            self.agent.shutdown()
        if self.task_queue:
            self.task_queue.stop()
        self.logger.info("已关闭")


# ============================================================
# 入口函数
# ============================================================

def main():
    """主入口"""
    import argparse

    parser = argparse.ArgumentParser(
        description="MyAgent - 本地桌面端执行型 AI 助手",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
使用示例:
  python main.py                   # CLI 交互模式
  python main.py --tray            # 系统托盘模式
  python main.py --server          # HTTP API 模式 (端口 8080)
  python main.py --server --port 3000  # HTTP API 模式 (端口 3000)
  python main.py --config my.yaml  # 使用指定配置文件
        """
    )

    parser.add_argument(
        "--tray", "-t",
        action="store_true",
        help="以系统托盘模式运行",
    )
    parser.add_argument(
        "--server", "-s",
        action="store_true",
        help="以 HTTP API 服务模式运行",
    )
    parser.add_argument(
        "--port", "-p",
        type=int,
        default=8080,
        help="HTTP API 端口 (默认 8080)",
    )
    parser.add_argument(
        "--host",
        type=str,
        default="127.0.0.1",
        help="HTTP API 监听地址 (默认 127.0.0.1)",
    )
    parser.add_argument(
        "--config", "-c",
        type=str,
        default=None,
        help="配置文件路径",
    )
    parser.add_argument(
        "--version", "-v",
        action="version",
        version="MyAgent v1.0.0",
    )

    args = parser.parse_args()

    # 初始化应用
    app = MyAgentApp()

    # 信号处理
    def signal_handler(sig, frame):
        print("\n收到退出信号...")
        app.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    if hasattr(signal, 'SIGTERM'):
        signal.signal(signal.SIGTERM, signal_handler)

    # 运行
    if args.tray:
        app.run_tray()
    elif args.server:
        app.run_server(host=args.host, port=args.port)
    else:
        app.run_cli()


if __name__ == "__main__":
    main()
