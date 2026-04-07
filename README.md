# MyAgent - 本地桌面端执行型 AI 助手

## 概述

MyAgent 是一个功能强大的本地桌面端 AI 助手，具有极强的执行力和记忆力。

### 核心特性

- 🚀 **Open Interpreter 风格执行引擎** - 支持 Python / Shell / PowerShell 代码直接执行
- 🧠 **三层记忆系统** - 短期/工作/长期记忆 + SQLite 持久化
- 🤖 **多Agent架构** - MasterAgent / ToolAgent / MemoryAgent 协同工作
- 💬 **多平台聊天接入** - Telegram / Discord / 飞书 / QQ / 微信
- 🔧 **技能系统** - OpenClaw 风格 JSON 结构化技能调用
- 🖥️ **系统托盘** - 后台运行、开机自启
- 🛡️ **稳定可靠** - 错误自动修复、重试机制、死循环检测

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 配置 API Key

编辑 `config.yaml`，填入你的 LLM API Key:

```yaml
llm:
  api_key: "your-api-key-here"
  model: gpt-4o
```

支持多种 LLM 提供商:
- **OpenAI** (默认): GPT-4o, GPT-4, GPT-3.5
- **智谱AI**: GLM-4, GLM-3
- **自定义 API**: 任何 OpenAI 兼容的 API

### 3. 运行

```bash
# CLI 交互模式
python main.py

# 系统托盘模式 (后台运行)
python main.py --tray

# HTTP API 模式
python main.py --server --port 8080
```

## 运行模式

### CLI 模式 (默认)
直接在终端中与 AI 助手对话，支持命令行指令。

### 系统托盘模式 (`--tray`)
在系统托盘中后台运行，右键图标可查看状态、打开日志、重启、退出。

### HTTP API 模式 (`--server`)
启动 HTTP 服务，可通过 API 调用:

```bash
# 发送消息
curl -X POST http://127.0.0.1:8080/api/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "帮我查看系统信息", "session_id": "test"}'

# 查看统计
curl http://127.0.0.1:8080/api/stats

# 健康检查
curl http://127.0.0.1:8080/api/health
```

## 聊天平台接入

### Telegram
1. 在 Telegram 中找 @BotFather 创建 Bot
2. 获取 Bot Token
3. 配置 `config.yaml`:

```yaml
chatbot:
  telegram:
    enabled: true
    bot_token: "your-bot-token"
    allowed_users: ["your-user-id"]  # 安全: 限制用户
```

### Discord
1. 在 Discord Developer Portal 创建 Application
2. 创建 Bot 并获取 Token
3. 邀请 Bot 到服务器
4. 配置 `config.yaml`:

```yaml
chatbot:
  discord:
    enabled: true
    bot_token: "your-bot-token"
    allowed_channels: ["channel-id"]
```

### 飞书
1. 在飞书开放平台创建应用
2. 获取 App ID 和 App Secret
3. 配置 `config.yaml`:

```yaml
chatbot:
  feishu:
    enabled: true
    app_id: "your-app-id"
    app_secret: "your-app-secret"
```

### QQ
1. 在 QQ 开放平台注册机器人
2. 配置 `config.yaml`:

```yaml
chatbot:
  qq:
    enabled: true
    bot_appid: "your-appid"
    bot_token: "your-token"
```

### 微信
微信通过第三方 HTTP API 接入 (如 wechaty)，配置 API 地址即可:

```yaml
chatbot:
  wechat:
    enabled: true
    api_url: "http://your-wechat-api:3000"
    api_token: "your-token"
```

## 内置技能

| 技能名 | 说明 | 参数 |
|--------|------|------|
| `run_code` | 执行代码 (自动检测语言) | code, language, work_dir, timeout |
| `run_python` | 执行 Python 代码 | code, work_dir, timeout |
| `run_shell` | 执行 Shell 命令 | code, work_dir, timeout |
| `run_powershell` | 执行 PowerShell | code, work_dir, timeout |
| `read_file` | 读取文件内容 | path, encoding, offset, limit |
| `write_file` | 写入文件 | path, content, append |
| `list_directory` | 列出目录 | path, pattern, recursive |
| `delete_file` | 删除文件/目录 | path |
| `move_file` | 移动/重命名文件 | source, destination |
| `search_in_files` | 在文件中搜索 | directory, pattern, file_pattern |
| `web_search` | 搜索互联网 | query, num |
| `url_fetch` | 获取网页内容 | url |
| `get_system_info` | 获取系统信息 | - |
| `get_env` | 获取环境变量 | name |
| `set_env` | 设置环境变量 | name, value |
| `process_list` | 列出进程 | - |
| `disk_usage` | 磁盘使用情况 | path |
| `http_request` | 发送 HTTP 请求 | url, method, headers, body |
| `check_connectivity` | 网络连通性检查 | host, port |
| `text_analyze` | 文本分析 | text |
| `text_transform` | 文本转换 | text, operation |

## 项目结构

```
myagent/
├── main.py           # 主入口 (CLI/托盘/HTTP API)
├── config.py         # 配置管理
├── agent.py          # 多Agent架构
├── memory.py         # 记忆系统
├── executor.py       # 执行引擎
├── skills.py         # 技能系统
├── llm.py            # LLM 接口层
├── chatbot.py        # 聊天平台接入
├── task_queue.py     # 任务队列
├── config.yaml       # 配置文件
├── requirements.txt  # 依赖清单
├── __init__.py
├── skills/
│   └── __init__.py
├── agents/
│   └── __init__.py
├── data/             # 数据目录 (SQLite DB等)
└── logs/             # 日志目录
```

## 打包部署

### Windows (exe)
```bash
pip install pyinstaller
pyinstaller --onefile --windowed --icon=icon.ico \
  --name MyAgent \
  --hidden-import=pystray._win32 \
  --hidden-import=PIL \
  main.py
```

### macOS (dmg)
```bash
pip install pyinstaller
pyinstaller --onefile --windowed --icon=icon.icns \
  --name MyAgent \
  --osx-bundle-identifier=com.myagent.app \
  main.py
```

## 安全说明

- 默认阻止危险系统命令 (rm -rf /, format 等)
- 可配置命令白名单/黑名单
- 聊天平台支持用户白名单
- 代码执行有超时控制
- 自动检测 fork bomb 等恶意模式

## 环境变量

可通过环境变量覆盖配置 (优先级最高):

```bash
export MYAGENT_LLM_API_KEY="your-key"
export MYAGENT_LLM_MODEL="gpt-4o"
export MYAGENT_APP_LOG_LEVEL="DEBUG"
```

## 技术栈

- **Python 3.8+**
- **SQLite** - 数据持久化
- **pystray** - 系统托盘
- **Pillow** - 图标生成
- **websocket-client** - WebSocket (Discord/QQ)
- **urllib** - HTTP 请求 (标准库，无额外依赖)
- **yaml** - 配置文件

## License

MIT
