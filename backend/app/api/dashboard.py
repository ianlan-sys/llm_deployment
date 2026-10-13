from datetime import datetime, time, timedelta, timezone

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_session
from app.models import AuditEvent, ReviewTask
from app.schemas import AuditEventResponse, DashboardResponse
from app.services.review_pipeline import ACTIVE_TASK_STATES


router = APIRouter(tags=["dashboard"])

_SAFE_AUDIT_SUMMARIES = frozenset(
    {
        "Call suggestion stream cancelled",
        "Call suggestion stream completed",
        "Call suggestion provider unavailable",
        "Call summary completed",
        "Call summary provider unavailable",
        "Knowledge query processed",
        "Knowledge provider unavailable",
        "Call adoption updated",
        "Provider settings updated",
        "Provider connection tested",
        "Feature provider settings updated",
        "Feature provider connection tested",
        "Review submitted",
        "Review retried",
        "Review cancelled",
        "Review interrupted",
        "Review stage updated",
        "Review completed",
        "Review failed",
        "Demo login succeeded",
        "Demo login failed",
        "Demo login rate limited",
        "Demo logout succeeded",
    }
)
_REQUEST_EVENT_TYPES = frozenset(
    {
        "call.suggestion.completed",
        "call.suggestion.cancelled",
        "call.suggestion.failed",
        "call.adoption.updated",
        "call.summary.completed",
        "call.summary.failed",
        "knowledge.query.completed",
        "knowledge.query.no_evidence",
        "knowledge.query.failed",
        "settings.provider.updated",
        "settings.provider.tested",
        "settings.feature.updated",
        "settings.feature.tested",
        "review.submitted",
        "review.retried",
        "review.cancelled",
    }
)
_REDACTED_SUMMARY = "已脱敏的操作摘要"


@router.get("/api/dashboard", response_model=DashboardResponse)
def dashboard(
    request: Request, session: Session = Depends(get_session)
) -> DashboardResponse:
    start, end = _utc_day_bounds()
    events = list(
        session.scalars(
            select(AuditEvent).where(
                AuditEvent.created_at >= start,
                AuditEvent.created_at < end,
            )
        )
    )
    request_events = [
        event for event in events if event.event_type in _REQUEST_EVENT_TYPES
    ]
    elapsed = [
        event.elapsed_ms for event in request_events if event.elapsed_ms is not None
    ]
    knowledge_events = [
        event for event in events if event.event_type.startswith("knowledge.query.")
    ]
    completed_knowledge_events = [
        event
        for event in knowledge_events
        if event.event_type == "knowledge.query.completed"
    ]
    review_counts = _review_counts(session)
    active_review_count = sum(
        count
        for task_status, count in review_counts.items()
        if task_status in ACTIVE_TASK_STATES
    )

    return DashboardResponse(
        service="ok",
        mode=request.app.state.feature_provider_router.snapshot("call_assistant").mode,
        today_requests=len(request_events),
        average_latency_ms=round(sum(elapsed) / len(elapsed)) if elapsed else 0,
        citation_coverage=round(
            (len(completed_knowledge_events) / len(knowledge_events)) * 100
        )
        if knowledge_events
        else 0,
        review_counts=review_counts,
        queues={
            "realtime": 0,
            "long_document": active_review_count,
        },
    )


@router.get("/api/audit-events", response_model=list[AuditEventResponse])
def audit_events(
    limit: int = Query(default=50, ge=1, le=100),
    session: Session = Depends(get_session),
) -> list[AuditEventResponse]:
    events = session.scalars(
        select(AuditEvent)
        .order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc())
        .limit(limit)
    ).all()
    return [_audit_event_response(event) for event in events]


def _utc_day_bounds() -> tuple[datetime, datetime]:
    start = datetime.combine(datetime.now(timezone.utc).date(), time.min, timezone.utc)
    return start, start + timedelta(days=1)


def _review_counts(session: Session) -> dict[str, int]:
    tasks = session.scalars(select(ReviewTask.status)).all()
    counts: dict[str, int] = {}
    for task_status in tasks:
        counts[task_status] = counts.get(task_status, 0) + 1
    return counts


def _audit_event_response(event: AuditEvent) -> AuditEventResponse:
    return AuditEventResponse(
        id=event.id,
        event_type=event.event_type,
        request_summary=_safe_request_summary(event.request_summary),
        reference_id=event.reference_id,
        mode=event.mode,
        elapsed_ms=event.elapsed_ms,
        result_status=event.result_status,
        created_at=event.created_at,
    )


def _safe_request_summary(value: str) -> str:
    """Only emit known fixed summaries, never arbitrary persisted input."""
    return value if value in _SAFE_AUDIT_SUMMARIES else _REDACTED_SUMMARY
