# MyAgent - 本地桌面端执行型AI助手

> 🤖 一个执行力极强、记忆力极强、运行稳定的本地桌面端AI助手。
> 支持 Windows / macOS / Linux，系统托盘后台运行。
> Open Interpreter 风格执行引擎 + 三层记忆系统 + 多Agent架构 + 多平台接入。

---

## 🌟 核心特性

### 🚀 执行引擎
- **多语言执行**: Python / Shell (Bash) / PowerShell / CMD
- **自动修复**: ImportError 自动安装、编码修复、缩进修复
- **安全控制**: 危险命令拦截、超时控制、输出截断
- **结构化结果**: 执行结果标准化，LLM 稳定理解

### 🧠 三层记忆系统
- **短期记忆**: 对话上下文，自动淘汰旧消息
- **工作记忆**: 任务进度、执行步骤、中间结果
- **长期记忆**: 用户偏好、技能经验、错误模式
- **SQLite 持久化**: 本地存储，支持跨会话检索

### 🤝 多Agent架构
- **主Agent**: 任务规划、Agent调度、结果汇总
- **工具Agent**: 代码执行、技能调用、文件操作
- **记忆Agent**: 记忆读写、经验总结、错误记录
- **计划-执行-反思循环**: 自动迭代直到完成

### 💬 多聊天平台接入
- **Telegram**: 官方 Bot API
- **Discord**: discord.py
- **飞书**: Webhook 长连接
- **QQ**: OneBot v11 协议
- **微信**: WxPusher / HTTP 桥接
- 纯 Python 实现，无需 Node.js

### 🛠️ 技能系统 (OpenClaw 兼容)
- **文件操作**: 读写、搜索、移动、删除
- **网络搜索**: DuckDuckGo 免API搜索
- **系统操作**: 信息查询、进程管理、环境变量
- **浏览器自动化**: Playwright 驱动
- JSON Schema 定义，支持 LLM Function Calling

### 🛡️ 稳定性保障
- 任务队列机制 (优先级调度)
- 执行超时控制
- 异常捕获与自动恢复
- LLM 输出 JSON 格式强校验
- 无 HTTP 网关、单机原生运行

### 📱 系统托盘
- 后台静默运行
- 开机自启支持
- 日志目录快捷访问
- 可打包为 exe / dmg

---

## 📁 项目结构

```
myagent/
├── main.py                 # 主入口 + 系统托盘 + CLI
├── config.py               # 配置管理 (环境变量/配置文件/默认值)
│
├── core/                   # 核心模块
│   ├── llm.py              # LLM 客户端 (OpenAI/Anthropic/Ollama)
│   ├── task_queue.py       # 任务队列
│   ├── logger.py           # 日志系统
│   └── utils.py            # 工具函数
│
├── memory/                 # 记忆系统
│   └── manager.py          # 三层记忆管理器 (SQLite)
│
├── executor/               # 执行引擎
│   └── engine.py           # 代码执行器 (Python/Shell/PowerShell)
│
├── agents/                 # Agent 架构
│   ├── base.py             # Agent 基类
│   ├── main_agent.py       # 主Agent (规划调度)
│   ├── tool_agent.py       # 工具Agent (执行调用)
│   └── memory_agent.py     # 记忆Agent (读写总结)
│
├── chatbot/                # 聊天平台接入
│   ├── base.py             # 平台基类
│   ├── manager.py          # 平台管理器
│   ├── telegram_bot.py     # Telegram
│   ├── discord_bot.py      # Discord
│   ├── feishu_bot.py       # 飞书
│   ├── qq_bot.py           # QQ
│   └── wechat_bot.py       # 微信
│
├── skills/                 # 技能系统
│   ├── base.py             # 技能基类 (OpenClaw 兼容)
│   ├── registry.py         # 技能注册表
│   ├── file_skill.py       # 文件操作
│   ├── search_skill.py     # 搜索技能
│   ├── system_skill.py     # 系统操作
│   └── browser_skill.py    # 浏览器自动化
│
├── requirements.txt        # 依赖清单
├── setup.py                # 打包配置
└── README.md               # 本文件
```

---

## 🚀 快速开始

### 1. 安装依赖

```bash
# 克隆项目
git clone https://github.com/ctz168/myagent.git
cd myagent

# 安装核心依赖
pip install -r requirements.txt

# 按需安装聊天平台
pip install python-telegram-bot   # Telegram
pip install discord.py            # Discord
pip install playwright && playwright install chromium  # 浏览器
pip install anthropic            # Claude
```

### 2. 配置 LLM

**方式一: 环境变量 (推荐)**
```bash
export MYAGENT_LLM_API_KEY="sk-your-openai-key"
export MYAGENT_LLM_MODEL="gpt-4"
```

**方式二: 配置文件**
```bash
# 首次运行会自动创建 ~/.myagent/config.json
python main.py
```

