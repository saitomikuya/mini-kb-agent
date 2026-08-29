"""Database models, migration, and SQLite connection tests."""

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect

from app.config import Settings, get_settings
from app.db import Base, build_engine
import app.models  # noqa: F401  # Register model metadata for assertions.


EXPECTED_COLUMNS = {
    "index_generations": {
        "generation_number",
        "status",
        "root_index_path",
        "document_count",
        "created_at",
        "activated_at",
    },
    "jobs": {
        "id",
        "job_type",
        "status",
        "control_state",
        "total_items",
        "completed_items",
        "failed_items",
        "current_file_id",
        "heartbeat_at",
        "started_at",
        "finished_at",
        "paused_at",
        "paused_seconds",
        "error",
        "created_at",
    },
    "job_items": {
        "id",
        "job_id",
        "source_file_id",
        "status",
        "attempts",
        "started_at",
        "finished_at",
        "error",
        "progress_json",
    },
    "source_files": {
        "id",
        "relative_path",
        "filename",
        "extension",
        "size",
        "mtime_ns",
        "sha256",
        "source_status",
        "conversion_status",
        "index_status",
        "last_error",
        "converted_at",
        "created_at",
        "updated_at",
    },
    "api_providers": {
        "id",
        "name",
        "provider_type",
        "base_url",
        "encrypted_api_key",
        "protocol_preference",
        "extra_headers_json",
        "azure_mode",
        "azure_api_version",
        "enabled",
        "created_at",
        "updated_at",
    },
    "model_profiles": {
        "id",
        "provider_id",
        "name",
        "remote_model_name",
        "protocol_override",
        "context_window",
        "max_output_tokens",
        "reasoning_effort",
        "extra_request_json",
        "supports_text",
        "supports_vision",
        "supports_structured_output",
        "tested_protocol",
        "last_test_status",
        "last_test_latency_ms",
        "last_tested_at",
        "enabled",
        "created_at",
        "updated_at",
    },
    "model_role_bindings": {"role", "model_profile_id", "updated_at"},
    "model_role_prompt_settings": {"role", "prompts_json", "updated_at"},
    "knowledge_tuning_settings": {"id", "values_json", "updated_at"},
    "chat_sessions": {"id", "title", "created_at", "updated_at"},
    "messages": {
        "id",
        "session_id",
        "role",
        "content",
        "answer_json",
        "created_at",
    },
    "chat_events": {
        "id",
        "message_id",
        "event_type",
        "event_json",
        "created_at",
    },
}


def test_models_declare_background_job_tables() -> None:
    assert set(Base.metadata.tables) == set(EXPECTED_COLUMNS)
    for table_name, column_names in EXPECTED_COLUMNS.items():
        assert set(Base.metadata.tables[table_name].columns.keys()) == column_names


def test_sqlite_required_pragmas(tmp_path: Path) -> None:
    engine = build_engine(Settings(data_dir=tmp_path))
    try:
        with engine.connect() as connection:
            journal_mode = connection.exec_driver_sql("PRAGMA journal_mode").scalar_one()
            foreign_keys = connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one()
            busy_timeout = connection.exec_driver_sql("PRAGMA busy_timeout").scalar_one()
    finally:
        engine.dispose()

    assert journal_mode.lower() == "wal"
    assert foreign_keys == 1
    assert busy_timeout == 5000


def test_alembic_upgrade_creates_background_job_schema(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_root = Path(__file__).resolve().parents[1]
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    alembic_config = Config(str(project_root / "alembic.ini"))
    alembic_config.set_main_option(
        "script_location",
        str(project_root / "migrations"),
    )

    try:
        command.upgrade(alembic_config, "head")
    finally:
        get_settings.cache_clear()

    engine = build_engine(Settings(data_dir=tmp_path))
    try:
        inspector = inspect(engine)
        assert set(inspector.get_table_names()) == {
            "alembic_version",
            *EXPECTED_COLUMNS,
        }
        for table_name, expected_columns in EXPECTED_COLUMNS.items():
            actual_columns = {
                column["name"] for column in inspector.get_columns(table_name)
            }
            assert actual_columns == expected_columns

        message_fks = inspector.get_foreign_keys("messages")
        event_fks = inspector.get_foreign_keys("chat_events")
        assert message_fks[0]["referred_table"] == "chat_sessions"
        assert message_fks[0]["options"] == {"ondelete": "CASCADE"}
        assert event_fks[0]["referred_table"] == "messages"
        assert event_fks[0]["options"] == {"ondelete": "CASCADE"}

        profile_fks = inspector.get_foreign_keys("model_profiles")
        role_fks = inspector.get_foreign_keys("model_role_bindings")
        assert profile_fks[0]["referred_table"] == "api_providers"
        assert profile_fks[0]["options"] == {"ondelete": "CASCADE"}
        assert role_fks[0]["referred_table"] == "model_profiles"
        assert role_fks[0]["options"] == {"ondelete": "RESTRICT"}

        job_fks = inspector.get_foreign_keys("jobs")
        item_fks = inspector.get_foreign_keys("job_items")
        assert job_fks[0]["referred_table"] == "source_files"
        assert job_fks[0]["options"] == {"ondelete": "SET NULL"}
        assert {
            (foreign_key["referred_table"], foreign_key["options"]["ondelete"])
            for foreign_key in item_fks
        } == {("jobs", "CASCADE"), ("source_files", "SET NULL")}
    finally:
        engine.dispose()
