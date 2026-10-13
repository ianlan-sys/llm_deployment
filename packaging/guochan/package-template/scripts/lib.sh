# shellcheck shell=bash
# 公共函数：定位 Compose 命令。由各管理脚本 source 使用。
PKG_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${PKG_DIR}"
if docker compose version >/dev/null 2>&1; then
  COMPOSE=(docker compose)
else
  COMPOSE=("${PKG_DIR}/bin/docker-compose")
fi
COMPOSE+=(-p llmdemo -f "${PKG_DIR}/docker-compose.yml")
