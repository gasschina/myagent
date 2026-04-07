"""
config.py - 全局配置管理模块
=============================
集中管理所有配置项，支持从环境变量、配置文件、默认值三级加载。
配置文件默认路径: ~/.myagent/config.json
"""
from __future__ import annotations

import json
import os
import sys
import platform
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import Optional, Dict, Any, List


# ==============================================================================
# 配置数据类
# ==============================================================================

@dataclass
class LLMConfig:
    """LLM 大模型配置"""
    provider: str = "openai"           # openai | anthropic | ollama | custom
    api_key: str = ""
    base_url: str = "https://api.openai.com/v1"
    model: str = "gpt-4"
    temperature: float = 0.1
    max_tokens: int = 4096
    timeout: int = 120                 # 请求超时(秒)
    max_retries: int = 3               # 最大重试次数
    # Anthropic 专用
    anthropic_api_key: str = ""
    # Ollama 专用
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "llama3"


@dataclass
class MemoryConfig:
    """记忆系统配置"""
    db_path: str = ""                  # SQLite 数据库路径，默认 ~/.myagent/memory.db
    max_short_term: int = 50           # 短期记忆最大轮数
    max_working: int = 100             # 工作记忆最大条数
    auto_summarize: bool = True        # 自动总结开关
    summarize_threshold: int = 20      # 触发总结的对话轮数


@dataclass
class ExecutorConfig:
    """执行引擎配置"""
    timeout: int = 300                 # 默认执行超时(秒)
    max_retries: int = 2               # 自动重试次数
    auto_fix: bool = True              # 自动修复错误
    max_output_length: int = 50000     # 输出最大长度
    execution_mode: str = "local"      # 执行模式: local(本机) | sandbox(沙盒)
    sandbox_image: str = "python:3.12-slim"  # 沙盒 Docker 镜像
    sandbox_network: bool = False      # 沙盒是否允许网络
    sandbox_memory: str = "512m"       # 沙盒内存限制
    allowed_dirs: List[str] = field(default_factory=list)  # 允许访问的目录(空=全部)
    blocked_commands: List[str] = field(default_factory=lambda: [
        "rm -rf /", "format", "del /f /s /q C:\\", "mkfs", "dd if=/dev/zero"
    ])


@dataclass
class AgentConfig:
    """Agent 配置"""
    max_iterations: int = 30           # 单任务最大迭代次数
    max_parallel: int = 3              # 最大并行任务数
    verbose: bool = True               # 详细日志


@dataclass
class TrayConfig:
    """系统托盘配置"""
    auto_start: bool = False           # 开机自启
    show_notifications: bool = True    # 显示通知
    icon_path: str = ""                # 托盘图标路径


@dataclass
class ChatPlatformConfig:
    """单个聊天平台配置"""
    enabled: bool = False
    platform: str = ""                 # telegram | discord | feishu | qq | wechat
    token: str = ""                    # Bot Token
    app_id: str = ""                   # App ID (某些平台需要)
    app_secret: str = ""               # App Secret
    webhook_url: str = ""              # Webhook URL
    allowed_users: List[str] = field(default_factory=list)  # 允许的用户白名单(空=全部)
    extra: Dict[str, Any] = field(default_factory=dict)     # 平台特有配置


