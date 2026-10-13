#!/usr/bin/env bash
# 停止演示服务（保留数据与模型）
set -Eeuo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/scripts/lib.sh"
"${COMPOSE[@]}" down
echo "已停止。数据与模型已保留，运行 ./start.sh 可再次启动。"
