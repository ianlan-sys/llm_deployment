import asyncio
import json
from collections.abc import AsyncIterator, Callable, Iterator
from datetime import datetime, timezone
from time import perf_counter
from uuid import uuid4

import anyio
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.database import get_session
from app.providers.base import ProviderUnavailable
from app.schemas import (
    CallAdoptionRequest,
    CallScenarioResponse,
    CallSessionResponse,
    CallSuggestionRequest,
    CallSummaryRequest,
    CallSummaryResponse,
    CallTemplateReference,
)
from app.services.audit import AuditService
from app.services.call_assistant import CallAssistant
from app.services.inference import (
    InferenceCancellation,
    InferenceQueueFull,
    InferenceTimedOut,
)

router = APIRouter(tags=["calls"])
_CALL_STREAM_END = object()


def sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def _close_provider_stream(provider_stream: Iterator[str]) -> None:
    close = getattr(provider_stream, "close", None)
    if close is None:
        return
    try:
        await run_in_threadpool(close)
    except (RuntimeError, ValueError):
        # A generator may still be unwinding in the abandoned worker thread.
        return


async def call_suggestion_events(
    provider_stream: Iterator[str],
    request: Request,
    audit: AuditService,
    request_id: str,
    mode: str,
    scenario_id: str,
    template_ids: list[str],
    started_at: str,
    started: float,
    session_id: str | None = None,
    templates: list[CallTemplateReference] | None = None,
    assistant: CallAssistant | None = None,
    cancel_stream: Callable[[], object] | None = None,
) -> AsyncIterator[str]:
    """Format a synchronous provider stream without blocking the ASGI loop."""
    audited = False
    pieces: list[str] = []
    ttft_ms: int | None = None

    def record_once(event_type: str, result_status: str, request_summary: str) -> None:
        nonlocal audited
        if audited:
            return
        audited = True
        audit.record(
            event_type,
            result_status,
            request_summary,
            mode,
            session_id or scenario_id,
            int((perf_counter() - started) * 1000),
        )

    def persist_stream(status: str) -> None:
        if assistant is None or session_id is None:
            return
        assistant.update_stream(
            session_id,
            "".join(pieces),
            ttft_ms,
            int((perf_counter() - started) * 1000),
            status,
        )

    meta = {
        "request_id": request_id,
        "mode": mode,
        "template_ids": template_ids,
        "started_at": started_at,
    }
    if session_id is not None:
        meta["session_id"] = session_id
        meta["templates"] = [template.model_dump() for template in templates or []]

    try:
        yield sse("meta", meta)
        while True:
            text = await anyio.to_thread.run_sync(
                _next_provider_text,
                provider_stream,
                abandon_on_cancel=True,
            )
            if text is _CALL_STREAM_END:
                break
            if await request.is_disconnected():
                if cancel_stream is not None:
                    cancel_stream()
                persist_stream("cancelled")
                record_once(
                    "call.suggestion.cancelled",
                    "cancelled",
                    "Call suggestion stream cancelled",
                )
                return
            if ttft_ms is None:
                ttft_ms = int((perf_counter() - started) * 1000)
            pieces.append(text)
            yield sse("delta", {"content": text})
        elapsed_ms = int((perf_counter() - started) * 1000)
        persist_stream("completed")
        record_once(
            "call.suggestion.completed",
            "completed",
            "Call suggestion stream completed",
        )
        yield sse("done", {"request_id": request_id, "elapsed_ms": elapsed_ms})
    except asyncio.CancelledError:
        if cancel_stream is not None:
            cancel_stream()
        persist_stream("cancelled")
        record_once(
            "call.suggestion.cancelled",
            "cancelled",
            "Call suggestion stream cancelled",
        )
        raise
    except InferenceQueueFull:
        persist_stream("failed")
        record_once(
            "call.suggestion.failed",
            "queue_full",
            "Call suggestion queue full",
        )
        yield sse(
            "error",
            {
                "status": "queue_full",
                "message": "本地模型请求较多，请稍后重试。",
            },
        )
    except InferenceTimedOut:
        persist_stream("failed")
        record_once(
            "call.suggestion.failed",
            "timeout",
            "Call suggestion timed out",
        )
        yield sse(
            "error",
            {
                "status": "timeout",
                "message": "本地模型响应超时，请稍后重试。",
            },
        )
    except ProviderUnavailable:
        persist_stream("failed")
        record_once(
            "call.suggestion.failed",
            "failed",
            "Call suggestion provider unavailable",
        )
        yield sse(
            "error",
            {"message": "模型服务暂不可用，请检查配置后重试。"},
        )
    finally:
        await _close_provider_stream(provider_stream)


def _next_provider_text(
    provider_stream: Iterator[str],
) -> str | object:
    try:
        return next(provider_stream)
    except StopIteration:
        return _CALL_STREAM_END


@router.get("/api/demo/call-scenarios", response_model=list[CallScenarioResponse])
def call_scenarios(session: Session = Depends(get_session)) -> list[CallScenarioResponse]:
    return CallAssistant(session).scenarios()


