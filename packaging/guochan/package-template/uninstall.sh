#!/usr/bin/env bash
# 卸载演示服务。默认保留数据；加 --purge 同时删除数据、模型与镜像（不可恢复）。
set -Eeuo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/scripts/lib.sh"
remove_desktop() {
  if [[ -d /usr/local/share/llmdemo ]]; then
    ./setup-desktop.sh --remove ${SUDO_USER:+--user "${SUDO_USER}"} 2>/dev/null \
      || rm -rf /usr/local/share/llmdemo /usr/share/applications/llmdemo.desktop
  fi
}
if [[ "${1:-}" == "--purge" ]]; then
  read -r -p "将永久删除所有演示数据、模型和镜像，输入 yes 确认：" answer
  [[ "${answer}" == "yes" ]] || { echo "已取消"; exit 1; }
  "${COMPOSE[@]}" down --volumes
  docker image rm llmdemo/backend:1.0.0 llmdemo/frontend:1.0.0 llmdemo/ollama-cpu:0.32.1 || true
  rm -f .env
  remove_desktop
  echo "已完全卸载。"
else
  "${COMPOSE[@]}" down
  remove_desktop
  echo "服务已移除，数据卷保留。如需彻底删除请运行 ./uninstall.sh --purge"
fi
