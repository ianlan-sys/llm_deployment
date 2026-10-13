from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.migrations import run_schema_migrations
from app.models import KnowledgeSource


def test_migrates_pre_upgrade_database_without_losing_sources(tmp_path):
    url = f"sqlite:///{tmp_path / 'legacy.db'}"
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE knowledge_sources (id VARCHAR(36) PRIMARY KEY, title VARCHAR(200) NOT NULL, "
            "source_type VARCHAR(50) NOT NULL, scope VARCHAR(150) NOT NULL, content TEXT NOT NULL, "
            "status VARCHAR(30) NOT NULL, updated_at DATETIME NOT NULL)"
        )
        connection.exec_driver_sql(
            "INSERT INTO knowledge_sources VALUES "
            "('legacy-source','旧资料','operation_manual','全局','保留内容','active','2026-01-01')"
        )

    run_schema_migrations(engine)
    run_schema_migrations(engine)

    columns = {
        row[1]
        for row in engine.connect().exec_driver_sql("PRAGMA table_info(knowledge_sources)")
    }
    assert {
        "category",
        "version",
        "effective_date",
        "file_path",
        "media_type",
        "file_size",
        "content_summary",
        "reviewed_at",
        "reviewed_by",
    } <= columns
    with Session(engine) as session:
        source = session.get(KnowledgeSource, "legacy-source")
        assert source is not None
        assert source.content == "保留内容"
        assert (
            source.category,
            source.version,
            source.effective_date,
            source.file_path,
            source.media_type,
            source.file_size,
            source.content_summary,
            source.reviewed_at,
            source.reviewed_by,
        ) == (None, None, None, None, None, None, None, None, None)
    engine.dispose()


def test_migrations_create_persistence_tables(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'fresh.db'}")

    run_schema_migrations(engine)

    tables = {
        row[0]
        for row in engine.connect().exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    assert {
        "auth_sessions",
        "login_attempts",
        "feature_provider_settings",
        "model_request_metrics",
        "resource_samples",
    } <= tables
    engine.dispose()