@router.post("/api/calls/suggestions/stream")
async def stream_suggestions(
    payload: CallSuggestionRequest,
    request: Request,
    session: Session = Depends(get_session),
) -> StreamingResponse:
    snapshot = request.app.state.feature_provider_router.snapshot(
        "call_assistant"
    )
    assistant = CallAssistant(session, snapshot.provider)
    try:
        scenario, references, provider_stream = assistant.suggestion_stream(
            payload.scenario_id, payload.transcript
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="未找到通话场景。") from exc

    request_id = str(uuid4())
    call_session = assistant.create_session(
        scenario.id,
        payload.transcript,
        references,
        snapshot.mode,
        request_id,
    )
    started_at = datetime.now(timezone.utc).isoformat()
    started = perf_counter()
    cancellation = InferenceCancellation()
    coordinated_stream = request.app.state.inference_coordinator.stream(
        "call_assistant",
        "call_suggestion",
        snapshot,
        lambda: provider_stream,
        cancellation=cancellation,
    )

    def cancel_stream() -> None:
        cancellation.cancel()
        cancel = getattr(provider_stream, "cancel", None)
        if cancel is not None:
            cancel()

    return StreamingResponse(
        call_suggestion_events(
            provider_stream=coordinated_stream,
            request=request,
            audit=AuditService(session),
            request_id=request_id,
            mode=snapshot.mode,
            scenario_id=scenario.id,
            template_ids=[reference.id for reference in references],
            started_at=started_at,
            started=started,
            session_id=call_session.id,
            templates=references,
            assistant=assistant,
            cancel_stream=cancel_stream,
        ),
        media_type="text/event-stream",
    )


@router.get(
    "/api/calls/sessions/{session_id}", response_model=CallSessionResponse
)
def get_call_session(
    session_id: str, session: Session = Depends(get_session)
) -> CallSessionResponse:
    try:
        return CallSessionResponse.model_validate(
            CallAssistant(session).required_session(session_id)
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="未找到通话会话。") from exc


@router.put(
    "/api/calls/sessions/{session_id}/adoption",
    response_model=CallSessionResponse,
)
def adopt_call_suggestion(
    session_id: str,
    payload: CallAdoptionRequest,
    session: Session = Depends(get_session),
) -> CallSessionResponse:
    assistant = CallAssistant(session)
    try:
        response = assistant.adopt(session_id, payload.accepted_reply)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="未找到通话会话。") from exc
    AuditService(session).record(
        "call.adoption.updated",
        response.adoption_status,
        "Call adoption updated",
        response.provider_mode,
        session_id,
    )
    return response


@router.post("/api/calls/summary", response_model=CallSummaryResponse)
def summarize_call(
    payload: CallSummaryRequest,
    request: Request,
    session: Session = Depends(get_session),
) -> CallSummaryResponse:
    started = perf_counter()
    audit = AuditService(session)
    assistant = CallAssistant(session)
    try:
        call_session = assistant.required_session(payload.session_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="未找到通话会话。") from exc
    if (
        call_session.scenario_id != payload.scenario_id
        or call_session.transcript != payload.original_transcript
    ):
        raise HTTPException(status_code=409, detail="通话会话上下文不一致。")

    snapshot = request.app.state.feature_provider_router.snapshot(
        "call_assistant"
    )
    scenario = call_session.scenario
    call_context = {
        "scenario": {
            "id": scenario.id,
            "name": scenario.name,
            "description": scenario.description,
            "category": scenario.category,
            "priority": scenario.priority,
        },
        "original_transcript": payload.original_transcript,
        "accepted_reply": payload.accepted_reply,
    }
    try:
        summary = request.app.state.inference_coordinator.execute(
            "call_assistant",
            "call_summary",
            snapshot,
            lambda: CallAssistant(
                session, snapshot.provider
            ).summary(call_context),
        )
    except InferenceQueueFull as exc:
        audit.record(
            "call.summary.failed",
            "queue_full",
            "Call summary queue full",
            snapshot.mode,
            payload.session_id,
            int((perf_counter() - started) * 1000),
        )
        raise HTTPException(
            status_code=429,
            detail="本地模型请求较多，请稍后重试。",
        ) from exc
    except InferenceTimedOut as exc:
        audit.record(
            "call.summary.failed",
            "timeout",
            "Call summary timed out",
            snapshot.mode,
            payload.session_id,
            int((perf_counter() - started) * 1000),
        )
        raise HTTPException(
            status_code=503,
            detail="本地模型响应超时，请稍后重试。",
        ) from exc
    except ProviderUnavailable as exc:
        audit.record(
            "call.summary.failed",
            "failed",
            "Call summary provider unavailable",
            snapshot.mode,
            payload.session_id,
            int((perf_counter() - started) * 1000),
        )
        raise HTTPException(
            status_code=503, detail="模型服务暂不可用，请稍后重试。"
        ) from exc
    assistant.save_summary(call_session, payload.accepted_reply, summary)
    audit.record(
        "call.summary.completed",
        "completed",
        "Call summary completed",
        snapshot.mode,
        payload.session_id,
        int((perf_counter() - started) * 1000),
    )
    return summary
