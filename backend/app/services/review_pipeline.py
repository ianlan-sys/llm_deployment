import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from typing import Literal, TypeAlias

from pydantic import TypeAdapter, ValidationError
from sqlalchemy import select, update

from app.models import ReviewFinding, ReviewRule, ReviewTask
from app.providers.base import AIProvider, ProviderUnavailable
from app.providers.feature_router import FeatureProviderSnapshot
from app.schemas import ReviewFindingInput, ReviewRuleInput
from app.services.inference import (
    InferenceCoordinator,
    InferenceQueueFull,
    InferenceTimedOut,
)
from app.services.review_parser import DocumentParseError, parse_document
from app.services.audit import AuditService


TaskStatus: TypeAlias = Literal[
    "queued",
    "parsing",
    "matching_rules",
    "reviewing",
    "summarizing",
    "completed",
    "failed",
    "cancelled",
    "interrupted",
]
ACTIVE_TASK_STATES = frozenset(
    {"queued", "parsing", "matching_rules", "reviewing", "summarizing"}
)
RETRYABLE_TASK_STATES = frozenset({"failed", "cancelled", "interrupted"})
TERMINAL_TASK_STATES = frozenset({"completed", "failed", "cancelled", "interrupted"})
_TRANSITIONS = {
    "queued": {"parsing", "failed", "cancelled", "interrupted"},
    "parsing": {"matching_rules", "failed", "cancelled", "interrupted"},
    "matching_rules": {"reviewing", "failed", "cancelled", "interrupted"},
    "reviewing": {"summarizing", "failed", "cancelled", "interrupted"},
    "summarizing": {"completed", "failed", "cancelled", "interrupted"},
    "completed": set(),
    "failed": {"queued"},
    "cancelled": {"queued"},
    "interrupted": {"queued"},
}
_FINDING_LIST = TypeAdapter(list[ReviewFindingInput])
_RISK_ORDER = {"low": 0, "medium": 1, "high": 2}
_EXPECTED_STAGE = {
    "parsing": "queued",
    "matching_rules": "parsing",
    "reviewing": "matching_rules",
    "summarizing": "reviewing",
    "completed": "summarizing",
}
_LOCAL_RULE_CHECK_ITEMS = frozenset({"安全设计", "数据保护", "性能容量", "高可用设计", "技术要求"})
_RULE_SUFFIXES = ("说明", "设计", "要求", "容量")


class InvalidTaskTransition(ValueError):
    pass


class TaskCancelled(RuntimeError):
    pass


@dataclass
class ReviewTaskState:
    status: TaskStatus
    progress: int

    def transition(self, status: TaskStatus, progress: int) -> None:
        if status not in _TRANSITIONS.get(self.status, set()):
            raise InvalidTaskTransition(
                f"cannot transition review task from {self.status} to {status}"
            )
        if not 0 <= progress <= 100:
            raise InvalidTaskTransition("progress must be between 0 and 100")
        if status != "queued" and progress < self.progress:
            raise InvalidTaskTransition("progress cannot decrease")
        if status == "completed" and progress != 100:
            raise InvalidTaskTransition("completed tasks require 100 percent progress")
        if status == "queued" and progress != 0:
            raise InvalidTaskTransition(
                "retried tasks must restart at zero percent progress"
            )
        self.status = status
        self.progress = progress


def merge_findings(findings: list[ReviewFindingInput]) -> list[ReviewFindingInput]:
    """Merge duplicates, retaining the highest-risk version for a key."""
    merged: dict[tuple[str, str], ReviewFindingInput] = {}
    for finding in findings:
        key = (finding.rule_id, re.sub(r"\s+", "", finding.location).casefold())
        current = merged.get(key)
        if current is None or _RISK_ORDER[finding.risk] > _RISK_ORDER[current.risk]:
            merged[key] = finding
    return list(merged.values())


def review_with_one_repair(
    provider: AIProvider,
    document_type: str,
    section: str,
    rules: list[ReviewRuleInput],
) -> list[ReviewFindingInput]:
    """Validate model findings and retry one malformed structured response."""
    try:
        return _validated_findings(
            provider.review_section(document_type, section, rules), rules, section
        )
    except Exception as exc:
        if not _is_structured_output_error(exc):
            raise
        repair_section = (
            f"{section}\n\n修复要求：上一次响应未通过结构化校验"
            f"（{exc}）。请仅返回符合规定 schema 的 JSON 数组，"
            "且 excerpt 必须是原文中的连续片段。"
        )
    return _validated_findings(
        provider.review_section(document_type, repair_section, rules), rules, section
    )


def _validated_findings(
    raw: object, rules: list[ReviewRuleInput], section: str
) -> list[ReviewFindingInput]:
    findings = _FINDING_LIST.validate_python(raw)
    rule_ids = {rule.id for rule in rules}
    for finding in findings:
        if finding.rule_id not in rule_ids:
            raise ValueError("finding references an inapplicable review rule")
        normalized_excerpt = re.sub(r"\s+", "", finding.excerpt)
        normalized_section = re.sub(r"\s+", "", section)
        if not normalized_excerpt or normalized_excerpt not in normalized_section:
            raise ValueError("finding excerpt is not grounded in the reviewed section")
    return findings


