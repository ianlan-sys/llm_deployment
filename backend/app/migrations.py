from sqlalchemy import Engine

from app.models import Base


KNOWLEDGE_SOURCE_COLUMNS = {
    "category": "VARCHAR(100)",
    "version": "VARCHAR(50)",
    "effective_date": "DATE",
    "original_filename": "VARCHAR(255)",
    "file_path": "VARCHAR(500)",
    "media_type": "VARCHAR(150)",
    "file_size": "INTEGER",
    "content_summary": "TEXT",
    "parse_warnings": "JSON",
    "error_summary": "TEXT",
    "reviewed_at": "DATETIME",
    "reviewed_by": "VARCHAR(100)",
    "created_at": "DATETIME",
}


def run_schema_migrations(engine: Engine) -> None:
    """Create current tables and add nullable columns to legacy SQLite databases."""
    Base.metadata.create_all(engine)
    if engine.dialect.name != "sqlite":
        return

    with engine.begin() as connection:
        existing = {
            row[1]
            for row in connection.exec_driver_sql("PRAGMA table_info(knowledge_sources)")
        }
        for name, sql_type in KNOWLEDGE_SOURCE_COLUMNS.items():
            if name not in existing:
                connection.exec_driver_sql(
                    f"ALTER TABLE knowledge_sources ADD COLUMN {name} {sql_type}"
                )
        connection.exec_driver_sql(
            "UPDATE knowledge_sources "
            "SET parse_warnings = '[]' WHERE parse_warnings IS NULL"
        )
        connection.exec_driver_sql(
            "UPDATE knowledge_sources "
            "SET created_at = COALESCE(updated_at, CURRENT_TIMESTAMP) "
            "WHERE created_at IS NULL"
        )
