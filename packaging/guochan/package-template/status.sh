#!/usr/bin/env bash
# 查看服务状态与最近日志
set -Eeuo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/scripts/lib.sh"
"${COMPOSE[@]}" ps
echo
"${COMPOSE[@]}" logs --tail 30
