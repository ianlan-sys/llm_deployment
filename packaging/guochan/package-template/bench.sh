#!/usr/bin/env bash
# 本地模型测速：让演示模型回答一个固定问题，输出加载时间和生成速度。需先安装并启动服务。
# 用法：sudo ./bench.sh [次数，默认 3]
set -Eeuo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/scripts/lib.sh"

ROUNDS="${1:-3}"
[[ "${ROUNDS}" =~ ^[1-9][0-9]?$ ]] || { echo "次数应为 1–99 的整数" >&2; exit 1; }

echo "测速中：共 ${ROUNDS} 次，每次生成约 120 个 token（CPU 上每次约 10–120 秒）……"
"${COMPOSE[@]}" exec -T backend python - "${ROUNDS}" <<'PY'
import json
import sys
import urllib.request

rounds = int(sys.argv[1])
body = {
    "model": "qwen3:1.7b-q4_K_M",
    "prompt": "用三句话说明核心交换机划分 VLAN 的作用。",
    "stream": False,
    "think": False,
    "keep_alive": -1,
    "options": {"num_predict": 120, "temperature": 0},
}
speeds = []
for index in range(1, rounds + 1):
    request = urllib.request.Request(
        "http://ollama:11434/api/generate",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        result = json.loads(urllib.request.urlopen(request, timeout=900).read())
    except Exception as error:  # noqa: BLE001 - 现场排查需要看到原始错误
        print(f"  第 {index} 次：失败（{error}）")
        continue
    tokens = result.get("eval_count", 0)
    seconds = result.get("eval_duration", 0) / 1e9
    load = result.get("load_duration", 0) / 1e9
    first = (result.get("load_duration", 0) + result.get("prompt_eval_duration", 0)) / 1e9
    speed = tokens / seconds if seconds else 0.0
    speeds.append(speed)
    print(
        f"  第 {index} 次：模型加载 {load:.1f} 秒，首字前等待 {first:.1f} 秒，"
        f"生成 {tokens} token 用时 {seconds:.1f} 秒，速度 {speed:.1f} token/秒"
    )

if not speeds:
    sys.exit("测速失败：模型服务没有返回结果，请运行 ./status.sh 查看日志")
average = sum(speeds) / len(speeds)
print(f"平均生成速度：{average:.1f} token/秒")
if average >= 8:
    print("结论：速度足够现场实时演示本地模型。")
elif average >= 3:
    print("结论：可以演示，但回答较慢；建议提前把要问的问题问一遍，或把知识库切到 Mock 模式。")
else:
    print("结论：速度过慢，不建议现场用本地模型；请在“运行与设置”页把各业务切到 Mock 模式。")
PY
