# shellcheck shell=bash
# 环境体检：只读取系统信息，不做任何修改。由 check.sh 和 install.sh source 使用。
# preflight_report <端口>：逐项输出检查结果，返回值为“需要处理”的项目数。

_pf_ok()   { printf '  [ 正常 ] %s\n' "$*"; }
_pf_warn() { printf '  [ 注意 ] %s\n' "$*"; PF_WARNINGS=$((PF_WARNINGS + 1)); }
_pf_info() { printf '  [ 信息 ] %s\n' "$*"; }

preflight_report() {
  local port="${1:-8080}"
  local pkg_dir="${PKG_DIR:-.}"
  PF_WARNINGS=0

  # 操作系统与内核
  local os_name
  os_name="$(. /etc/os-release 2>/dev/null && echo "${PRETTY_NAME:-}")"
  _pf_info "操作系统：${os_name:-未知}"
  [[ -r /etc/kylin-release ]] && _pf_info "麒麟版本：$(head -1 /etc/kylin-release 2>/dev/null)"
  [[ -r /etc/.kyinfo ]] && _pf_info "麒麟构建：$(grep -m1 -i '^dist_id' /etc/.kyinfo 2>/dev/null | cut -d= -f2-)"
  _pf_info "内核：$(uname -r)，架构：$(uname -m)"

  # CPU
  local cpu_model cores
  cpu_model="$(LC_ALL=C lscpu 2>/dev/null | sed -n 's/^Model name:[[:space:]]*//p' | head -1)"
  cores="$(nproc 2>/dev/null || echo 0)"
  _pf_info "CPU：${cpu_model:-未知}"
  if (( cores >= 4 )); then
    _pf_ok "CPU 核数：${cores}"
  else
    _pf_warn "CPU 核数：${cores}（建议至少 4 核，本地模型回答会很慢）"
  fi

  # 内存页大小：部分麒麟 ARM64 内核使用 64K 页，本安装包尚未在该配置上实测
  local page
  page="$(getconf PAGESIZE 2>/dev/null || echo 0)"
  if [[ "${page}" == 4096 ]]; then
    _pf_ok "内存页大小：4K"
  else
    _pf_warn "内存页大小：$((page / 1024))K（非 4K 页内核未经实测；若容器启动即退出，请记录 ./status.sh 的日志）"
  fi

  # 内存
  local mem_kb mem_gb
  mem_kb="$(sed -n 's/^MemTotal:[[:space:]]*\([0-9]*\).*/\1/p' /proc/meminfo 2>/dev/null)"
  mem_gb=$(( (${mem_kb:-0} + 524288) / 1048576 ))
  if (( ${mem_kb:-0} >= 7 * 1048576 )); then
    _pf_ok "内存：约 ${mem_gb} GB"
  else
    _pf_warn "内存：约 ${mem_gb} GB（要求 8 GB；不足时把知识库切到 Mock 模式）"
  fi

  # 磁盘：Docker 数据目录需要放镜像和模型
  local docker_root free_kb free_gb
  docker_root="$(docker info --format '{{.DockerRootDir}}' 2>/dev/null || true)"
  [[ -d "${docker_root}" ]] || docker_root=/var/lib
  free_kb="$(df -Pk "${docker_root}" 2>/dev/null | awk 'NR==2 {print $4}')"
  free_gb=$(( ${free_kb:-0} / 1048576 ))
  if (( ${free_kb:-0} >= 10 * 1048576 )); then
    _pf_ok "磁盘（${docker_root}）：可用 ${free_gb} GB"
  else
    _pf_warn "磁盘（${docker_root}）：可用 ${free_gb} GB（建议至少 10 GB，否则导入镜像和模型可能失败）"
  fi

  # systemd：离线 Docker 以 systemd 服务方式安装
  if command -v systemctl >/dev/null 2>&1 && [[ -d /run/systemd/system ]]; then
    _pf_ok "systemd：可用"
  else
    _pf_warn "未检测到 systemd，--install-docker 无法注册 Docker 服务"
  fi

  # Docker
  if command -v docker >/dev/null 2>&1; then
    local docker_version
    docker_version="$(docker version --format '{{.Server.Version}}' 2>/dev/null || true)"
    if [[ -z "${docker_version}" ]]; then
      _pf_warn "Docker 已安装但服务未运行（systemctl start docker）"
    elif [[ "${docker_version%%.*}" =~ ^[0-9]+$ ]] && (( ${docker_version%%.*} < 20 )); then
      _pf_warn "Docker ${docker_version} 版本较旧，建议卸载后用 --install-docker 安装包内的 Docker 27"
    else
      _pf_ok "Docker：${docker_version}"
    fi
  else
    _pf_info "Docker：未安装（安装时加 --install-docker 使用包内离线 Docker）"
  fi
  if [[ -f /sys/fs/cgroup/cgroup.controllers ]]; then
    _pf_info "cgroup：v2"
  else
    _pf_info "cgroup：v1"
  fi
  if ! command -v iptables >/dev/null 2>&1 && ! command -v nft >/dev/null 2>&1; then
    _pf_warn "未找到 iptables / nft，Docker 网络可能无法创建"
  fi

  # 麒麟安全机制 KySec：执行控制开启时可能拦截安装包内的脚本和 Docker 二进制
  if command -v getstatus >/dev/null 2>&1; then
    local kysec
    kysec="$(getstatus 2>/dev/null | tr '\n' ';' | sed 's/;$//')"
    if grep -qiE 'status:[[:space:]]*(enabled|enforcing)' <<<"${kysec}"; then
      _pf_warn "KySec 处于强制模式（${kysec}）。若脚本或 Docker 被拦截，见 README“故障排查”"
    else
      _pf_info "KySec：${kysec:-未知}"
    fi
  fi
  if command -v getenforce >/dev/null 2>&1; then
    local selinux
    selinux="$(getenforce 2>/dev/null || true)"
    if [[ "${selinux}" == Enforcing ]]; then
      _pf_warn "SELinux 为 Enforcing；若容器无法读取数据卷，见 README“故障排查”"
    else
      _pf_info "SELinux：${selinux:-未知}"
    fi
  fi

  # 端口：已被本 Demo 占用属于正常情况
  local listener=""
  if command -v ss >/dev/null 2>&1; then
    listener="$(ss -Hltn "sport = :${port}" 2>/dev/null | head -1)"
  fi
  if [[ -z "${listener}" ]]; then
    _pf_ok "端口 ${port}：空闲"
  elif docker ps --format '{{.Names}}' 2>/dev/null | grep -q '^llmdemo-frontend'; then
    _pf_ok "端口 ${port}：已由本 Demo 使用"
  else
    _pf_warn "端口 ${port} 已被其他程序占用，安装时请加 --port 换一个端口"
  fi

  # 防火墙：只影响其他电脑访问
  if command -v firewall-cmd >/dev/null 2>&1 && firewall-cmd --state >/dev/null 2>&1; then
    _pf_info "firewalld：运行中（其他电脑访问需放行端口 ${port}；一体机本机演示不受影响）"
  fi

  # 浏览器：一体机演示用
  local browser found=""
  for browser in qaxbrowser-safe-stable qaxbrowser-safe browser360-cn-stable uos-browser \
                 chromium chromium-browser google-chrome-stable microsoft-edge-stable firefox; do
    if command -v "${browser}" >/dev/null 2>&1; then found="${browser}"; break; fi
  done
  if [[ -n "${found}" ]]; then
    _pf_ok "浏览器：${found}"
  else
    _pf_info "浏览器：未找到已知浏览器（服务器版属正常；一体机演示需要桌面浏览器）"
  fi

  # 安装包自身
  if [[ -f "${pkg_dir}/ARCH" ]]; then
    local pkg_arch host_arch
    pkg_arch="$(cat "${pkg_dir}/ARCH")"
    case "$(uname -m)" in
      x86_64|amd64) host_arch=amd64 ;;
      aarch64|arm64) host_arch=arm64 ;;
      *) host_arch="$(uname -m)" ;;
    esac
    if [[ "${pkg_arch}" == "${host_arch}" ]]; then
      _pf_ok "安装包架构：${pkg_arch}，与本机一致"
    else
      _pf_warn "安装包架构为 ${pkg_arch}，本机为 ${host_arch}，请换用 ${host_arch} 安装包"
    fi
  fi

  return "${PF_WARNINGS}"
}
