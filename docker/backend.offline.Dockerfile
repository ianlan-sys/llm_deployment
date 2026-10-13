# 离线包后端镜像：依赖在构建机上按目标架构下载 wheel 安装，最终镜像无需联网，
# 同时支持 linux/amd64（海光/兆芯）与 linux/arm64（鲲鹏/飞腾）。
FROM --platform=$BUILDPLATFORM python:3.11-slim AS deps
ARG TARGETARCH
COPY backend /src/backend
# 与当前线上后端完全一致的依赖版本，保证演示行为一致
COPY docker/backend-constraints.txt /src/constraints.txt
RUN set -eu; \
    case "$TARGETARCH" in \
      amd64) A=x86_64 ;; \
      arm64) A=aarch64 ;; \
      *) echo "unsupported arch: $TARGETARCH" >&2; exit 1 ;; \
    esac; \
    pip wheel --no-deps --no-cache-dir -w /wheels /src/backend; \
    PLATFORMS=""; \
    for t in manylinux2014 manylinux_2_17 manylinux_2_24 manylinux_2_27 manylinux_2_28 manylinux_2_31 manylinux_2_34; do \
      PLATFORMS="$PLATFORMS --platform ${t}_${A}"; \
    done; \
    pip install --no-cache-dir --target /deps $PLATFORMS \
      --only-binary=:all: --python-version 3.11 --implementation cp \
      -c /src/constraints.txt \
      /wheels/*.whl; \
    pip freeze --path /deps > /deps/REQUIREMENTS.lock; \
    mkdir -p /empty-data

FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/opt/deps \
    DEMO_RUNTIME_DIR=/data
COPY --from=deps /deps /opt/deps
COPY --from=deps /empty-data /data
WORKDIR /app
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
