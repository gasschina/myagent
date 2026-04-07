"""
配置管理模块 - 集中管理所有配置项
支持 YAML 配置文件 + 环境变量 + 默认值三级覆盖
"""
import os
import json
import platform
import yaml
from pathlib import Path
from typing import Any, Dict, Optional
from dataclasses import dataclass, field


# ============================================================
# 默认配置
# ============================================================
DEFAULTS = {
    "app": {
        "name": "MyAgent",
        "version": "1.0.0",
        "log_level": "INFO",
        "log_file": "logs/myagent.log",
        "data_dir": "data",
        "max_log_size_mb": 50,
        "log_backup_count": 5,
    },
    "llm": {
        "provider": "openai",          # openai / zhipu / custom
        "api_key": "",
        "api_base": "https://api.openai.com/v1",
        "model": "gpt-4o",
        "temperature": 0.7,
        "max_tokens": 4096,
        "timeout": 120,
        "max_retries": 3,
        "retry_delay": 5,
    },
    "memory": {
        "db_path": "data/myagent.db",
        "max_short_term_messages": 50,
        "max_working_memory_items": 200,
        "auto_summarize_threshold": 40,
        "long_term_similarity_threshold": 0.7,
    },
    "executor": {
        "timeout": 300,               # 单次执行超时秒数
        "max_retries": 3,             # 最大重试次数
        "auto_fix_attempts": 2,       # 自动修复尝试次数
        "work_dir": None,             # 默认工作目录 (None=当前目录)
        "allowed_commands": [],       # 空列表=允许所有
        "blocked_commands": [
            "rm -rf /", "del /f /s /q C:\\", "format", "mkfs",
            "shutdown", "reboot", "halt", "poweroff",
        ],
        "python_path": "python3",
        "max_output_length": 50000,
    },
    "agent": {
        "max_plan_steps": 20,
        "max_tool_calls_per_step": 5,
        "execution_loop_max": 50,
        "thinking_budget": 3,          # 每个 plan step 最多思考次数
    },
    "chatbot": {
        "telegram": {
            "enabled": False,
            "bot_token": "",
            "allowed_users": [],
            "webhook_url": "",
            "poll_interval": 1.0,
        },
        "discord": {
            "enabled": False,
            "bot_token": "",
            "allowed_channels": [],
            "allowed_users": [],
        },
        "feishu": {
            "enabled": False,
            "app_id": "",
            "app_secret": "",
            "verification_token": "",
            "encrypt_key": "",
        },
        "qq": {
            "enabled": False,
            "bot_appid": "",
            "bot_token": "",
            "sandbox": True,
        },
        "wechat": {
            "enabled": False,
            # 微信通过 HTTP API 接入 (如 wechaty)
            "api_url": "",
            "api_token": "",
            "allowed_users": [],
        },
    },
    "skills": {
        "enabled": True,
        "search_engine": "duckduckgo",
        "browser_headless": True,
        "max_file_size_mb": 100,
    },
    "tray": {
        "auto_start": False,
        "show_notifications": True,
        "icon_path": "",
    },
}


@dataclass
class Config:
    """配置对象，支持属性访问和环境变量覆盖"""

    _data: Dict[str, Any] = field(default_factory=lambda: dict(DEFAULTS))
    _config_path: Optional[str] = None

    def __getitem__(self, key: str) -> Any:
        """支持 config['llm.api_key'] 风格访问"""
        keys = key.split('.')
        val = self._data
        for k in keys:
            if isinstance(val, dict):
                val = val.get(k)
            else:
                raise KeyError(f"Config key '{key}' not found at '{k}'")
            if val is None:
                raise KeyError(f"Config key '{key}' not found")
        return val

    def get(self, key: str, default: Any = None) -> Any:
        try:
            return self[key]
        except (KeyError, TypeError):
            return default

    def set(self, key: str, value: Any) -> None:
        """设置配置值，支持点号路径"""
        keys = key.split('.')
        d = self._data
        for k in keys[:-1]:
            if k not in d:
                d[k] = {}
            d = d[k]
        d[keys[-1]] = value

    @property
    def data(self) -> Dict[str, Any]:
        return self._data

    def save(self, path: Optional[str] = None) -> None:
        """保存配置到 YAML 文件"""
        save_path = path or self._config_path
        if not save_path:
            raise ValueError("No config path specified")
        Path(save_path).parent.mkdir(parents=True, exist_ok=True)
        with open(save_path, 'w', encoding='utf-8') as f:
            yaml.dump(self._data, f, default_flow_style=False, allow_unicode=True)

    def to_dict(self) -> Dict[str, Any]:
        return dict(self._data)


def _deep_merge(base: dict, override: dict) -> dict:
    """深度合并两个字典"""
    result = dict(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _apply_env_overrides(config: Config) -> None:
    """从环境变量覆盖配置 (MYAGENT_SECTION_KEY)"""
    prefix = "MYAGENT_"
    for key, value in os.environ.items():
        if key.startswith(prefix):
            config_key = key[len(prefix):].lower()
            # MYAGENT_LLM_API_KEY -> llm.api_key
            parts = config_key.split('_')
            # 简单启发式: 常见缩写保持原样
            if len(parts) >= 2:
                config_key = parts[0]
                for p in parts[1:]:
                    config_key += '.' + p
            config.set(config_key, value)


def load_config(config_path: Optional[str] = None) -> Config:
    """
    加载配置:
    1. 默认配置
    2. YAML 配置文件覆盖
    3. 环境变量覆盖
    """
    cfg = Config()

    # 查找配置文件
    search_paths = []
    if config_path:
        search_paths.append(config_path)
    search_paths.extend([
        Path.cwd() / "config.yaml",
        Path.home() / ".myagent" / "config.yaml",
        Path(__file__).parent / "config.yaml",
    ])

    for path in search_paths:
        p = Path(path)
        if p.exists():
            with open(p, 'r', encoding='utf-8') as f:
                user_config = yaml.safe_load(f) or {}
            cfg._data = _deep_merge(cfg._data, user_config)
            cfg._config_path = str(p)
            break

    # 环境变量覆盖
    _apply_env_overrides(cfg)

    # 确保数据目录存在
    data_dir = cfg.get("app.data_dir", "data")
    Path(data_dir).mkdir(parents=True, exist_ok=True)

    # 确保日志目录存在
    log_file = cfg.get("app.log_file", "logs/myagent.log")
    Path(log_file).parent.mkdir(parents=True, exist_ok=True)

    return cfg


# 全局配置单例
_global_config: Optional[Config] = None


def get_config() -> Config:
    """获取全局配置"""
    global _global_config
    if _global_config is None:
        _global_config = load_config()
    return _global_config


def init_config(config_path: Optional[str] = None) -> Config:
    """初始化全局配置"""
    global _global_config
    _global_config = load_config(config_path)
    return _global_config
