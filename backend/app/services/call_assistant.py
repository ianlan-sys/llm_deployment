from collections.abc import Iterator

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import CallScenario, CallSession, CallTemplate
from app.providers.base import AIProvider
from app.schemas import (
    CallScenarioResponse,
    CallSessionResponse,
    CallSummaryResponse,
    CallTemplateReference,
)


class CallAssistant:
    """Retrieves approved scripts and delegates text generation to the active provider."""

    def __init__(self, session: Session, provider: AIProvider | None = None):
        self.session = session
        self.provider = provider

    def scenarios(self) -> list[CallScenarioResponse]:
        scenarios = self.session.scalars(
            select(CallScenario).where(CallScenario.status == "active").order_by(CallScenario.name)
        )
        return [CallScenarioResponse.model_validate(scenario) for scenario in scenarios]

    def suggestion_stream(
        self, scenario_id: str, transcript: str
    ) -> tuple[CallScenario, list[CallTemplateReference], Iterator[str]]:
        scenario = self.session.get(CallScenario, scenario_id)
        if scenario is None or scenario.status != "active":
            raise LookupError("Call scenario was not found")
        templates = self._templates(scenario_id, transcript)
        references = [
            CallTemplateReference(
                id=template.id,
                title=template.title,
                content=template.content,
            )
            for template in templates
        ]
        provider_templates = [
            reference.model_dump() | {"tags": template.tags}
            for reference, template in zip(references, templates, strict=True)
        ]
        return (
            scenario,
            references,
            self._provider().stream_call_suggestion(
                {
                    "scenario_id": scenario.id,
                    "scenario_name": scenario.name,
                    "intent": transcript,
                    "transcript": transcript,
                },
                provider_templates,
            ),
        )

    def summary(self, call_context: dict) -> CallSummaryResponse:
        return self._provider().generate_call_summary(call_context)

    def create_session(
        self,
        scenario_id: str,
        transcript: str,
        references: list[CallTemplateReference],
        provider_mode: str,
        request_id: str,
    ) -> CallSession:
        call_session = CallSession(
            scenario_id=scenario_id,
            transcript=transcript,
            template_references=[reference.model_dump() for reference in references],
            provider_mode=provider_mode,
            request_id=request_id,
        )
        self.session.add(call_session)
        self.session.commit()
        self.session.refresh(call_session)
        return call_session

    def update_stream(
        self,
        session_id: str,
        suggestion: str,
        ttft_ms: int | None,
        elapsed_ms: int,
        status: str,
    ) -> None:
        call_session = self.required_session(session_id)
        call_session.suggestion = suggestion
        call_session.ttft_ms = ttft_ms
        call_session.elapsed_ms = elapsed_ms
        call_session.status = status
        self.session.commit()

    def adopt(self, session_id: str, accepted_reply: str) -> CallSessionResponse:
        call_session = self.required_session(session_id)
        call_session.accepted_reply = accepted_reply
        call_session.adoption_status = (
            "accepted" if accepted_reply == call_session.suggestion else "edited"
        )
        self.session.commit()
        self.session.refresh(call_session)
        return CallSessionResponse.model_validate(call_session)

    def save_summary(
        self,
        call_session: CallSession,
        accepted_reply: str,
        summary: CallSummaryResponse,
    ) -> None:
        call_session.accepted_reply = accepted_reply
        call_session.adoption_status = (
            "accepted" if accepted_reply == call_session.suggestion else "edited"
        )
        call_session.summary = summary.model_dump()
        call_session.status = "summarized"
        self.session.commit()

    def required_session(self, session_id: str) -> CallSession:
        call_session = self.session.get(CallSession, session_id)
        if call_session is None:
            raise LookupError("Call session was not found")
        return call_session

    def _provider(self) -> AIProvider:
        if self.provider is None:
            from app.providers.base import ProviderUnavailable

            raise ProviderUnavailable("The active provider is unavailable")
        return self.provider

    def _templates(self, scenario_id: str, transcript: str) -> list[CallTemplate]:
        templates = list(
            self.session.scalars(
                select(CallTemplate)
                .where(CallTemplate.scenario_id == scenario_id, CallTemplate.status == "approved")
                .order_by(CallTemplate.title)
            )
        )
        matching = [template for template in templates if self._matches(template, transcript)]
        return matching or templates

    @staticmethod
    def _matches(template: CallTemplate, transcript: str) -> bool:
        searchable = f"{template.title} {template.content} {' '.join(template.tags or [])}"
        return any(tag and tag in transcript for tag in template.tags or []) or transcript in searchable
