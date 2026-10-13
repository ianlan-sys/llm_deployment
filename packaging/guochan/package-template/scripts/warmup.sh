# shellcheck shell=bash
# 预加载演示模型并常驻内存，避免演示时第一个问题等待模型加载。需先 source lib.sh。
warmup_model() {
  "${COMPOSE[@]}" exec -T backend python -c '
import json, urllib.request
req = urllib.request.Request(
    "http://ollama:11434/api/generate",
    data=json.dumps({"model": "qwen3:1.7b-q4_K_M", "keep_alive": -1}).encode(),
    headers={"Content-Type": "application/json"},
)
urllib.request.urlopen(req, timeout=300).read()
' >/dev/null 2>&1
}