**方式三: 使用 Ollama 本地模型 (免费)**
```bash
# 安装 Ollama
curl -fsSL https://ollama.ai/install.sh | sh
ollama pull llama3

# 配置
export MYAGENT_LLM_PROVIDER="ollama"
export MYAGENT_OLLAMA_MODEL="llama3"
```

**方式四: 使用 Anthropic Claude**
```bash
export MYAGENT_LLM_PROVIDER="anthropic"
export MYAGENT_ANTHROPIC_API_KEY="sk-ant-..."
```

### 3. 运行

```bash
# 交互式命令行
python main.py

# 系统托盘后台运行
python main.py --tray

# 调试模式
python main.py --debug

# 设置开机自启
python main.py --autostart
```

### 4. 配置聊天平台 (可选)

编辑 `~/.myagent/config.json`:

```json
{
  "llm": {
    "provider": "openai",
    "api_key": "sk-...",
    "model": "gpt-4"
  },
  "chat_platforms": [
    {
      "platform": "telegram",
      "enabled": true,
      "token": "YOUR_BOT_TOKEN"
    },
    {
      "platform": "discord",
      "enabled": true,
      "token": "YOUR_DISCORD_BOT_TOKEN"
    }
  ]
}
```

或使用环境变量:
```bash
export MYAGENT_TELEGRAM_TOKEN="your-telegram-bot-token"
export MYAGENT_DISCORD_TOKEN="your-discord-bot-token"
```

---

## 💡 使用示例

### CLI 交互
```
👤 你: 帮我在桌面创建一个 hello.py 文件
🤖 助手: ✅ 已在桌面创建 hello.py

👤 你: 搜索最新的Python 3.12新特性
🤖 助手: [搜索结果...]

👤 你: 查看系统信息
🤖 助手: 系统: macOS 14.0 | CPU: 8核 | 内存: 12.3GB可用
```

### CLI 内置命令
| 命令 | 说明 |
|------|------|
| `help` | 显示帮助 |
| `status` | 查看系统状态 |
| `skills` | 列出所有技能 |
| `memory` | 查看记忆统计 |
| `sessions` | 查看会话列表 |
| `session <id>` | 切换会话 |
| `clear` | 清除当前对话历史 |
| `quit` | 退出 |

### 技能调用
```
# 直接调用技能
👤 你: !system_info
👤 你: !file_read path=/tmp/test.txt
👤 你: !web_search query=AI news
```

---

## 🏗️ 扩展开发

### 添加自定义技能
```python
# skills/my_skill.py
from skills.base import Skill, SkillResult, SkillParameter

class MyCustomSkill(Skill):
    name = "my_skill"
    description = "我的自定义技能"
    category = "custom"
    parameters = [
        SkillParameter("input", "string", "输入参数", required=True),
    ]

    async def execute(self, input: str = "", **kwargs) -> SkillResult:
        # 你的逻辑
        return SkillResult(
            success=True,
            data={"result": input.upper()},
            message=f"处理完成: {input}",
        )
```

技能会自动被发现和注册 (命名规则: `*_skill.py`)。

### 添加自定义聊天平台
```python
# chatbot/my_platform_bot.py
from chatbot.base import BaseChatBot, ChatMessage, ChatResponse

class MyPlatformBot(BaseChatBot):
    platform_name = "my_platform"

    async def start(self):
        # 实现启动逻辑
        pass

    async def stop(self):
        pass

    async def send_message(self, response: ChatResponse) -> bool:
        pass
```

然后在 `chatbot/manager.py` 中注册即可。

---

## 📦 打包

### Windows (exe)
```bash
pip install pyinstaller
pyinstaller --onefile --windowed --icon=icon.ico \
    --add-data "skills:skills" \
    --add-data "chatbot:chatbot" \
    --name MyAgent main.py
```

### macOS (dmg)
```bash
pyinstaller --onefile --windowed --icon=icon.icns \
    --add-data "skills:skills" \
    --name MyAgent main.py
# 然后使用 hdiutil 创建 dmg
```

---

## ⚙️ 环境变量参考

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `MYAGENT_LLM_PROVIDER` | LLM 提供商 | openai |
| `MYAGENT_LLM_API_KEY` | API Key | - |
| `MYAGENT_LLM_BASE_URL` | API 地址 | OpenAI 官方 |
| `MYAGENT_LLM_MODEL` | 模型名称 | gpt-4 |
| `MYAGENT_LLM_TEMPERATURE` | 温度 | 0.1 |
| `MYAGENT_TELEGRAM_TOKEN` | Telegram Bot Token | - |
| `MYAGENT_DISCORD_TOKEN` | Discord Bot Token | - |
| `MYAGENT_FEISHU_APP_ID` | 飞书 App ID | - |
| `MYAGENT_FEISHU_APP_SECRET` | 飞书 App Secret | - |
| `MYAGENT_LOG_LEVEL` | 日志级别 | INFO |

---

## 📄 License

MIT
