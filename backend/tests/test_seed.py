import ipaddress
import json
import re
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.models import (
    CallScenario,
    CallTemplate,
    KnowledgeChunk,
    KnowledgeSource,
    ReviewRule,
)
from app.seed import seed_demo_data
from app.seed import DATA_DIR


EXPECTED_KNOWLEDGE_CATEGORIES = {
    "网络与 VLAN、路由、防火墙",
    "Linux/Windows 服务器巡检",
    "数据库、备份与恢复",
    "账号权限与安全基线",
    "故障分级与应急处置",
    "变更管理、维护窗口和回退方案",
}


def test_seed_is_idempotent(db_session):
    seed_demo_data(db_session)
    seed_demo_data(db_session)

    assert db_session.scalar(select(func.count()).select_from(CallScenario)) == 4
    assert db_session.scalar(select(func.count()).select_from(CallTemplate)) == 12
    assert db_session.scalar(select(func.count()).select_from(KnowledgeSource)) == 30
    assert db_session.scalar(select(func.count()).select_from(ReviewRule)) == 24


def test_seeded_ids_are_uuid_text_and_foreign_references_are_valid(db_session):
    seed_demo_data(db_session)

    scenarios = list(db_session.scalars(select(CallScenario)))
    templates = list(db_session.scalars(select(CallTemplate)))
    sources = list(db_session.scalars(select(KnowledgeSource)))
    chunks = list(db_session.scalars(select(KnowledgeChunk)))
    rules = list(db_session.scalars(select(ReviewRule)))

    for record in [*scenarios, *templates, *sources, *chunks, *rules]:
        assert str(UUID(record.id)) == record.id

    scenario_ids = {scenario.id for scenario in scenarios}
    source_ids = {source.id for source in sources}
    assert {template.scenario_id for template in templates} <= scenario_ids
    assert {chunk.source_id for chunk in chunks} <= source_ids


def test_lifespan_creates_and_seeds_an_empty_database(tmp_path):
    database_url = f"sqlite:///{tmp_path / 'demo.db'}"
    settings = Settings(runtime_dir=tmp_path, database_url=database_url)

    with TestClient(create_app(settings)) as client:
        assert client.get("/api/health").status_code == 200

    engine = create_engine(database_url, connect_args={"check_same_thread": False})
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(CallScenario)) == 4
    engine.dispose()


def test_seed_upgrades_review_rule_applicability_without_adding_rows(db_session):
    seed_demo_data(db_session)
    rule = db_session.scalar(
        select(ReviewRule).where(ReviewRule.check_item == "架构说明")
    )
    rule.document_type = "technical_proposal"
    db_session.commit()

    seed_demo_data(db_session)

    db_session.refresh(rule)
    assert rule.document_type == "design_document"
    assert db_session.scalar(select(func.count()).select_from(ReviewRule)) == 24


def test_seeded_knowledge_has_exact_categories_and_recommended_questions(db_session):
    seed_demo_data(db_session)

    sources = list(db_session.scalars(select(KnowledgeSource)))
    questions = json.loads(
        (DATA_DIR / "recommended_questions.json").read_text(encoding="utf-8")
    )

    assert 28 <= len(sources) <= 32
    assert {source.category for source in sources} == EXPECTED_KNOWLEDGE_CATEGORIES
    assert all(source.status == "active" for source in sources)
    assert all(source.reviewed_by == "system-seed" for source in sources)
    assert len(questions) == 10
    assert all(isinstance(question, str) and question.strip() for question in questions)


def test_seeded_knowledge_uses_only_reserved_addresses(db_session):
    seed_demo_data(db_session)
    sources = list(db_session.scalars(select(KnowledgeSource)))
    text = "\n".join(
        [source.title + "\n" + source.content for source in sources]
        + [chunk.content for source in sources for chunk in source.chunks]
    )

    for candidate in re.findall(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", text):
        address = ipaddress.ip_address(candidate)
        assert (
            address in ipaddress.ip_network("192.0.2.0/24")
            or address in ipaddress.ip_network("198.51.100.0/24")
            or address in ipaddress.ip_network("203.0.113.0/24")
        )


def test_seed_synchronizes_bundled_knowledge_without_duplicate_ids(db_session):
    seed_demo_data(db_session)
    source = db_session.scalar(
        select(KnowledgeSource).where(KnowledgeSource.title == "核心节点A网络连通性手册")
    )
    original_id = source.id
    source.category = "错误分类"
    source.status = "archived"
    db_session.commit()

    seed_demo_data(db_session)

    db_session.refresh(source)
    assert source.id == original_id
    assert source.category == "网络与 VLAN、路由、防火墙"
    assert source.status == "active"
    assert db_session.scalar(select(func.count()).select_from(KnowledgeSource)) == 30
