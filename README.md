# 本地化大模型应用平台 Demo

这是一套用于领导汇报和技术 POC 的本地 Web Demo，包含坐席辅助、运维知识问答和文档审核三条业务流。默认的演示模式完全离线、输出可复现，不需要模型权重或外部服务。

> 本项目仅供演示和技术验证。所有单位、网络、设备和地址均为虚构数据；模型输出不是正式审核结论、运维变更指令或专业意见，必须由人员复核。

## Windows 一键启动

前置条件：Windows PowerShell 5.1 或 PowerShell 7、Python 3.11（`python` 或 `py -3.11`）、Node.js 与 pnpm/Corepack。首次运行若发现依赖缺失，脚本会先明确提示，再安装后端或前端依赖。

```powershell
powershell -ExecutionPolicy Bypass -File scripts\start-demo.ps1
```

未预先设置认证环境变量时，脚本会在当前终端打印仅供本次运行使用的临时登录凭据，并将认证配置只传给后端子进程；明文密码不会写入文件。请保存终端中打印的用户名和密码用于登录。浏览器会打开 [http://localhost:5173](http://localhost:5173)。服务进程以隐藏子窗口运行，日志和 PID 元数据位于 `data/runtime/`。不想自动打开浏览器时可传入 `-NoBrowser`。

停止服务：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\stop-demo.ps1
```

停止脚本在结束进程前会校验 PID、可执行文件路径和启动命令行是否与启动时的记录完全一致，避免误停复用了旧 PID 的其他进程。全部进程安全停止后，它只删除 `processes.json`，不删除数据库、上传文件或日志；任一进程校验失败时 PID 文件会保留，便于排查和重试。

## Docker Compose

Docker 路径只向主机公开前端入口 `8080`，后端只在 Compose 内部网络上提供 `8000`。SQLite 和上传文件保存在 `backend-data` 具名卷中。

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\initialize-demo-env.ps1
docker compose up --build -d
Invoke-RestMethod http://localhost:8080/api/health
```

初始化脚本从 `.env.example` 创建被 Git 忽略的 `.env`，拒绝覆盖已有文件，并只把用户名、密码哈希和会话密钥写入其中；明文密码仅在终端打印一次，请保存后用于登录。打开 [http://localhost:8080](http://localhost:8080)。普通停止不会删除具名卷：

```powershell
docker compose down
```

## 演示流程

1. 在“坐席辅助”中选择预置来电场景，生成、采纳话术，再生成工单摘要。
2. 在“运维知识库”询问“核心交换机 VLAN 如何划分？”，展开“来源 1”查看原文。
3. 在“文档审核”上传 `backend/app/data/review_samples/technical-proposal.txt`，文件类型选“技术方案”，等待完成后查看风险项或导出 CSV。

## 真实模型配置

Docker 运行时复制 `.env.example` 为 `.env`，填写端点、模型和密钥，并将 `DEMO_PROVIDER_MODE` 改为 `real`。Windows 运行时在启动脚本前设置同名服务器环境变量：

```powershell
$env:DEMO_OPENAI_BASE_URL='http://127.0.0.1:11434/v1'
$env:DEMO_OPENAI_MODEL='your-model-name'
$env:DEMO_OPENAI_API_KEY='server-side-secret'
$env:DEMO_PROVIDER_MODE='real'
powershell -ExecutionPolicy Bypass -File scripts\start-demo.ps1
```

- `DEMO_OPENAI_BASE_URL`：Ollama、vLLM、MindIE 或其他 OpenAI 兼容服务地址。
- `DEMO_OPENAI_MODEL`：模型名称。
- `DEMO_OPENAI_API_KEY`：仅由 FastAPI 进程从服务器环境读取。前端不包含密钥输入框，该值不会进入 Vite 构建参数、浏览器存储、API 响应或审计日志。
- `DEMO_PROVIDER_MODE=real`：在启动时选择真实模型；也可在运行与设置页通过连接测试后启用。

设置页可配置非敏感的 Base URL 和模型名称，并在启用真实模型前执行连接测试。默认是 `DEMO_PROVIDER_MODE=demo`；建议先保持演示模式完成离线演练。

## 数据、限制与重置

- 支持 PDF、DOCX 和 UTF-8 TXT，默认单文件上限为 20 MiB（`DEMO_MAX_UPLOAD_BYTES=20971520`）。Docker Nginx 使用 22 MiB 代理包络以容纳 20 MiB 文件的 multipart 额外开销；如调大后端环境值，也要同步调整 `docker/nginx.conf` 的 `client_max_body_size`。
- 扫描版 PDF 没有可提取文本时会在解析阶段失败；首版不内置 OCR，请先用受信任工具完成 OCR。
- Windows 数据位于 `data/runtime/`。如需完全重置，先运行停止脚本，再手工删除该目录。
- Docker 普通 `docker compose down` 会保留数据。只在明确需要不可恢复的全量重置时运行 `docker compose down --volumes`。

## 故障排查

### 端口被占用

Windows 启动需要 `8000` 和 `5173`，Docker 入口需要 `8080`。先停止已有 Demo，再用 `Get-NetTCPConnection -LocalPort 8000,5173,8080 -ErrorAction SilentlyContinue` 定位占用者。不要直接删除 PID 文件后强制结束不明进程。

### 模型连接超时

确认 Base URL 从后端运行环境可达。Docker Desktop 访问宿主机服务时通常使用 `host.docker.internal`，不要使用容器内的 `127.0.0.1`。根据模型首 token 延迟调整 `DEMO_PROVIDER_TIMEOUT_SECONDS`；连接失败不会静默切换成演示结果。

### 审核任务被中断

服务重启时，尚未完成的审核任务会标记为“已中断”，而不会伪装成完成。在文档审核页选中该任务后点击“重新审核”。原上传文件保留在本地数据目录或 Docker 具名卷中。

### Windows 启动失败

查看 `data/runtime/backend.stderr.log` 和 `data/runtime/frontend.stderr.log`。如果 `processes.json` 存在，优先运行 `scripts\stop-demo.ps1`；它只会停止命令行与启动记录完全匹配的进程。

## 开发验证

```powershell
cd backend
py -3.11 -m pytest -q
py -3.11 -m ruff check app tests
cd ..\frontend
corepack pnpm vitest run
corepack pnpm build
corepack pnpm lint
corepack pnpm exec playwright install chromium
corepack pnpm exec playwright test
```

## 服务器运维手册（`/opt/local-llm-demo`）

以下命令面向 Linux 服务器上的 Compose 部署，并假定当前运维用户就是
`root`；本节命令统一不使用 `sudo`。应用通过宿主机公网端口 `8080` 提供演示
访问；Ollama 只在 Compose 私有网络监听，绝不能为它添加宿主机 `ports`。
现有端口 `8000` 上的 `everlink-api` 不属于本项目，部署和回滚时都不得停止
或修改它。

### 1. 备份部署目录和应用数据

在任何变更前记录容器状态，并创建带时间戳的源码与 SQLite/上传文件备份。
为了获得一致的应用数据归档，短暂停止本 Demo 的前后端；不要删除或重建具名卷。

```bash
set -Eeuo pipefail
cd /opt/local-llm-demo
docker compose ps
STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP_DIR="/opt/backups/local-llm-demo-${STAMP}"
install -d -m 700 "${BACKUP_DIR}"
cp -a /opt/local-llm-demo "${BACKUP_DIR}/source"
BACKEND_CONTAINER="$(docker compose ps --all -q backend)"
test -n "${BACKEND_CONTAINER}"
BACKEND_VOLUME="$(docker inspect "${BACKEND_CONTAINER}" \
  --format '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Name}}{{end}}{{end}}')"
test -n "${BACKEND_VOLUME}"
docker volume inspect "${BACKEND_VOLUME}"
docker compose stop frontend backend
docker run --rm -v "${BACKEND_VOLUME}:/data:ro" \
  -v "${BACKUP_DIR}:/backup" alpine:3.21 \
  tar -czf /backup/backend-data.tgz -C /data .
docker compose up -d backend frontend
```

`backend-data` 保存 SQLite、上传文件和索引；`ollama-data` 保存模型权重。
升级不会执行 `docker compose down --volumes`，因此两个具名卷都会保留。
记录本次输出的 `BACKUP_DIR` 和 `BACKEND_VOLUME`，供回滚使用。

### 2. 按审核通过的提交 SHA 发布源码

由发布负责人提供审核通过的完整 40 位提交 SHA。先确认工作树干净、远端确实是
指定 Tencent Git 仓库，再抓取对象并进入 detached HEAD；不要盲目执行 `git pull`，
也不要以可移动的分支头代替发布 SHA：

```bash
set -Eeuo pipefail
cd /opt/local-llm-demo
test -z "$(git status --porcelain)"
test "$(git remote get-url origin)" = \
  "https://git.code.tencent.com/hongxulan/LLM-demo-local-deoplyment.git"
DEPLOY_SHA='<approved-40-character-commit-sha>'
printf '%s' "${DEPLOY_SHA}" | grep -Eq '^[0-9a-f]{40}$'
git fetch --no-tags origin "${DEPLOY_SHA}"
test "$(git rev-parse FETCH_HEAD)" = "${DEPLOY_SHA}"
git cat-file -e "${DEPLOY_SHA}^{commit}"
git checkout --detach "${DEPLOY_SHA}"
test "$(git rev-parse HEAD)" = "${DEPLOY_SHA}"
test -z "$(git status --porcelain)"
```

任一检查失败都必须停止发布并排查，不能覆盖服务器上的未知改动。

### 3. 生成并配置一次性演示凭据

在可信 Windows 运维机的仓库根目录运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/generate-demo-auth.ps1
```

脚本只向当前终端打印用户名、20 位明文密码、scrypt 哈希和会话密钥，不写入
文件。明文密码只显示一次。把 `DEMO_AUTH_USERNAME`、
`DEMO_AUTH_PASSWORD_HASH` 和 `DEMO_SESSION_SECRET` 写入服务器 `.env`，不要把
`DEMO_AUTH_PASSWORD` 写入 `.env`：

```bash
set -Eeuo pipefail
cd /opt/local-llm-demo
umask 077
cp -n .env.example .env
chmod 600 .env
${EDITOR:-vi} .env
```

服务器 `.env` 中必须设置：

```dotenv
DEMO_AUTH_REQUIRED=true
DEMO_AUTH_USERNAME=<generator output>
DEMO_AUTH_PASSWORD_HASH=<generator output>
DEMO_SESSION_SECRET=<generator output>
```

当前入口是 HTTP 时，演示账号和密码必须视为可丢弃的一次性凭据；不要复用个人、
生产或其他系统密码。对外长期开放前应配置 HTTPS。

### 4. 启动私有 Ollama 并拉取指定模型

先只启动 Ollama，再显式运行一次性初始化容器。`model-init` 必须以状态码 0
结束，才可继续构建应用：

```bash
set -Eeuo pipefail
cd /opt/local-llm-demo
docker compose up -d ollama
docker compose run --rm model-init
docker compose exec ollama ollama list
```

`ollama list` 必须包含精确标签 `qwen3:1.7b-q4_K_M`，此时只确认模型已下载，
不能据此宣称模型已加载。同时确认宿主机没有 Ollama 监听端口：

```bash
set -Eeuo pipefail
ss -ltn | grep ':11434 ' && exit 1 || true
```

### 5. 构建、启动与健康检查

```bash
set -Eeuo pipefail
cd /opt/local-llm-demo
docker compose up --build -d backend frontend
docker compose ps
curl -fsS http://127.0.0.1:8080/api/health
docker compose logs --tail=200 backend frontend ollama
```

不得占用或替换现有的 `8000` 服务。可用
`ss -ltnp | grep ':8000 '` 在部署前后记录并核对该监听器仍在运行。

### 6. 浏览器真实模型验收

使用第 3 步临时保存的明文密码登录，然后依次完成：

1. 未登录访问业务页会跳转登录页，凭据登录成功；
2. 监控页显示 Ollama 在线、模型已下载，但尚不把“已下载”误报成“已加载”；
3. 在设置页把“运维知识库”切换为 Local，再提出推荐问题，回答包含可展开的引用；
4. 首次真实问答完成后返回监控页，确认模型状态变为已加载；
5. 上传 UTF-8 TXT，预览并批准，再次提问能够引用新资料；
6. 在设置页把“坐席辅助”单独切换为 Local，确认文档审核模式保持不变；
7. 监控页的 24 小时请求统计在真实调用后更新。

公网演示入口为 `http://<server-ip>:8080/`；当前仍是 HTTP，因此只使用本次部署
生成的可丢弃凭据。

### 7. 重启与持久化验收

```bash
set -Eeuo pipefail
cd /opt/local-llm-demo
docker compose restart
docker compose ps
curl -fsS http://127.0.0.1:8080/api/health
docker compose exec ollama ollama list
```

重新登录并确认已批准的上传资料、知识库引用、功能模式设置与监控历史仍存在。
这证明 `backend-data` 和 `ollama-data` 在 Compose 重启后保持持久化。

### 8. 日志、凭据轮换与日常检查

```bash
set -Eeuo pipefail
cd /opt/local-llm-demo
docker compose logs --since=30m --tail=300 backend frontend ollama
docker compose ps
docker compose exec ollama ollama list
curl -fsS http://127.0.0.1:8080/api/health
```

轮换凭据时，重新运行 `scripts/generate-demo-auth.ps1`，只把新的用户名、密码哈希
和会话密钥更新到服务器 `.env`，随后重建后端以使旧会话立即失效：

```bash
set -Eeuo pipefail
cd /opt/local-llm-demo
${EDITOR:-vi} .env
docker compose up -d --force-recreate backend
docker compose up -d frontend
```

再次完成未登录跳转、新凭据登录和健康检查；安全地丢弃旧明文密码。

### 9. 回滚

把 `BACKUP_DIR` 设置为第 1 步记录的确切目录。先验证源码和归档存在，并确认
`BACKEND_VOLUME` 是本项目 `/data` 使用的具名卷，再停止本 Demo：

```bash
set -Eeuo pipefail
BACKUP_DIR="/opt/backups/local-llm-demo-YYYYMMDD-HHMMSS"
test -f "${BACKUP_DIR}/backend-data.tgz"
test -f "${BACKUP_DIR}/source/docker-compose.yml"
tar -tzf "${BACKUP_DIR}/backend-data.tgz" >/dev/null
docker run --rm -v "${BACKUP_DIR}:/backup:ro" alpine:3.21 tar -tzf /backup/backend-data.tgz >/dev/null
cd /opt/local-llm-demo
BACKEND_CONTAINER="$(docker compose ps --all -q backend)"
test -n "${BACKEND_CONTAINER}"
BACKEND_VOLUME="$(docker inspect "${BACKEND_CONTAINER}" \
  --format '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Name}}{{end}}{{end}}')"
test -n "${BACKEND_VOLUME}"
docker volume inspect "${BACKEND_VOLUME}"
docker compose stop frontend backend
rsync -a --delete "${BACKUP_DIR}/source/" /opt/local-llm-demo/
docker run --rm -v "${BACKEND_VOLUME}:/data" \
  -v "${BACKUP_DIR}:/backup:ro" alpine:3.21 sh -eu -c \
  'tar -tzf /backup/backend-data.tgz >/dev/null
  RESTORE_DIR="$(mktemp -d /data/.restore-staging.XXXXXX)"
  trap "rm -rf -- \"${RESTORE_DIR}\"" EXIT
  tar -xzf /backup/backend-data.tgz -C "${RESTORE_DIR}"
  test -n "$(find "${RESTORE_DIR}" -mindepth 1 -print -quit)"
  find /data -mindepth 1 -maxdepth 1 ! -path "${RESTORE_DIR}" -exec rm -rf -- {} +
  cp -a "${RESTORE_DIR}/." /data/
  rm -rf -- "${RESTORE_DIR}"
  trap - EXIT'
cd /opt/local-llm-demo
docker compose up --build -d
docker compose ps
curl -fsS http://127.0.0.1:8080/api/health
```

`set -Eeuo pipefail` 会让任一校验或恢复命令失败时立即退出。恢复失败时服务保持停止，
原备份不会被修改；检查错误和剩余的 `.restore-staging.*` 后，修正问题后安全重试
完整回滚块。在健康检查成功前不要单独启动前端或后端。

回滚仅恢复保存的 Compose/源码和 `backend-data` 中的 SQLite、上传文件与索引；
不会删除 `ollama-data`，也不会触碰 `everlink-api` 或端口 `8000`。
