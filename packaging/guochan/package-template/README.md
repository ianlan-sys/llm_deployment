# 本地化大模型应用平台 Demo · 国产 OS 离线安装包

本安装包包含运行 Demo 所需的全部内容：容器镜像、演示模型 qwen3:1.7b、Docker Compose 和离线 Docker。**安装全程不需要联网**，适用于物理隔离环境。

> 本 Demo 仅供演示和技术验证。所有单位、网络、设备和地址均为虚构数据；模型输出必须由人员复核。

## 选择安装包

| 安装包 | 适用 CPU | 常见国产平台 |
| --- | --- | --- |
| `local-llm-demo-offline-amd64.tar.gz` | x86\_64 | 海光、兆芯、Intel / AMD |
| `local-llm-demo-offline-arm64.tar.gz` | ARM64 | 鲲鹏、飞腾 |

不确定时在目标机器上运行 `uname -m`：输出 `x86_64` 选 amd64，输出 `aarch64` 选 arm64。

**已适配的操作系统**：openEuler 22.03 / 24.03 LTS、银河麒麟 V10、统信 UOS 及其他 64 位 Linux（内核 ≥ 4.19）。

**硬件要求**：4 核 CPU、8 GB 内存、20 GB 可用磁盘。演示模式不需要 GPU。

## 安装（3 步）

```bash
# 1. 拷贝安装包到目标机器后解压
tar -xzf local-llm-demo-offline-arm64.tar.gz
cd local-llm-demo-offline-arm64

# 2. 安装（机器上没有 Docker 时加 --install-docker）
sudo ./install.sh --install-docker

# 3. 浏览器打开脚本输出的地址，用输出的用户名和口令登录
```

**口令只显示一次**，请立即记录。忘记口令可运行 `sudo ./reset-password.sh`。

### 一体机演示（推荐：一台国产电脑既运行系统又显示）

适用于装有**银河麒麟 V10 桌面版 / 统信 UOS 桌面版**的国产 PC 或迷你主机（飞腾、鲲鹏选 arm64 包；海光、兆芯选 amd64 包）。用**平时登录桌面的那个用户**打开终端执行：

```bash
sudo ./install.sh --install-docker --desktop --bind 127.0.0.1
```

- `--desktop`：在桌面放一个“本地大模型演示”图标，并在**登录桌面后自动全屏打开** Demo；演示样例文件复制到桌面“演示样例”文件夹，文档审核上传时直接从这里选。
- `--bind 127.0.0.1`：只允许本机访问，网线插着也不会被内网其他电脑访问，符合保密要求。
- 自动选择浏览器：奇安信可信浏览器 → 360 / UOS 浏览器 → Chromium → Firefox。使用独立的浏览器配置，不影响日常浏览记录。指定浏览器可设置环境变量 `LLMDEMO_BROWSER`。
- 全屏中按 **F11** 退出全屏（Firefox 为展台模式，按 **Alt+F4** 关闭），关闭后双击桌面图标重新打开。
- 不想登录后自动打开：`sudo ./setup-desktop.sh --no-autostart`；移除桌面集成：`sudo ./setup-desktop.sh --remove`。
- 已经安装过、只想补装桌面图标：`sudo ./setup-desktop.sh`。

**演示前一天**：重启一次整机，确认登录后 1–3 分钟内页面自动全屏打开，并按下方“演示流程”完整走一遍。

常用参数：

| 参数 | 作用 |
| --- | --- |
| `--port 9000` | 修改访问端口（默认 8080） |
| `--bind 127.0.0.1` | 只允许本机访问（默认内网其他电脑也能访问） |
| `--username 名称` | 修改登录用户名（默认 `demo-operator`） |
| `--install-docker` | 本机无 Docker 时，安装包内自带的离线 Docker |
| `--desktop` | 一体机：桌面图标 + 登录后自动全屏打开 |

## 日常操作

