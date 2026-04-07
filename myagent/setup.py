"""
setup.py - MyAgent 打包配置
支持 pip install 和 PyInstaller 打包
"""
from setuptools import setup, find_packages

setup(
    name="myagent",
    version="1.0.0",
    description="本地桌面端执行型AI助手 - Open Interpreter 风格",
    author="MyAgent",
    python_requires=">=3.10",
    packages=find_packages(exclude=["logs", "data", "*.pyc"]),
    install_requires=[
        "openai>=1.12.0",
        "aiohttp>=3.9.0",
        "duckduckgo-search>=6.0.0",
        "beautifulsoup4>=4.12.0",
        "psutil>=5.9.0",
        "pystray>=0.19.5",
        "Pillow>=10.0.0",
    ],
    extras_require={
        "telegram": ["python-telegram-bot>=21.0"],
        "discord": ["discord.py>=2.3.0"],
        "browser": ["playwright>=1.41.0"],
        "anthropic": ["anthropic>=0.18.0"],
        "all": [
            "python-telegram-bot>=21.0",
            "discord.py>=2.3.0",
            "playwright>=1.41.0",
            "anthropic>=0.18.0",
        ],
    },
    entry_points={
        "console_scripts": [
            "myagent=main:main",
        ],
    },
)