@dataclass
class AppConfig:
    """应用总配置"""
    llm: LLMConfig = field(default_factory=LLMConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    executor: ExecutorConfig = field(default_factory=ExecutorConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    tray: TrayConfig = field(default_factory=TrayConfig)
    chat_platforms: List[ChatPlatformConfig] = field(default_factory=list)
    log_level: str = "INFO"
    data_dir: str = ""                 # 数据目录，默认 ~/.myagent/
    language: str = "zh-CN"


# ==============================================================================
# 配置管理器
# ==============================================================================

class ConfigManager:
    """
    配置管理器 - 三级加载策略
    优先级: 环境变量 > 配置文件 > 默认值
    """

    CONFIG_DIR_NAME = ".myagent"
    CONFIG_FILE_NAME = "config.json"

    def __init__(self):
        self._config = AppConfig()
        self._config_dir = Path.home() / self.CONFIG_DIR_NAME
        self._config_file = self._config_dir / self.CONFIG_FILE_NAME
        self._data_dir = self._config_dir
        self._ensure_dirs()

    def _ensure_dirs(self):
        """确保必要目录存在"""
        self._config_dir.mkdir(parents=True, exist_ok=True)
        (self._config_dir / "data").mkdir(exist_ok=True)
        (self._config_dir / "logs").mkdir(exist_ok=True)

    @property
    def config(self) -> AppConfig:
        return self._config

    @property
    def config_dir(self) -> Path:
        return self._config_dir

    @property
    def data_dir(self) -> Path:
        return self._data_dir / "data"

    @property
    def logs_dir(self) -> Path:
        return self._config_dir / "logs"

    def load(self) -> AppConfig:
        """加载配置(配置文件 + 环境变量覆盖)"""
        self._load_from_file()
        self._load_from_env()
        self._apply_defaults()
        return self._config

    def save(self) -> None:
        """保存当前配置到文件"""
        config_dict = self._to_dict(self._config)
        with open(self._config_file, "w", encoding="utf-8") as f:
            json.dump(config_dict, f, ensure_ascii=False, indent=2)

    def _load_from_file(self):
        """从配置文件加载"""
        if not self._config_file.exists():
            self.save()  # 创建默认配置
            return
        try:
            with open(self._config_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            self._apply_dict(self._config, data)
        except (json.JSONDecodeError, IOError) as e:
            print(f"[CONFIG] 配置文件读取失败，使用默认值: {e}")

    def _load_from_env(self):
        """从环境变量加载(前缀 MYAGENT_)"""
        env_mapping = {
            "MYAGENT_LLM_PROVIDER": ("llm", "provider"),
            "MYAGENT_LLM_API_KEY": ("llm", "api_key"),
            "MYAGENT_LLM_BASE_URL": ("llm", "base_url"),
            "MYAGENT_LLM_MODEL": ("llm", "model"),
            "MYAGENT_LLM_TEMPERATURE": ("llm", "temperature", float),
            "MYAGENT_LLM_MAX_TOKENS": ("llm", "max_tokens", int),
            "MYAGENT_ANTHROPIC_API_KEY": ("llm", "anthropic_api_key"),
            "MYAGENT_OLLAMA_BASE_URL": ("llm", "ollama_base_url"),
            "MYAGENT_OLLAMA_MODEL": ("llm", "ollama_model"),
            "MYAGENT_LOG_LEVEL": ("log_level", None, str),
            "MYAGENT_LANGUAGE": ("language", None, str),
        }
        for env_key, mapping in env_mapping.items():
            value = os.environ.get(env_key)
            if value is None:
                continue
            if len(mapping) == 3:
                group, attr, converter = mapping
            elif len(mapping) == 2:
                group, attr = mapping
                converter = str
            else:
                continue
            target = getattr(self._config, group) if group else self._config
            try:
                setattr(target, attr, converter(value))
            except (ValueError, AttributeError):
                pass

        # 聊天平台 Token 从环境变量加载
        platform_env = {
            "MYAGENT_TELEGRAM_TOKEN": "telegram",
            "MYAGENT_DISCORD_TOKEN": "discord",
            "MYAGENT_FEISHU_APP_ID": "feishu",
            "MYAGENT_FEISHU_APP_SECRET": "feishu",
            "MYAGENT_QQ_TOKEN": "qq",
            "MYAGENT_WECHAT_TOKEN": "wechat",
        }
        for env_key, platform_name in platform_env.items():
            token = os.environ.get(env_key)
            if token:
                self._ensure_chat_platform(platform_name, token)

    def _ensure_chat_platform(self, platform: str, token: str):
        """确保聊天平台配置存在"""
        for cp in self._config.chat_platforms:
            if cp.platform == platform:
                cp.token = token or cp.token
                cp.enabled = True
                return
        cp = ChatPlatformConfig(platform=platform, token=token, enabled=True)
        self._config.chat_platforms.append(cp)

    def _apply_defaults(self):
        """应用平台相关的默认值"""
        if not self._config.memory.db_path:
            self._config.memory.db_path = str(self.data_dir / "memory.db")
        if not self._config.data_dir:
            self._config.data_dir = str(self._config_dir)

        # 设置 Ollama base_url
        if self._config.llm.provider == "ollama" and not self._config.llm.base_url:
            self._config.llm.base_url = self._config.llm.ollama_base_url
            self._config.llm.model = self._config.llm.ollama_model

    def _to_dict(self, obj) -> dict:
        """递归 dataclass 转 dict"""
        if hasattr(obj, '__dataclass_fields__'):
            return {k: self._to_dict(v) for k, v in asdict(obj).items()}
        elif isinstance(obj, list):
            return [self._to_dict(i) for i in obj]
        return obj

    def _apply_dict(self, target, data: dict):
        """递归将 dict 应用到 dataclass"""
        for key, value in data.items():
            if not hasattr(target, key):
                continue
            current = getattr(target, key)
            if isinstance(value, dict) and hasattr(current, '__dataclass_fields__'):
                self._apply_dict(current, value)
            elif isinstance(value, list) and key == "chat_platforms":
                platforms = []
                for item in value:
                    cp = ChatPlatformConfig(**{
                        k: v for k, v in item.items() if k in ChatPlatformConfig.__dataclass_fields__
                    })
                    platforms.append(cp)
                setattr(target, key, platforms)
            elif key in getattr(type(target), '__dataclass_fields__', {}):
                setattr(target, key, value)

    def get_chat_platform(self, platform: str) -> Optional[ChatPlatformConfig]:
        """获取指定聊天平台配置"""
        for cp in self._config.chat_platforms:
            if cp.platform == platform:
                return cp
        return None

    def get_enabled_platforms(self) -> List[ChatPlatformConfig]:
        """获取所有启用的聊天平台"""
        return [cp for cp in self._config.chat_platforms if cp.enabled]

    # ── 热重载 ──
    def reload(self) -> AppConfig:
        """从配置文件重新加载配置(热重载，不重启服务)"""
        old_config = self._config
        self._config = AppConfig()
        self._load_from_file()
        self._load_from_env()
        self._apply_defaults()
        return self._config

    def update_llm(self, **kwargs) -> AppConfig:
        """更新 LLM 配置并立即生效"""
        for k, v in kwargs.items():
            if hasattr(self._config.llm, k):
                setattr(self._config.llm, k, v)
        self.save()
        return self._config

    def update_executor(self, **kwargs) -> AppConfig:
        """更新执行引擎配置并立即生效"""
        for k, v in kwargs.items():
            if hasattr(self._config.executor, k):
                setattr(self._config.executor, k, v)
        self.save()
        return self._config

    # ── 导入 / 导出 ──
    def export_config(self, include_secrets: bool = False) -> dict:
        """导出完整配置为字典（含备份元数据）"""
        cfg = self._to_dict(self._config)
        if not include_secrets:
            # 脱敏处理
            if 'api_key' in cfg.get('llm', {}):
                key = cfg['llm']['api_key']
                if key:
                    cfg['llm']['api_key'] = key[:6] + '****' + key[-4:] if len(key) > 10 else '****'
            if 'anthropic_api_key' in cfg.get('llm', {}):
                key = cfg['llm']['anthropic_api_key']
                if key:
                    cfg['llm']['anthropic_api_key'] = key[:6] + '****' + key[-4:] if len(key) > 10 else '****'
            # 脱敏聊天平台 token
            for p in cfg.get('chat_platforms', []):
                if p.get('token'):
                    t = p['token']
                    p['token'] = t[:6] + '****' + t[-4:] if len(t) > 10 else '****'
                if p.get('app_secret'):
                    s = p['app_secret']
                    p['app_secret'] = s[:6] + '****' + s[-4:] if len(s) > 10 else '****'

        result = {
            '_meta': {
                'version': '1.0',
                'exported_at': __import__('datetime').datetime.now().isoformat(),
                'hostname': platform.node(),
                'include_secrets': include_secrets,
 },
            'config': cfg,
        }
        return result

    def import_config(self, data: dict, overwrite: bool = False) -> dict:
        """
        导入配置。

        Args:
            data: 导入的配置字典（含 _meta 和 config）
            overwrite: 是否完全覆盖（否则合并）

        Returns:
            dict: {ok, message, changed_keys}
        """
        if isinstance(data, dict) and 'config' in data:
            cfg_data = data['config']
        elif isinstance(data, dict):
            cfg_data = data
        else:
            return {'ok': False, 'message': '无效的配置格式'}

        # 验证基本结构
        if not isinstance(cfg_data, dict):
            return {'ok': False, 'message': '配置数据必须是字典'}

        changed = []
        if overwrite:
            # 完全覆盖
            self._config = AppConfig()
            self._apply_dict(self._config, cfg_data)
            changed = list(cfg_data.keys())
        else:
            # 合并模式：只覆盖有意义的字段
            for section, fields in cfg_data.items():
                if section.startswith('_'):
                    continue
                current = getattr(self._config, section, None)
                if current is None or not hasattr(current, '__dataclass_fields__'):
                    continue
                if isinstance(fields, dict):
                    for k, v in fields.items():
                        if hasattr(current, k):
                            old_val = getattr(current, k)
                            if old_val != v:
                                setattr(current, k, v)
                                changed.append(f"{section}.{k}")

        # 保存并应用默认值
        self._apply_defaults()
        self.save()
        return {
            'ok': True,
            'message': f'配置已导入（{len(changed)} 项变更）',
            'changed_keys': changed,
        }

    def get_full_config(self) -> dict:
        """获取完整配置字典（敏感字段脱敏）"""
        return self.export_config(include_secrets=False)


# ==============================================================================
# 全局配置实例
# ==============================================================================

_global_config: Optional[ConfigManager] = None


def get_config() -> ConfigManager:
    """获取全局配置管理器实例"""
    global _global_config
    if _global_config is None:
        _global_config = ConfigManager()
        _global_config.load()
    return _global_config


def reset_config():
    """重置全局配置(测试用)"""
    global _global_config
    _global_config = None
