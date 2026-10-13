from collections.abc import Iterator
from dataclasses import dataclass
from typing import Protocol

from app.schemas import (
    CallSummary,
    ContextChunk,
    KnowledgeAnswer,
    ProviderHealth,
    ReviewFindingInput,
    ReviewRuleInput,
)


class ProviderUnavailable(RuntimeError):
    """Raised when a configured model provider cannot serve a request."""


@dataclass(frozen=True)
class ProviderUsage:
    input_tokens: int | None
    output_tokens: int | None


class AIProvider(Protocol):
    @property
    def last_usage(self) -> ProviderUsage: ...

    def reset_usage(self) -> None: ...

    def health_check(self) -> ProviderHealth: ...

    def stream_call_suggestion(self, call_context: dict, templates: list[dict]) -> Iterator[str]: ...

    def generate_call_summary(self, call_context: dict) -> CallSummary: ...

    def answer_with_context(self, question: str, chunks: list[ContextChunk]) -> KnowledgeAnswer: ...

    def stream_answer_with_context(
        self, question: str, chunks: list[ContextChunk]
    ) -> Iterator[str]: ...

    def review_section(
        self, document_type: str, section: str, rules: list[ReviewRuleInput]
    ) -> list[ReviewFindingInput]: ...
