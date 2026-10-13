# 静态前端只在构建机架构上编译一次，最终镜像按目标架构选择 nginx 基础镜像。
FROM --platform=$BUILDPLATFORM node:22-alpine AS build
WORKDIR /workspace
COPY frontend/package.json frontend/pnpm-lock.yaml ./
RUN corepack enable && corepack pnpm install --frozen-lockfile
COPY frontend/ ./
RUN corepack pnpm build

FROM nginx:1.27-alpine
COPY docker/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /workspace/dist /usr/share/nginx/html
EXPOSE 80
