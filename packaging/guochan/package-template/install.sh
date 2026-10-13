#!/usr/bin/env bash
# 本地化大模型应用平台 Demo —— 国产 OS 离线安装脚本
# 用法：./install.sh [--port 8080] [--bind 0.0.0.0] [--username demo-operator] [--install-docker] [--desktop]
#       --desktop：一体机演示，安装桌面图标并在登录后自动全屏打开 Demo（麒麟 / UOS 桌面版）
set -Eeuo pipefail

PKG_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${PKG_DIR}"

PORT=""
BIND_ADDR=""
USERNAME="demo-operator"
INSTALL_DOCKER=0
DESKTOP=0
PROJECT="llmdemo"

log()  { printf '\033[1;32m[安装]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[注意]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[失败]\033[0m %s\n' "$*" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --port) PORT="${2:?--port 需要端口号}"; shift 2 ;;
    --bind) BIND_ADDR="${2:?--bind 需要监听地址}"; shift 2 ;;
    --username) USERNAME="${2:?--username 需要用户名}"; shift 2 ;;
    --install-docker) INSTALL_DOCKER=1; shift ;;
    --desktop) DESKTOP=1; shift ;;
    -h|--help) sed -n '2,4p' "$0"; exit 0 ;;
    *) die "未知参数：$1" ;;
  esac
done

[[ "${EUID}" -eq 0 ]] || die "请使用 root 运行（例如 sudo ./install.sh）"
[[ -z "${PORT}" || "${PORT}" =~ ^[0-9]{1,5}$ ]] || die "端口无效：${PORT}"
[[ -z "${BIND_ADDR}" || "${BIND_ADDR}" =~ ^[0-9.]+$ ]] || die "监听地址无效：${BIND_ADDR}"
[[ "${USERNAME}" =~ ^[A-Za-z0-9._-]{1,64}$ ]] || die "用户名只能包含字母、数字和 . _ -"

# 1. 架构检查
PKG_ARCH="$(cat ARCH)"
case "$(uname -m)" in
  x86_64|amd64) HOST_ARCH=amd64 ;;
  aarch64|arm64) HOST_ARCH=arm64 ;;
  *) die "不支持的 CPU 架构：$(uname -m)" ;;
esac
[[ "${HOST_ARCH}" == "${PKG_ARCH}" ]] || \
  die "本安装包为 ${PKG_ARCH}，本机为 ${HOST_ARCH}；请改用 local-llm-demo-offline-${HOST_ARCH} 安装包"
log "系统：$(. /etc/os-release 2>/dev/null && echo "${PRETTY_NAME:-未知}")，架构 ${HOST_ARCH}"

# 环境体检（只提示，不中断安装；也可单独运行 ./check.sh）
source scripts/preflight.sh
log "环境体检："
preflight_report "${PORT:-8080}" || warn "体检有需要注意的项目（见上方 [ 注意 ] 行），安装继续"

# 2. 完整性校验
log "校验安装包完整性（SHA-256）……"
sha256sum --quiet -c SHA256SUMS || die "文件校验失败，安装包可能损坏或被篡改"

# 3. Docker
install_bundled_docker() {
  local tgz
  tgz="$(ls bin/docker-*.tgz 2>/dev/null | head -1)"
  [[ -n "${tgz}" ]] || die "安装包内未找到 Docker 离线二进制"
  log "安装离线 Docker：${tgz##*/}"
  tar -xzf "${tgz}" -C /usr/local/bin --strip-components=1
  getent group docker >/dev/null || groupadd --system docker
  cat > /etc/systemd/system/containerd.service <<'UNIT'
[Unit]
Description=containerd container runtime (llmdemo offline bundle)
After=network.target

[Service]
ExecStartPre=-/sbin/modprobe overlay
ExecStart=/usr/local/bin/containerd
Restart=always
Delegate=yes
KillMode=process
LimitNOFILE=infinity

[Install]
WantedBy=multi-user.target
UNIT
  cat > /etc/systemd/system/docker.service <<'UNIT'
[Unit]
Description=Docker Engine (llmdemo offline bundle)
After=network-online.target containerd.service
Requires=containerd.service

[Service]
Type=notify
ExecStart=/usr/local/bin/dockerd --containerd=/run/containerd/containerd.sock
Restart=always
LimitNOFILE=infinity
Delegate=yes
KillMode=process

[Install]
WantedBy=multi-user.target
UNIT
  systemctl daemon-reload
  systemctl enable --now containerd docker
}

