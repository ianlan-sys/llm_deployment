from app.config import Settings


def test_settings_resolves_default_database_url_from_runtime_dir(tmp_path):
    settings = Settings(runtime_dir=tmp_path)

    assert settings.resolved_database_url() == f"sqlite:///{tmp_path / 'demo.db'}"
