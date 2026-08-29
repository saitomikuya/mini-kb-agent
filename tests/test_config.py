"""Central settings boundary tests."""

from pathlib import Path

from app.config import Settings


def test_settings_load_supported_environment_variables(monkeypatch) -> None:
    monkeypatch.setenv("CHAT_PASSWORD", "chat-secret")
    monkeypatch.setenv("ADMIN_PASSWORD", "admin-secret")
    monkeypatch.setenv("DATA_DIR", "/tmp/mini-kb-data")
    monkeypatch.setenv("SOURCE_DIR", "/tmp/mini-kb-sources")
    monkeypatch.setenv("SOURCE_DISPLAY_ROOT", "/host/knowledge-sources")
    monkeypatch.setenv("TZ", "Asia/Shanghai")
    monkeypatch.setenv("SESSION_MAX_AGE", "1234")
    monkeypatch.setenv("JOB_HEARTBEAT_TIMEOUT", "45")
    monkeypatch.setenv("NAVIGATION_MAX_SELECTED_DOCUMENTS", "6")
    monkeypatch.setenv("NAVIGATION_MAX_ROUNDS", "3")
    monkeypatch.setenv("NAVIGATION_DEFAULT_CONTEXT_WINDOW", "64000")
    monkeypatch.setenv("NAVIGATION_DEFAULT_MAX_OUTPUT_TOKENS", "4096")
    monkeypatch.setenv("NAVIGATION_ROOT_INPUT_TOKEN_CAP", "24000")
    monkeypatch.setenv("NAVIGATION_FOLDER_INPUT_TOKEN_CAP", "48000")
    monkeypatch.setenv("LEXICAL_MAX_PARTS_PER_DOCUMENT", "9")

    settings = Settings.from_env()

    assert settings.chat_password == "chat-secret"
    assert settings.admin_password == "admin-secret"
    assert settings.data_dir == Path("/tmp/mini-kb-data")
    assert settings.source_dir == Path("/tmp/mini-kb-sources")
    assert settings.source_display_root == Path("/host/knowledge-sources")
    assert settings.timezone == "Asia/Shanghai"
    assert settings.session_max_age == 1234
    assert settings.job_heartbeat_timeout == 45
    assert settings.navigation_max_selected_documents == 6
    assert settings.navigation_max_rounds == 3
    assert settings.navigation_default_context_window == 64000
    assert settings.navigation_default_max_output_tokens == 4096
    assert settings.navigation_root_input_token_cap == 24000
    assert settings.navigation_folder_input_token_cap == 48000
    assert settings.lexical_max_parts_per_document == 9
    assert settings.database_path == Path(
        "/tmp/mini-kb-data/app.db"
    )
    assert settings.queue_database_path == Path("/tmp/mini-kb-data/queue.db")


def test_empty_source_display_root_is_disabled(monkeypatch) -> None:
    monkeypatch.setenv("SOURCE_DISPLAY_ROOT", "")

    assert Settings.from_env().source_display_root is None