if ! command -v docker >/dev/null 2>&1; then
  if [[ "${INSTALL_DOCKER}" -eq 1 ]]; then
    install_bundled_docker
  else
    die "未检测到 Docker。可用系统源安装（如 dnf install docker），或加参数 --install-docker 使用安装包自带的离线 Docker"
  fi
fi
if ! docker info >/dev/null 2>&1; then
  systemctl start docker 2>/dev/null || true
  sleep 2
  docker info >/dev/null 2>&1 || die "Docker 服务未运行，请执行 systemctl start docker 后重试"
fi
systemctl enable docker >/dev/null 2>&1 || warn "未能设置 Docker 开机自启，请手动执行 systemctl enable docker"
DOCKER_MAJOR="$(docker version --format '{{.Server.Version}}' 2>/dev/null | cut -d. -f1)"
if [[ "${DOCKER_MAJOR}" =~ ^[0-9]+$ ]] && (( DOCKER_MAJOR < 20 )); then
  warn "系统 Docker 版本较旧（$(docker version --format '{{.Server.Version}}')），若后续启动异常，可卸载系统 Docker 后使用 --install-docker"
fi

# 4. Docker Compose（优先系统插件，否则使用安装包自带版本）
if docker compose version >/dev/null 2>&1; then
  COMPOSE=(docker compose)
else
  install -D -m 755 bin/docker-compose /usr/local/lib/docker/cli-plugins/docker-compose
  if docker compose version >/dev/null 2>&1; then
    COMPOSE=(docker compose)
  else
    COMPOSE=("${PKG_DIR}/bin/docker-compose")
  fi
fi
COMPOSE+=(-p "${PROJECT}" -f "${PKG_DIR}/docker-compose.yml")
log "Compose：$("${COMPOSE[@]}" version --short 2>/dev/null || echo 已就绪)"

# 5. 导入镜像
for image_tar in images/*.tar; do
  log "导入镜像 ${image_tar##*/} ……"
  docker load -i "${image_tar}" >/dev/null
done

# 6. 生成 .env 与一次性登录口令（不覆盖已有配置）
NEW_PASSWORD=""
if [[ -f .env ]]; then
  log "检测到已有 .env，保留原登录凭据"
else
  log "生成登录凭据 ……"
  umask 077
  err_file="$(mktemp)"
  docker run --rm --network none -v "${PKG_DIR}:/pkg:ro" llmdemo/backend:1.0.0 \
    python /pkg/scripts/render_env.py /pkg/.env.example "${USERNAME}" > .env.tmp 2> "${err_file}" \
    || { rm -f .env.tmp "${err_file}"; die "凭据生成失败"; }
  NEW_PASSWORD="$(sed -n 's/^DEMO_AUTH_PASSWORD=//p' "${err_file}")"
  rm -f "${err_file}"
  mv .env.tmp .env
  chmod 600 .env
  umask 022
fi
if [[ -n "${PORT}" ]]; then
  sed -i "s/^LLMDEMO_HTTP_PORT=.*/LLMDEMO_HTTP_PORT=${PORT}/" .env
fi
if [[ -n "${BIND_ADDR}" ]]; then
  sed -i "s/^LLMDEMO_BIND_ADDR=.*/LLMDEMO_BIND_ADDR=${BIND_ADDR}/" .env
