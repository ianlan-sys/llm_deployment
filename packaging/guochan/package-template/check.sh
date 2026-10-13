#!/usr/bin/env bash
# 环境体检：安装前后都可以运行，只读取信息，不修改系统。
# 用法：sudo ./check.sh [--port 8080]
# 结果同时保存到当前目录的 llmdemo-check-<时间>.txt，便于带出现场排查（不含口令和密钥）。
set -uo pipefail

PKG_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${PKG_DIR}"

PORT=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --port) PORT="${2:?--port 需要端口号}"; shift 2 ;;
    -h|--help) sed -n '2,4p' "$0"; exit 0 ;;
    *) echo "未知参数：$1" >&2; exit 1 ;;
  esac
done
if [[ -z "${PORT}" ]]; then
  PORT="$(sed -n 's/^LLMDEMO_HTTP_PORT=//p' .env 2>/dev/null)"
fi
PORT="${PORT:-8080}"

source scripts/preflight.sh

REPORT="llmdemo-check-$(date +%Y%m%d-%H%M%S).txt"
{
  echo "本地大模型演示 · 环境体检  $(date '+%Y-%m-%d %H:%M:%S')"
  [[ -f VERSION ]] && sed 's/^/  /' VERSION
  echo
  echo "一、运行环境"
  preflight_report "${PORT}"
  warnings=$?

  echo
  echo "二、服务状态"
  if docker ps --format '{{.Names}}' 2>/dev/null | grep -q '^llmdemo-'; then
    docker ps --all --filter name=llmdemo- --format '  {{.Names}}\t{{.Status}}'
    if curl -fsS -o /dev/null --max-time 5 "http://127.0.0.1:${PORT}/api/health" 2>/dev/null; then
      echo "  [ 正常 ] http://127.0.0.1:${PORT}/api/health 可访问"
    else
      echo "  [ 注意 ] http://127.0.0.1:${PORT}/api/health 无法访问，运行 ./status.sh 查看日志"
      warnings=$((warnings + 1))
    fi
  else
    echo "  [ 信息 ] 尚未安装或服务未启动"
  fi

  echo
  if (( warnings == 0 )); then
    echo "结论：未发现需要处理的问题。"
  else
    echo "结论：有 ${warnings} 项需要注意，见上方 [ 注意 ] 行。"
  fi
} 2>&1 | tee "${REPORT}"
echo "体检结果已保存到 ${PKG_DIR}/${REPORT}"
