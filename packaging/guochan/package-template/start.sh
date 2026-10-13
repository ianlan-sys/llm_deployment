#!/usr/bin/env bash
# 启动演示服务
set -Eeuo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/scripts/lib.sh"
[[ -f .env ]] || { echo "未找到 .env，请先运行 ./install.sh" >&2; exit 1; }
"${COMPOSE[@]}" up -d --wait
# 前端 nginx 会缓存后端容器 IP，后端重建后需重启前端，否则出现 502
"${COMPOSE[@]}" restart frontend >/dev/null
source "${PKG_DIR}/scripts/warmup.sh"
echo "预加载演示模型 ……"
warmup_model || echo "模型预加载未完成，首次提问可能较慢" >&2
"${COMPOSE[@]}" ps
