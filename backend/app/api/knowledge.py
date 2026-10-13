import asyncio
import json
from collections.abc import AsyncIterator, Iterator
from threading import Event

import anyio
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.database import get_session
from app.providers.base import ProviderUnavailable
from app.schemas import KnowledgeQueryRequest, KnowledgeQueryResponse
from app.services.inference import (
    InferenceQueueFull,
    InferenceTimedOut,
)
from app.services.knowledge import KnowledgeService, KnowledgeStreamEvent

router = APIRouter(prefix="/api/knowledge", tags=["knowledge"])
_STREAM_END = object()


def _sse(event: str, data: dict) -> str:
    return (
        f"event: {event}\n"
        f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
    )


def _service(request: Request, session: Session) -> KnowledgeService:
    snapshot = request.app.state.feature_provider_router.snapshot(
        "knowledge"
    )
    return KnowledgeService(
        session,
        request.app.state.knowledge_index_service,
        request.app.state.inference_coordinator,
        snapshot,
    )


@router.post("/query", response_model=KnowledgeQueryResponse)
def query_knowledge(
    payload: KnowledgeQueryRequest,
    request: Request,
    session: Session = Depends(get_session),
) -> KnowledgeQueryResponse:
    try:
        return _service(request, session).query(
            payload.question, payload.scope
        )
    except ProviderUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail="模型服务暂不可用，请重试或手动切换为 Mock 模式。",
        ) from exc
    except InferenceQueueFull as exc:
        raise HTTPException(
            status_code=429,
            detail="本地模型请求较多，请稍后重试。",
        ) from exc
    except InferenceTimedOut as exc:
        raise HTTPException(
            status_code=503,
            detail="本地模型响应超时，请稍后重试。",
        ) from exc


async def _close_events(
    events: Iterator[KnowledgeStreamEvent],
) -> None:
    close = getattr(events, "close", None)
    if close is None:
        return
    try:
        await run_in_threadpool(close)
    except (RuntimeError, ValueError):
        return


async def _event_stream(
    events: Iterator[KnowledgeStreamEvent],
    request: Request,
) -> AsyncIterator[str]:
    try:
        while True:
            iteration_done = Event()
            try:
                frame = await anyio.to_thread.run_sync(
                    _next_event,
                    events,
                    iteration_done,
                    abandon_on_cancel=True,
                )
            except asyncio.CancelledError:
                with anyio.CancelScope(shield=True):
                    await _close_events(events)
                    await anyio.to_thread.run_sync(
                        iteration_done.wait
                    )
                raise
            if frame is _STREAM_END:
                return
            if await request.is_disconnected():
                return
            yield _sse(frame.event, frame.data)
    except asyncio.CancelledError:
        raise
    except ProviderUnavailable:
        yield _sse(
            "error",
            {
                "status": "error",
                "message": (
                    "模型服务暂不可用，请重试或手动切换为 Mock 模式。"
                ),
            },
        )
    except InferenceQueueFull:
        yield _sse(
            "error",
            {
                "status": "queue_full",
                "message": "本地模型请求较多，请稍后重试。",
            },
        )
    except InferenceTimedOut:
        yield _sse(
            "error",
            {
                "status": "timeout",
                "message": "本地模型响应超时，请稍后重试。",
            },
        )
    except Exception:
        yield _sse(
            "error",
            {
                "status": "error",
                "message": "知识问答暂不可用，请稍后重试。",
            },
        )
    finally:
        with anyio.CancelScope(shield=True):
            await _close_events(events)


def _next_event(
    events: Iterator[KnowledgeStreamEvent],
    iteration_done: Event,
):
    try:
        return next(events)
    except StopIteration:
        return _STREAM_END
    finally:
        iteration_done.set()


@router.post("/query/stream")
def stream_knowledge(
    payload: KnowledgeQueryRequest,
    request: Request,
    session: Session = Depends(get_session),
) -> StreamingResponse:
    events = _service(request, session).stream(
        payload.question, payload.scope
    )
    return StreamingResponse(
        _event_stream(events, request),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