def _is_structured_output_error(exc: Exception) -> bool:
    if isinstance(exc, (ValidationError, ValueError)):
        return True
    return isinstance(exc, ProviderUnavailable) and "structured" in str(exc).casefold()


class ReviewPipeline:
    """Runs and persists each stage of one document review attempt."""

    def __init__(
        self,
        session_factory,
        coordinator: InferenceCoordinator,
    ):
        self.session_factory = session_factory
        self.coordinator = coordinator

    def run(
        self,
        task_id: str,
        snapshot: FeatureProviderSnapshot,
    ) -> None:
        started = perf_counter()
        mode = snapshot.mode
        try:
            self._transition(task_id, "parsing", 10, mode, started)
            with self.session_factory() as session:
                task = self._required_task(session, task_id)
                path = Path(task.file_path)
                media_type = _media_type_for(path)
            parsed = parse_document(path, media_type)

            self._transition(task_id, "matching_rules", 25, mode, started)
            with self.session_factory() as session:
                task = self._required_task(session, task_id)
                self._ensure_status(task, "matching_rules")
                document_type = task.document_type
                all_rules = self._applicable_rules(session, document_type)

            self._transition(task_id, "reviewing", 40, mode, started)
            findings: list[ReviewFindingInput] = []
            global_rules, local_rules = _split_rules(all_rules)
            recalled_by_section = [
                _recall_local_rules(section.title, section.content, local_rules)
                for section in parsed.sections
            ]
            recalled_local_ids = {
                rule.id for rules in recalled_by_section for rule in rules
            }
            completeness_rules = global_rules + [
                rule for rule in local_rules if rule.id not in recalled_local_ids
            ]
            if completeness_rules:
                full_document = "\n\n".join(section.content for section in parsed.sections)
                findings.extend(
                    finding.model_copy(update={"location": "全文"})
                    for finding in self._review(
                        snapshot,
                        document_type,
                        full_document,
                        completeness_rules,
                    )
                )
            section_count = len(parsed.sections)
            for index, (section, rules) in enumerate(
                zip(parsed.sections, recalled_by_section, strict=True)
            ):
                with self.session_factory() as session:
                    task = self._required_task(session, task_id)
                    self._ensure_status(task, "reviewing")
                section_findings = (
                    self._review(
                        snapshot,
                        document_type,
                        section.content,
                        rules,
                    )
                    if rules
                    else []
                )
                findings.extend(
                    finding.model_copy(
                        update={
                            "location": _canonical_location(
                                section.title, finding.location
                            )
                        }
                    )
                    for finding in section_findings
                )
                self._set_review_progress(
                    task_id, 40 + ((index + 1) * 45 // section_count)
                )

            self._transition(task_id, "summarizing", 90, mode, started)
            merged = merge_findings(findings)
            self._complete_with_findings(task_id, merged, mode, started)
        except TaskCancelled:
            return
        except Exception as exc:
            self._mark_failed(task_id, exc, mode, started)

    def _review(
        self,
        snapshot: FeatureProviderSnapshot,
        document_type: str,
        section: str,
        rules: list[ReviewRuleInput],
    ) -> list[ReviewFindingInput]:
        return self.coordinator.execute(
            "document_review",
            "document_review",
            snapshot,
            lambda: review_with_one_repair(
                snapshot.provider,
                document_type,
                section,
                rules,
            ),
        )

    def _transition(
        self,
        task_id: str,
        status: TaskStatus,
        progress: int,
        mode: str,
        started: float,
    ) -> None:
        with self.session_factory() as session:
            task = self._required_task(session, task_id)
            expected = _EXPECTED_STAGE[status]
            if task.status != expected:
                raise TaskCancelled()
            state = ReviewTaskState(status=task.status, progress=task.progress)
            state.transition(status, progress)
            now = datetime.now(timezone.utc)
            values = {
                "status": state.status,
                "stage": state.status,
                "progress": state.progress,
                "error_summary": None,
            }
            if status == "parsing":
                values["started_at"] = now
            result = session.execute(
                update(ReviewTask)
                .where(ReviewTask.id == task_id, ReviewTask.status == expected)
                .values(**values)
            )
            if result.rowcount != 1:
                session.rollback()
                raise TaskCancelled()
            session.commit()
            AuditService(session).record(
                f"review.stage.{status}",
                status,
                "Review stage updated",
                mode,
                task_id,
                int((perf_counter() - started) * 1000),
            )

    def _set_review_progress(self, task_id: str, progress: int) -> None:
        with self.session_factory() as session:
            result = session.execute(
                update(ReviewTask)
                .where(
                    ReviewTask.id == task_id,
                    ReviewTask.status == "reviewing",
                    ReviewTask.progress <= progress,
                )
                .values(progress=min(progress, 89))
            )
            if result.rowcount != 1:
                session.rollback()
                raise TaskCancelled()
            session.commit()

    def _complete_with_findings(
        self,
        task_id: str,
        findings: list[ReviewFindingInput],
        mode: str,
        started: float,
    ) -> None:
        with self.session_factory() as session:
            result = session.execute(
                update(ReviewTask)
                .where(
                    ReviewTask.id == task_id,
                    ReviewTask.status == "summarizing",
                )
                .values(
                    status="completed",
                    stage="completed",
                    progress=100,
                    error_summary=None,
                    completed_at=datetime.now(timezone.utc),
                )
            )
            if result.rowcount != 1:
                session.rollback()
                raise TaskCancelled()
            session.add_all(
                ReviewFinding(
                    review_task_id=task_id,
                    review_rule_id=finding.rule_id,
                    risk=finding.risk,
                    description=finding.description,
                    location=finding.location,
                    excerpt=finding.excerpt,
                    recommendation=finding.recommendation,
                )
                for finding in findings
            )
            AuditService(session).add(
                "review.completed",
                "completed",
                "Review completed",
                mode,
                task_id,
                int((perf_counter() - started) * 1000),
            )
            session.commit()

    def _mark_failed(
        self, task_id: str, exc: Exception, mode: str, started: float
    ) -> None:
        with self.session_factory() as session:
            task = session.get(ReviewTask, task_id)
            if task is None or task.status not in ACTIVE_TASK_STATES:
                return
            state = ReviewTaskState(status=task.status, progress=task.progress)
            state.transition("failed", task.progress)
            result = session.execute(
                update(ReviewTask)
                .where(ReviewTask.id == task_id, ReviewTask.status == task.status)
                .values(
                    status=state.status,
                    error_summary=_safe_error_summary(exc),
                    completed_at=datetime.now(timezone.utc),
                )
            )
            if result.rowcount != 1:
                session.rollback()
                return
            AuditService(session).add(
                "review.failed",
                "failed",
                "Review failed",
                mode,
                task_id,
                int((perf_counter() - started) * 1000),
            )
            session.commit()

    @staticmethod
    def _required_task(session, task_id: str) -> ReviewTask:
        task = session.get(ReviewTask, task_id)
        if task is None:
            raise LookupError("review task not found")
        return task

    @staticmethod
    def _ensure_status(task: ReviewTask, expected: str) -> None:
        if task.status != expected:
            raise TaskCancelled()

    @staticmethod
    def _applicable_rules(session, document_type: str) -> list[ReviewRuleInput]:
        rules = list(
            session.scalars(
                select(ReviewRule).where(
                    ReviewRule.document_type == document_type,
                    ReviewRule.status == "active",
                )
            )
        )
        return [
            ReviewRuleInput(
                id=rule.id,
                document_type=rule.document_type,
                check_item=rule.check_item,
                criterion=rule.criterion,
                source=rule.source,
                default_risk=rule.default_risk,
            )
            for rule in rules
        ]


def _media_type_for(path: Path) -> str:
    media_types = {
        ".txt": "text/plain",
        ".pdf": "application/pdf",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    }
    try:
        return media_types[path.suffix.lower()]
    except KeyError as exc:
        raise ValueError("不支持的审核文档") from exc


def _safe_error_summary(exc: Exception) -> str:
    if isinstance(exc, InferenceQueueFull):
        return "本地模型请求较多，请稍后重试。"
    if isinstance(exc, InferenceTimedOut):
        return "本地模型响应超时，请稍后重试。"
    if isinstance(exc, ProviderUnavailable):
        return "模型审核失败：模型服务不可用或返回格式无效。"
    if isinstance(exc, DocumentParseError):
        return str(exc)[:500]
    return "文档审核失败，请稍后重试。"


def _canonical_location(section_title: str, model_location: str) -> str:
    normalized_title = re.sub(r"\s+", "", section_title).casefold()
    normalized_location = re.sub(r"\s+", "", model_location).casefold()
    if normalized_title in normalized_location:
        return model_location.strip()
    return f"{section_title} / {model_location.strip()}"


def _split_rules(
    rules: list[ReviewRuleInput],
) -> tuple[list[ReviewRuleInput], list[ReviewRuleInput]]:
    global_rules = [
        rule for rule in rules if rule.check_item not in _LOCAL_RULE_CHECK_ITEMS
    ]
    local_rules = [
        rule for rule in rules if rule.check_item in _LOCAL_RULE_CHECK_ITEMS
    ]
    return global_rules, local_rules


def _recall_local_rules(
    section_title: str,
    section_content: str,
    rules: list[ReviewRuleInput],
) -> list[ReviewRuleInput]:
    searchable = re.sub(r"\s+", "", f"{section_title}\n{section_content}")
    recalled: list[ReviewRuleInput] = []
    for rule in rules:
        terms = {rule.check_item}
        for suffix in _RULE_SUFFIXES:
            stripped = rule.check_item.replace(suffix, "")
            if len(stripped) >= 2:
                terms.add(stripped)
        if any(term and term in searchable for term in terms):
            recalled.append(rule)
    return recalled