fi
HTTP_PORT="$(sed -n 's/^LLMDEMO_HTTP_PORT=//p' .env)"
HTTP_PORT="${HTTP_PORT:-8080}"

# 7. 预置演示模型（离线，无需联网拉取）
OLLAMA_VOLUME="${PROJECT}_ollama-data"
if ! docker volume inspect "${OLLAMA_VOLUME}" >/dev/null 2>&1; then
  docker volume create \
    --label com.docker.compose.project="${PROJECT}" \
    --label com.docker.compose.volume=ollama-data \
    "${OLLAMA_VOLUME}" >/dev/null
fi
if docker run --rm --network none -v "${OLLAMA_VOLUME}:/root/.ollama" --entrypoint test \
     llmdemo/ollama-cpu:0.32.1 -f /root/.ollama/models/manifests/registry.ollama.ai/library/qwen3/1.7b-q4_K_M; then
  log "演示模型 qwen3:1.7b 已存在"
else
  log "导入演示模型 qwen3:1.7b（约 1.3 GB）……"
  docker run --rm --network none -v "${OLLAMA_VOLUME}:/root/.ollama" -v "${PKG_DIR}/models:/in:ro" \
    --entrypoint tar llmdemo/ollama-cpu:0.32.1 -xf /in/ollama-models-qwen3-1.7b.tar -C /root/.ollama
fi

# 8. 启动
log "启动服务 ……"
"${COMPOSE[@]}" up -d
# 前端 nginx 会缓存后端容器 IP，后端重建后需重启前端，否则出现 502
"${COMPOSE[@]}" restart frontend >/dev/null

for _ in $(seq 1 60); do
  if curl -fsS -o /dev/null "http://127.0.0.1:${HTTP_PORT}/api/health" 2>/dev/null; then
    break
  fi
  sleep 2
done
curl -fsS -o /dev/null "http://127.0.0.1:${HTTP_PORT}/api/health" \
  || die "服务未在 2 分钟内就绪，请运行 ./status.sh 查看日志"

log "预加载演示模型（CPU 上约需 10–60 秒）……"
PKG_DIR="${PKG_DIR}" source scripts/warmup.sh
warmup_model || warn "模型预加载未完成，不影响使用；首次提问可能较慢"

echo
log "安装完成！"
echo "  访问地址："
LISTEN="$(sed -n 's/^LLMDEMO_BIND_ADDR=//p' .env)"
if [[ "${LISTEN:-0.0.0.0}" == "0.0.0.0" ]]; then IPS="$(hostname -I 2>/dev/null) 127.0.0.1"; else IPS="${LISTEN}"; fi
for ip in ${IPS}; do
  [[ "${ip}" == *:* ]] && continue
  echo "    http://${ip}:${HTTP_PORT}"
done
echo "  登录用户：$(sed -n "s/^DEMO_AUTH_USERNAME='\(.*\)'$/\1/p" .env)"
if [[ -n "${NEW_PASSWORD}" ]]; then
  echo "  登录口令：${NEW_PASSWORD}"
  warn "口令只显示这一次，未写入磁盘，请立即记录。忘记口令可运行 ./reset-password.sh"
fi
if [[ "${DESKTOP}" -eq 1 ]]; then
  echo
  "${PKG_DIR}/setup-desktop.sh" || warn "桌面集成未完成，可稍后运行 sudo ./setup-desktop.sh --user <桌面用户名>"
fi
if [[ "${LISTEN:-0.0.0.0}" == "0.0.0.0" ]] && command -v firewall-cmd >/dev/null 2>&1 && firewall-cmd --state >/dev/null 2>&1; then
  warn "检测到 firewalld 正在运行；如需其他电脑访问，请执行："
  echo "    firewall-cmd --permanent --add-port=${HTTP_PORT}/tcp && firewall-cmd --reload"
fi