| 命令 | 作用 |
| --- | --- |
| `sudo ./start.sh` | 启动 |
| `sudo ./stop.sh` | 停止（数据保留） |
| `sudo ./status.sh` | 查看状态和日志 |
| `sudo ./reset-password.sh` | 重新生成登录口令 |
| `sudo ./setup-desktop.sh` | 安装 / 移除（`--remove`）桌面图标与自动打开 |
| `sudo ./uninstall.sh` | 卸载服务（数据保留） |
| `sudo ./uninstall.sh --purge` | 彻底删除数据、模型和镜像（不可恢复） |

服务设置为开机自启：机器重启后 Docker 会自动拉起 Demo。

## 演示流程

1. **坐席辅助**：选择预置来电场景，生成、采纳话术，再生成工单摘要。
2. **运维知识库**：询问“核心交换机 VLAN 如何划分？”，展开“来源 1”查看原文。此功能默认使用本机 qwen3:1.7b 模型实时推理（CPU 上首次回答约需 10–30 秒）。
3. **文档审核**：上传 `samples/technical-proposal.txt`，文件类型选“技术方案”，等待完成后查看风险项或导出 CSV。

各业务可在“运行与设置”页切换 Mock（离线、秒级、结果固定）或本地模型模式。**正式演示前建议完整演练一遍**；演示机性能较弱时，把知识库也切到 Mock 模式可获得最稳定的效果。

## 连接真实推理服务（可选）

如现场有 vLLM / MindIE 等 OpenAI 兼容推理服务，编辑安装目录下的 `.env`：

```dotenv
DEMO_PROVIDER_MODE=real
DEMO_OPENAI_BASE_URL=http://<推理服务器IP>:<端口>/v1
DEMO_OPENAI_MODEL=<模型名>
DEMO_OPENAI_API_KEY=<密钥，无则留空>
```

然后运行 `sudo ./start.sh` 使配置生效。

## 故障排查

**一体机登录后没有自动打开页面**
- 等 3 分钟（开机后 Docker 启动服务需要时间）；仍未打开就双击桌面图标，或在终端运行 `/usr/local/share/llmdemo/open-demo.sh` 查看提示。
- 桌面图标显示为文本文件或“不受信任”：右键图标选“允许运行”/“信任”。

**浏览器打不开页面**
- 本机先测：`curl http://127.0.0.1:8080/api/health`。
- 本机正常、其他电脑打不开：多半是防火墙。openEuler / 麒麟执行
  `sudo firewall-cmd --permanent --add-port=8080/tcp && sudo firewall-cmd --reload`。

**提示“本安装包为 amd64，本机为 arm64”**
- 下错了架构，换另一个安装包。

**提示“文件校验失败”**
- 安装包在拷贝中损坏，重新拷贝。可在拷贝前后分别运行 `sha256sum` 比对压缩包本身的校验值（见同目录 `.sha256` 文件）。

**系统自带 Docker 版本较旧、启动报错**
- openEuler / 麒麟源里的 Docker 可能是 18.09。可先卸载系统 Docker（`dnf remove docker`），再用 `sudo ./install.sh --install-docker` 安装包内的 Docker 27。

**端口 8080 被占用**
- `sudo ./install.sh --port 9000`（已安装过也可以重新运行，会保留原口令和数据）。

## 安全说明

- 口令以 scrypt 哈希存储在 `.env`（权限 600），明文不落盘。
- 后端与模型服务只在容器内部网络监听，仅前端端口对外。
- 当前入口为 HTTP：请只在可信内网使用，并使用一次性口令，不要复用其他系统的密码。

## 包内文件

```
install.sh  start.sh  stop.sh  status.sh  reset-password.sh  uninstall.sh  setup-desktop.sh
docker-compose.yml   .env.example   ARCH   VERSION   SHA256SUMS
images/     容器镜像（后端、前端、CPU 版 Ollama）
models/     演示模型 qwen3:1.7b
bin/        离线 Docker 27 与 Docker Compose
samples/    文档审核演示样例
scripts/    安装脚本内部使用
desktop/    一体机桌面图标与全屏启动脚本
```
