FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DEMO_RUNTIME_DIR=/data

WORKDIR /app
COPY backend /tmp/backend
RUN python -m pip install --no-cache-dir /tmp/backend \
    && rm -rf /tmp/backend \
    && mkdir -p /data

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
