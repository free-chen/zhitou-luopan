#!/usr/bin/env bash
# 智投罗盘 · 本地启动脚本
set -euo pipefail
cd "$(dirname "$0")"

export HOST="${HOST:-127.0.0.1}"
export PORT="${PORT:-8710}"

echo "启动智投罗盘：http://${HOST}:${PORT}"
exec python3 -m server.main
