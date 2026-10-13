#!/usr/bin/env bash
# 组装离线安装包：./build-package.sh amd64|arm64
# 前置：镜像已构建（amd64: llmdemo/*，arm64: llmdemo-arm64/*），downloads/ 与 common/ 已就绪。
# 详见同目录 README.md。
set -Eeuo pipefail
ARCH="${1:?amd64 或 arm64}"
B="$(cd "$(dirname "$0")" && pwd)"
# 源码目录：默认是本仓库根目录
SRC="${LLMDEMO_SRC:-$(cd "${B}/../.." && pwd)}"
case "${ARCH}" in
  amd64) BINARCH=x86_64; SRC_REPO=llmdemo ;;
  arm64) BINARCH=aarch64; SRC_REPO=llmdemo-arm64 ;;
  *) echo "unknown arch" >&2; exit 1 ;;
esac
NAME="local-llm-demo-offline-${ARCH}"
OUT="${B}/out/${NAME}"
# 本地镜像已被清理时，复用上一版安装包里导出的镜像 tar（只更新脚本与文档）
STASH="${B}/out/.images-${ARCH}"
mkdir -p "${STASH}"
mv "${OUT}"/images/*.tar "${STASH}/" 2>/dev/null || true
rm -rf "${OUT}"
mkdir -p "${OUT}"/{images,models,bin,samples}
cp -a "${B}/package-template/." "${OUT}/"
cp "${SRC}"/backend/app/data/review_samples/*.txt "${OUT}/samples/"
ln "${B}/common/ollama-models-qwen3-1.7b.tar" "${OUT}/models/"
cp "${B}/downloads/docker-27.5.1-${BINARCH}.tgz" "${OUT}/bin/"
install -m 755 "${B}/downloads/docker-compose-linux-${BINARCH}" "${OUT}/bin/docker-compose"
echo "${ARCH}" > "${OUT}/ARCH"
printf 'local-llm-demo offline 1.0.1\nsource commit %s\nbuilt %s\n' \
  "$(cat "${SRC}/DEPLOYED_COMMIT")" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "${OUT}/VERSION"

for spec in backend:1.0.0 frontend:1.0.0 ollama-cpu:0.32.1; do
  image="${spec%%:*}"
  src_ref="${SRC_REPO}/${spec}"
  if ! docker image inspect "${src_ref}" >/dev/null 2>&1 && [[ -f "${STASH}/${image}.tar" ]]; then
    echo "复用上一版镜像：${image}.tar"
    mv "${STASH}/${image}.tar" "${OUT}/images/${image}.tar"
    continue
  fi
  [[ "$(docker image inspect "${src_ref}" --format '{{.Architecture}}')" == "${ARCH}" ]] \
    || { echo "${src_ref} is not ${ARCH}" >&2; exit 1; }
  # 包内镜像统一命名为 llmdemo/*。打 arm64 包时先把 amd64 镜像备份到 llmdemo-amd64/*，
  # 临时把 llmdemo/* 指向 arm64 镜像导出，最后再恢复，避免 amd64 镜像失去标签被回收。
  if [[ "${SRC_REPO}" != llmdemo ]]; then
    docker tag "llmdemo/${spec}" "llmdemo-amd64/${spec}"
    docker tag "${src_ref}" "llmdemo/${spec}"
    docker save "llmdemo/${spec}" -o "${OUT}/images/${image}.tar"
    docker tag "llmdemo-amd64/${spec}" "llmdemo/${spec}"
  else
    docker save "${src_ref}" -o "${OUT}/images/${image}.tar"
  fi
done

rm -rf "${STASH}"
( cd "${OUT}" && find . -type f ! -name SHA256SUMS ! -name .env | sort | xargs sha256sum > SHA256SUMS )
tar -C "${B}/out" -czf "${B}/out/${NAME}.tar.gz" "${NAME}"
( cd "${B}/out" && sha256sum "${NAME}.tar.gz" > "${NAME}.tar.gz.sha256" )
ls -lh "${B}/out/${NAME}.tar.gz"
