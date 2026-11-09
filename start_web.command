#!/bin/bash
# HireDesk — macOS 启动脚本
# 用法：双击运行，或 ./start_web.command [--port 8800] [--no-browser]
set -u
cd "$(dirname "$0")" || exit 1

export JOBSCRAPER_DIR="${JOBSCRAPER_DIR:-$PWD/jobscraper}"

PY=".venv/bin/python"
if [ ! -x "$PY" ]; then
  echo "[err] 缺 .venv。先执行："
  echo "  python3.12 -m venv .venv && .venv/bin/pip install pymysql cryptography"
  exit 1
fi

# MySQL（Homebrew）：未运行则尝试启动
if command -v /opt/homebrew/bin/mysqladmin >/dev/null 2>&1; then
  if ! /opt/homebrew/bin/mysqladmin --socket=/tmp/mysql.sock -uroot ping >/dev/null 2>&1; then
    echo "[info] MySQL 未运行，尝试 brew services start mysql…"
    /opt/homebrew/bin/brew services start mysql >/dev/null 2>&1 || true
    for _ in $(seq 1 40); do
      /opt/homebrew/bin/mysqladmin --socket=/tmp/mysql.sock -uroot ping >/dev/null 2>&1 && break
      sleep 1
    done
  fi
fi

exec "$PY" webapp/server.py "$@"
