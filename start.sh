#!/bin/bash
# MyAgent Unix/macOS 启动脚本

echo "========================================"
echo "  MyAgent - 本地桌面端执行型 AI 助手"
echo "========================================"
echo ""

# 检查 Python
if ! command -v python3 &> /dev/null; then
    echo "[错误] 未找到 Python3，请先安装 Python 3.8+"
    exit 1
fi

# 检查依赖
if ! python3 -c "import yaml" &> /dev/null; then
    echo "[提示] 正在安装依赖..."
    pip3 install -r requirements.txt
fi

# 检查配置
if [ ! -f config.yaml ]; then
    echo "[提示] 未找到 config.yaml，使用默认配置"
    echo "[提示] 请编辑 config.yaml 填入你的 API Key"
fi

echo ""
echo "选择运行模式:"
echo "  1. CLI 交互模式"
echo "  2. 系统托盘模式"
echo "  3. HTTP API 模式"
echo ""
read -p "请输入 (1/2/3): " mode

case $mode in
    1) python3 main.py ;;
    2) python3 main.py --tray ;;
    3) python3 main.py --server --port 8080 ;;
    *) echo "无效选择" ;;
esac
