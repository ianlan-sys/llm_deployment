# 国产 OS 离线安装包构建

把 Demo 打成可在物理隔离环境安装的离线包，支持 x86\_64（海光、兆芯）和 ARM64（鲲鹏、飞腾）。安装包的使用说明见 [`package-template/README.md`](package-template/README.md)，它会随包一起分发。

## 目录

| 路径 | 内容 | 是否入库 |
| --- | --- | --- |
| `build-package.sh` | 组装安装包并生成校验和 | 是 |
| `package-template/` | 安装包内的脚本、Compose 文件和说明 | 是 |
| `../../docker/*.offline.Dockerfile`、`ollama-cpu.Dockerfile`、`backend-constraints.txt` | 离线镜像的构建文件 | 是 |
| `downloads/` | 离线 Docker 与 Docker Compose 二进制 | 否（体积大，按下文下载） |
| `common/` | 模型文件 `ollama-models-qwen3-1.7b.tar`（约 1.3 GB） | 否 |
| `out/` | 构建产物（每个包约 1.5 GB） | 否 |

## 构建步骤

构建机需要联网、Docker 和 buildx。以下命令都在仓库根目录执行。

### 1. 构建镜像

```bash
# amd64
for img in backend frontend; do
  docker buildx build --platform linux/amd64 -f docker/$img.offline.Dockerfile -t llmdemo/$img:1.0.0 --load .
done
docker buildx build --platform linux/amd64 -f docker/ollama-cpu.Dockerfile -t llmdemo/ollama-cpu:0.32.1 --load .

# arm64（镜像名用 llmdemo-arm64/*，打包时再统一改名为 llmdemo/*）
for img in backend frontend; do
  docker buildx build --platform linux/arm64 -f docker/$img.offline.Dockerfile -t llmdemo-arm64/$img:1.0.0 --load .
done
docker buildx build --platform linux/arm64 -f docker/ollama-cpu.Dockerfile -t llmdemo-arm64/ollama-cpu:0.32.1 --load .
```

后端依赖版本由 `docker/backend-constraints.txt` 锁定，与线上演示环境一致。

### 2. 准备离线 Docker

```bash
D=packaging/guochan/downloads && mkdir -p $D && cd $D
for a in x86_64 aarch64; do
  curl -fsSL -o docker-27.5.1-$a.tgz https://download.docker.com/linux/static/stable/$a/docker-27.5.1.tgz
  curl -fsSL -o docker-compose-linux-$a https://github.com/docker/compose/releases/download/v5.5.1/docker-compose-linux-$a
  curl -fsSL -o docker-compose-linux-$a.sha256 https://github.com/docker/compose/releases/download/v5.5.1/docker-compose-linux-$a.sha256
done
sha256sum -c docker-compose-linux-*.sha256
```

### 3. 导出模型

从一台已经拉取过 `qwen3:1.7b-q4_K_M` 的机器导出 Ollama 数据卷（卷名以实际为准）：

```bash
W=$PWD/packaging/guochan/common && mkdir -p $W
docker run --rm -v local-llm-demo_ollama-data:/d:ro -v $W:/out --entrypoint tar \
  llmdemo/ollama-cpu:0.32.1 -cf /out/ollama-models-qwen3-1.7b.tar -C /d models
```

### 4. 打包

```bash
packaging/guochan/build-package.sh amd64
packaging/guochan/build-package.sh arm64
```

产物在 `packaging/guochan/out/`，每个包旁边有 `.sha256` 文件。本地没有对应镜像时，脚本会复用 `out/` 里上一版安装包中的镜像 tar，只更新脚本和文档。

## 测试安装包时的注意事项

- 管理脚本的 Compose 项目名固定为 `llmdemo`（见 `package-template/scripts/lib.sh`）。在已经运行着同名项目的机器上测试时，先改掉测试副本里的项目名，避免冲突。
- 在 x86 构建机上测试 arm64 包需要先注册 QEMU：`docker run --privileged --rm tonistiigi/binfmt --install arm64`，测完用 `--uninstall qemu-aarch64` 移除。模拟环境下推理速度约 0.3 tokens/s，只能验证功能，不能代表真实性能。

## 当前状态（2026-10-02）

- amd64、arm64 两个包均已构建，校验和通过。
- arm64 包已在 QEMU 模拟下验证：服务健康、登录、口令重置、模型推理均正常。
- **尚未在真实的飞腾 / 鲲鹏设备上运行过。** 目前以 QEMU 模拟验证为准（2026-10-13 确认）；拿到演示设备后需要完整安装一次，并在重启后做一次全流程彩排。
- 目标操作系统已确定为麒麟（2026-10-13 确认）：演示一体机用银河麒麟桌面版，客户服务器用银河麒麟服务器版。
