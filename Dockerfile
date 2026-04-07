# MyAgent - 本地桌面端执行型 AI 助手
# Docker 构建文件 (主要用于服务器部署)
FROM python:3.11-slim

WORKDIR /app

# 安装系统依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3-pip \
    && rm -rf /var/lib/apt/lists/*

# 安装 Python 依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制代码
COPY . .

# 创建数据目录
RUN mkdir -p data logs

# 环境变量
ENV MYAGENT_APP_DATA_DIR=data
ENV MYAGENT_APP_LOG_FILE=logs/myagent.log

# HTTP API 模式
EXPOSE 8080

# 启动
CMD ["python", "main.py", "--server", "--host", "0.0.0.0", "--port", "8080"]
