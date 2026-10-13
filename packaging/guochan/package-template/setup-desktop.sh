#!/usr/bin/env bash
# 一体机桌面集成（银河麒麟 / 统信 UOS 桌面版）：桌面图标、开始菜单、登录后自动全屏打开 Demo、演示样例文件。
# 用法：sudo ./setup-desktop.sh [--user 桌面用户名] [--no-autostart] [--remove]
set -Eeuo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/scripts/lib.sh"

DESKTOP_USER="${SUDO_USER:-}"
AUTOSTART=1
REMOVE=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --user) DESKTOP_USER="${2:?--user 需要用户名}"; shift 2 ;;
    --no-autostart) AUTOSTART=0; shift ;;
    --remove) REMOVE=1; shift ;;
    -h|--help) sed -n '2,3p' "$0"; exit 0 ;;
    *) echo "未知参数：$1" >&2; exit 1 ;;
  esac
done

[[ "${EUID}" -eq 0 ]] || { echo "请使用 sudo 运行" >&2; exit 1; }
if [[ -z "${DESKTOP_USER}" || "${DESKTOP_USER}" == root ]]; then
  echo "请用桌面登录用户执行 sudo ./setup-desktop.sh，或加 --user <桌面用户名>" >&2
  exit 1
fi
USER_HOME="$(getent passwd "${DESKTOP_USER}" | cut -d: -f6)"
[[ -n "${USER_HOME}" && -d "${USER_HOME}" ]] || { echo "找不到用户 ${DESKTOP_USER} 的主目录" >&2; exit 1; }
USER_GROUP="$(id -gn "${DESKTOP_USER}")"

as_user() { runuser -u "${DESKTOP_USER}" -- "$@"; }

# 桌面目录：中文系统一般是 ~/桌面
DESKTOP_DIR="$(as_user env HOME="${USER_HOME}" xdg-user-dir DESKTOP 2>/dev/null || true)"
if [[ -z "${DESKTOP_DIR}" || "${DESKTOP_DIR}" == "${USER_HOME}" || ! -d "${DESKTOP_DIR}" ]]; then
  for d in "${USER_HOME}/桌面" "${USER_HOME}/Desktop"; do
    [[ -d "${d}" ]] && { DESKTOP_DIR="${d}"; break; }
  done
fi
DESKTOP_DIR="${DESKTOP_DIR:-${USER_HOME}/Desktop}"

SHARE=/usr/local/share/llmdemo
MENU_FILE=/usr/share/applications/llmdemo.desktop
ICON_FILE=/usr/share/icons/hicolor/scalable/apps/llmdemo.svg
AUTOSTART_FILE="${USER_HOME}/.config/autostart/llmdemo.desktop"
DESKTOP_FILE="${DESKTOP_DIR}/llmdemo.desktop"
SAMPLES_DIR="${DESKTOP_DIR}/演示样例"

if [[ "${REMOVE}" -eq 1 ]]; then
  rm -rf "${SHARE}" "${MENU_FILE}" "${ICON_FILE}" "${AUTOSTART_FILE}" "${DESKTOP_FILE}"
  echo "已移除桌面图标、菜单和自动启动（${SAMPLES_DIR} 保留，可手动删除）。"
  exit 0
fi

PORT="$(sed -n 's/^LLMDEMO_HTTP_PORT=//p' .env 2>/dev/null)"
URL="http://127.0.0.1:${PORT:-8080}"

install -D -m 755 desktop/open-demo.sh "${SHARE}/open-demo.sh"
install -D -m 644 desktop/llmdemo.svg "${ICON_FILE}"
command -v gtk-update-icon-cache >/dev/null 2>&1 && gtk-update-icon-cache -q /usr/share/icons/hicolor 2>/dev/null || true

entry() {
  cat <<EOF
[Desktop Entry]
Type=Application
Version=1.0
Name=本地大模型演示
Name[zh_CN]=本地大模型演示
Comment=全屏打开本地化大模型应用平台 Demo（F11 退出全屏，Alt+F4 关闭）
Exec=${SHARE}/open-demo.sh ${URL}
Icon=llmdemo
Terminal=false
Categories=Office;
StartupNotify=true
EOF
}

install -d /usr/share/applications
entry > "${MENU_FILE}"
chmod 644 "${MENU_FILE}"

install -d -o "${DESKTOP_USER}" -g "${USER_GROUP}" "${DESKTOP_DIR}"
entry > "${DESKTOP_FILE}"
chown "${DESKTOP_USER}:${USER_GROUP}" "${DESKTOP_FILE}"
chmod 755 "${DESKTOP_FILE}"   # 麒麟/UOS 文件管理器要求桌面快捷方式可执行才视为可信
as_user gio set "${DESKTOP_FILE}" metadata::trusted true 2>/dev/null || true

if [[ "${AUTOSTART}" -eq 1 ]]; then
  install -d -o "${DESKTOP_USER}" -g "${USER_GROUP}" "${USER_HOME}/.config" "${USER_HOME}/.config/autostart"
  { entry; echo "X-GNOME-Autostart-enabled=true"; } > "${AUTOSTART_FILE}"
  chown "${DESKTOP_USER}:${USER_GROUP}" "${AUTOSTART_FILE}"
else
  rm -f "${AUTOSTART_FILE}"
fi

install -d -o "${DESKTOP_USER}" -g "${USER_GROUP}" "${SAMPLES_DIR}"
install -m 644 -o "${DESKTOP_USER}" -g "${USER_GROUP}" samples/* "${SAMPLES_DIR}/"

echo "桌面集成完成（用户 ${DESKTOP_USER}）："
echo "  桌面图标：${DESKTOP_FILE}"
echo "  演示样例：${SAMPLES_DIR}"
if [[ "${AUTOSTART}" -eq 1 ]]; then
  echo "  登录桌面后自动全屏打开 ${URL}"
else
  echo "  未设置自动启动；双击桌面“本地大模型演示”图标打开"
fi
