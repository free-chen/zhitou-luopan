# 智投罗盘 · 容器镜像（预留云服务器部署）
# 本地：  docker build -t zhitou-luopan . && docker run -p 8710:8710 -e ARK_API_KEY=xxx zhitou-luopan
# 云端：  通过环境变量注入 ARK_API_KEY，镜像不含任何密钥文件
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOST=0.0.0.0 \
    PORT=8710

WORKDIR /app

# 仅依赖标准库，无需安装第三方包
COPY server ./server
COPY web ./web

EXPOSE 8710

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8710/api/health',timeout=4)"

CMD ["python", "-m", "server.main"]
