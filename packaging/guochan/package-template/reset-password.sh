#!/usr/bin/env bash
# 重新生成登录口令（保留其他配置与数据）
set -Eeuo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/scripts/lib.sh"
[[ -f .env ]] || { echo "未找到 .env，请先运行 ./install.sh" >&2; exit 1; }
username="$(sed -n "s/^DEMO_AUTH_USERNAME='\(.*\)'$/\1/p" .env)"
umask 077
err_file="$(mktemp)"
trap 'rm -f "${err_file}" .env.new .env.creds' EXIT
docker run --rm --network none -v "${PKG_DIR}:/pkg:ro" llmdemo/backend:1.0.0 \
  python /pkg/scripts/render_env.py /pkg/.env.example "${username:-demo-operator}" > .env.creds 2> "${err_file}"
# 只替换凭据三项，其余配置保持不变
cp .env .env.new
for key in DEMO_AUTH_PASSWORD_HASH DEMO_SESSION_SECRET; do
  value="$(grep "^${key}=" .env.creds)"
  grep -v "^${key}=" .env.new > .env.new.tmp && echo "${value}" >> .env.new.tmp && mv .env.new.tmp .env.new
done
mv .env.new .env
chmod 600 .env
"${COMPOSE[@]}" up -d --force-recreate backend
# 前端 nginx 会缓存后端容器 IP，后端重建后需重启前端，否则出现 502
"${COMPOSE[@]}" restart frontend
echo "登录用户：${username}"
echo "新口令：$(sed -n 's/^DEMO_AUTH_PASSWORD=//p' "${err_file}")（只显示一次，请立即记录）"
