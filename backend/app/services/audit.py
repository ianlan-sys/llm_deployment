from app.models import AuditEvent


class AuditService:
    """Persists concise, non-sensitive operation outcomes."""

    def __init__(self, session):
        self.session = session

    def add(
        self,
        event_type: str,
        result_status: str,
        request_summary: str,
        mode: str,
        reference_id: str | None = None,
        elapsed_ms: int | None = None,
    ) -> None:
        """Add an audit event without committing the caller's transaction."""
        self.session.add(
            AuditEvent(
                event_type=event_type,
                request_summary=request_summary,
                reference_id=reference_id,
                mode=mode,
                elapsed_ms=elapsed_ms,
                result_status=result_status,
            )
        )

    def record(
        self,
        event_type: str,
        result_status: str,
        request_summary: str,
        mode: str,
        reference_id: str | None = None,
        elapsed_ms: int | None = None,
    ) -> None:
        self.add(
            event_type,
            result_status,
            request_summary,
            mode,
            reference_id,
            elapsed_ms,
        )
        self.session.commit()
