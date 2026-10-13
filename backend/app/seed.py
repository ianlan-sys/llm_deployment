import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    CallScenario,
    CallTemplate,
    KnowledgeChunk,
    KnowledgeSource,
    ReviewRule,
)

DATA_DIR = Path(__file__).parent / "data"


def _load_json(filename: str) -> list[dict]:
    return json.loads((DATA_DIR / filename).read_text(encoding="utf-8"))


def _stable_id(entity_type: str, fixture_id: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"local-llm-web-demo/{entity_type}/{fixture_id}"))


def load_call_scenarios() -> list[CallScenario]:
    return [
        CallScenario(**(item | {"id": _stable_id("call-scenario", item["id"])}))
        for item in _load_json("call_scenarios.json")
    ]


def load_call_templates() -> list[CallTemplate]:
    return [
        CallTemplate(
            **(
                item
                | {
                    "id": _stable_id("call-template", item["id"]),
                    "scenario_id": _stable_id("call-scenario", item["scenario_id"]),
                }
            )
        )
        for item in _load_json("call_templates.json")
    ]


def load_knowledge_sources() -> tuple[list[KnowledgeSource], list[KnowledgeChunk]]:
    sources: list[KnowledgeSource] = []
    chunks: list[KnowledgeChunk] = []
    for item in _load_json("knowledge_sources.json"):
        chunk_data = item.pop("chunks")
        source_id = _stable_id("knowledge-source", item["id"])
        sources.append(
            KnowledgeSource(
                **(
                    item
                    | {
                        "id": source_id,
                        "reviewed_at": datetime(
                            2026, 7, 1, tzinfo=timezone.utc
                        ),
                        "reviewed_by": "system-seed",
                        "parse_warnings": [],
                    }
                )
            )
        )
        chunks.extend(
            KnowledgeChunk(
                source_id=source_id,
                **(chunk | {"id": _stable_id("knowledge-chunk", chunk["id"])}),
            )
            for chunk in chunk_data
        )
    return sources, chunks


def load_recommended_questions() -> list[str]:
    return _load_json("recommended_questions.json")


def load_review_rules() -> list[ReviewRule]:
    return [
        ReviewRule(**(item | {"id": _stable_id("review-rule", item["id"])}))
        for item in _load_json("review_rules.json")
    ]


def seed_demo_data(session: Session) -> None:
    """Insert the bundled fictional demo dataset once for an empty database."""
    _sync_review_rules(session)
    _sync_call_data(session)
    _sync_knowledge_data(session)
    session.commit()


def _sync_call_data(session: Session) -> None:
    scenario_fixtures = load_call_scenarios()
    template_fixtures = load_call_templates()
    scenarios = {
        row.id: row
        for row in session.scalars(
            select(CallScenario).where(
                CallScenario.id.in_([fixture.id for fixture in scenario_fixtures])
            )
        )
    }
    templates = {
        row.id: row
        for row in session.scalars(
            select(CallTemplate).where(
                CallTemplate.id.in_([fixture.id for fixture in template_fixtures])
            )
        )
    }
    for fixture in scenario_fixtures:
        row = scenarios.get(fixture.id)
        if row is None:
            session.add(fixture)
            continue
        for field in (
            "name",
            "description",
            "category",
            "priority",
            "contact_address",
            "status",
        ):
            setattr(row, field, getattr(fixture, field))
    for fixture in template_fixtures:
        row = templates.get(fixture.id)
        if row is None:
            session.add(fixture)
            continue
        for field in ("scenario_id", "title", "content", "tags", "status"):
            setattr(row, field, getattr(fixture, field))


def _sync_review_rules(session: Session) -> None:
    fixtures = load_review_rules()
    existing = {
        rule.id: rule
        for rule in session.scalars(
            select(ReviewRule).where(ReviewRule.id.in_([rule.id for rule in fixtures]))
        )
    }
    for fixture in fixtures:
        rule = existing.get(fixture.id)
        if rule is None:
            session.add(fixture)
            continue
        rule.document_type = fixture.document_type
        rule.check_item = fixture.check_item
        rule.criterion = fixture.criterion
        rule.source = fixture.source
        rule.default_risk = fixture.default_risk
        rule.status = fixture.status


def _sync_knowledge_data(session: Session) -> None:
    source_fixtures, chunk_fixtures = load_knowledge_sources()
    source_ids = [source.id for source in source_fixtures]
    chunk_ids = [chunk.id for chunk in chunk_fixtures]
    sources = {
        source.id: source
        for source in session.scalars(
            select(KnowledgeSource).where(KnowledgeSource.id.in_(source_ids))
        )
    }
    chunks = {
        chunk.id: chunk
        for chunk in session.scalars(
            select(KnowledgeChunk).where(KnowledgeChunk.id.in_(chunk_ids))
        )
    }
    source_fields = (
        "title",
        "source_type",
        "scope",
        "content",
        "status",
        "category",
        "version",
        "effective_date",
        "original_filename",
        "file_path",
        "media_type",
        "file_size",
        "content_summary",
        "parse_warnings",
        "error_summary",
        "reviewed_at",
        "reviewed_by",
    )
    for fixture in source_fixtures:
        row = sources.get(fixture.id)
        if row is None:
            session.add(fixture)
            continue
        for field in source_fields:
            setattr(row, field, getattr(fixture, field))
    for fixture in chunk_fixtures:
        row = chunks.get(fixture.id)
        if row is None:
            session.add(fixture)
            continue
        for field in ("source_id", "content", "location", "position", "index_key"):
            setattr(row, field, getattr(fixture, field))
