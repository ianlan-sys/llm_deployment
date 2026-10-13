#!/usr/bin/env bash
# 一体机演示：等待服务就绪后，用本机浏览器全屏打开 Demo。以桌面登录用户身份运行（不要用 sudo）。
# 用法：open-demo.sh [URL]   默认 http://127.0.0.1:8080
# 指定浏览器：LLMDEMO_BROWSER=/usr/bin/firefox open-demo.sh
set -uo pipefail

URL="${1:-http://127.0.0.1:8080}"
STATE_DIR="${XDG_DATA_HOME:-${HOME}/.local/share}/llmdemo"
mkdir -p "${STATE_DIR}"

notify() {
  command -v notify-send >/dev/null 2>&1 && notify-send -i llmdemo "本地大模型演示" "$1" 2>/dev/null
  echo "$1"
}

healthy() {
  if command -v curl >/dev/null 2>&1; then
    curl -fsS -o /dev/null --max-time 3 "${URL}/api/health"
  elif command -v wget >/dev/null 2>&1; then
    wget -q -O /dev/null -T 3 "${URL}/api/health"
  else
    python3 -c "import sys,urllib.request; urllib.request.urlopen(sys.argv[1], timeout=3)" "${URL}/api/health"
  fi
} >/dev/null 2>&1

# 开机后 Docker 拉起服务需要一点时间，最多等 3 分钟
if ! healthy; then
  notify "演示服务正在启动，请稍候……"
  for _ in $(seq 1 90); do
    healthy && break
    sleep 2
  done
  healthy || { notify "演示服务未就绪。请在终端运行：sudo <安装目录>/status.sh"; exit 1; }
fi

# Chromium 内核浏览器（麒麟默认的奇安信可信浏览器、统信 UOS 浏览器、360 等）：
# 独立配置目录 + 应用窗口模式 + 全屏，不影响用户日常浏览器的标签和历史。
open_chromium() {
  exec "$1" --user-data-dir="${STATE_DIR}/browser-profile" \
    --no-first-run --no-default-browser-check --password-store=basic \
    --disable-translate --start-fullscreen --app="${URL}"
}
open_firefox() {
  mkdir -p "${STATE_DIR}/firefox-profile"
  exec "$1" --new-instance --profile "${STATE_DIR}/firefox-profile" --kiosk "${URL}"
}

if [[ -n "${LLMDEMO_BROWSER:-}" ]]; then
  case "${LLMDEMO_BROWSER##*/}" in
    firefox*) open_firefox "${LLMDEMO_BROWSER}" ;;
    *) open_chromium "${LLMDEMO_BROWSER}" ;;
  esac
fi

for b in qaxbrowser-safe-stable qaxbrowser-safe browser360-cn-stable uos-browser \
         chromium chromium-browser google-chrome-stable microsoft-edge-stable browser; do
  command -v "${b}" >/dev/null 2>&1 && open_chromium "$(command -v "${b}")"
done
command -v firefox >/dev/null 2>&1 && open_firefox "$(command -v firefox)"

notify "未找到可用浏览器，改用系统默认浏览器打开（按 F11 全屏）"
exec xdg-open "${URL}"
