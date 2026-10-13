"""Deterministic test-only Ollama HTTP surface for Playwright."""

import json
from collections.abc import Iterator
from time import sleep

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse


MODEL = "qwen3:1.7b-q4_K_M"
MODEL_SIZE = 1_420_000_000
ANSWER = "本地模型已依据已审核资料生成回答。"

app = FastAPI(title="Task 13 fake Ollama")


@app.get("/api/version")
def version() -> dict[str, str]:
    return {"version": "0.6.8"}


@app.get("/api/tags")
def tags() -> dict[str, list[dict[str, object]]]:
    return {
        "models": [
            {
                "name": MODEL,
                "model": MODEL,
                "size": MODEL_SIZE,
            }
        ]
    }


@app.get("/api/ps")
def running_models() -> dict[str, list[dict[str, object]]]:
    return {
        "models": [
            {
                "name": MODEL,
                "model": MODEL,
                "size": MODEL_SIZE,
            }
        ]
    }


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    payload = await request.json()
    content = _completion_content(payload)
    if payload.get("stream") is True:
        return StreamingResponse(
            _stream_completion(content),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache"},
        )
    return {
        "id": "chatcmpl-task13",
        "object": "chat.completion",
        "model": MODEL,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": content,
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 17,
            "completion_tokens": 9,
            "total_tokens": 26,
        },
    }


def _completion_content(payload: dict) -> str:
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return ANSWER
    system = "\n".join(
        str(message.get("content", ""))
        for message in messages
        if isinstance(message, dict)
        and message.get("role") == "system"
    )
    if "appeal、process、result、follow_up" in system:
        return json.dumps(
            {
                "appeal": "客户咨询服务事项",
                "process": "已核对信息并完成登记",
                "result": "请求已受理，等待后续处理",
                "follow_up": "在承诺时限内反馈进展",
            },
            ensure_ascii=False,
        )
    if "rule_id、risk、description" in system:
        return "[]"
    if "Reply with OK" in system:
        return "OK"
    return ANSWER


def _stream_completion(content: str) -> Iterator[str]:
    midpoint = max(1, len(content) // 2)
    for piece in (content[:midpoint], content[midpoint:]):
        frame = {
            "id": "chatcmpl-task13",
            "object": "chat.completion.chunk",
            "model": MODEL,
            "choices": [
                {
                    "index": 0,
                    "delta": {"content": piece},
                    "finish_reason": None,
                }
            ],
        }
        yield f"data: {json.dumps(frame, ensure_ascii=False)}\n\n"
        sleep(0.05)
    usage = {
        "id": "chatcmpl-task13",
        "object": "chat.completion.chunk",
        "model": MODEL,
        "choices": [],
        "usage": {
            "prompt_tokens": 17,
            "completion_tokens": 9,
            "total_tokens": 26,
        },
    }
    yield f"data: {json.dumps(usage, ensure_ascii=False)}\n\n"
    yield "data: [DONE]\n\n"
