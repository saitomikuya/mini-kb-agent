"""Centralized process configuration.

Only this module reads environment variables. The immutable settings snapshot
is shared by the Web and Worker processes.
"""

from dataclasses import dataclass
from functools import lru_cache
import os
from pathlib import Path


DEFAULT_SESSION_MAX_AGE = 7 * 24 * 60 * 60
DEFAULT_JOB_HEARTBEAT_TIMEOUT = 60
DEFAULT_DOCUMENT_VISUAL_CONCURRENCY = 4
DEFAULT_NAVIGATION_MAX_SELECTED_DOCUMENTS = 12
DEFAULT_NAVIGATION_MAX_SELECTED_PARTS = 16
DEFAULT_NAVIGATION_MAX_ROUNDS = 2
DEFAULT_NAVIGATION_CONTEXT_WINDOW = 32_768
DEFAULT_NAVIGATION_MAX_OUTPUT_TOKENS = 2_048
DEFAULT_QUERY_ROUTER_CONTEXT_TOKENS = 131_072
DEFAULT_ANSWER_CONTEXT_TOKENS = 131_072
DEFAULT_NAVIGATION_ROOT_INPUT_TOKEN_CAP = 32_768
DEFAULT_NAVIGATION_FOLDER_INPUT_TOKEN_CAP = 65_536
DEFAULT_NAVIGATION_CONTEXT_SAFETY_PERCENT = 5
DEFAULT_LEXICAL_CANDIDATE_PARTS = 80
DEFAULT_LEXICAL_FALLBACK_PARTS = 8
DEFAULT_LEXICAL_MAX_PARTS_PER_DOCUMENT = 12
DEFAULT_NAVIGATION_LOW_CONFIDENCE_PERCENT = 45
DEFAULT_ANSWER_MAX_OUTPUT_TOKENS = 8_192
DEFAULT_ANSWER_VERBOSITY = "medium"
DEFAULT_DOCUMENT_TEXT_CHARS_PER_PART = 8_000
DEFAULT_DOCUMENT_EXCEL_ROWS_PER_PART = 200
DEFAULT_ROOT_MAX_DOCUMENT_TYPES = 12
DEFAULT_ROOT_MAX_TOPICS = 20
DEFAULT_ROOT_MAX_ENTITIES = 16
DEFAULT_ROOT_MAX_REPRESENTATIVE_TITLES = 8
DEFAULT_FOLDER_SUMMARY_TOPICS = 12


def _positive_int_from_env(name: str, default: int) -> int:
    raw_value = os.environ.get(name)
    if raw_value is None:
        return default

    try:
        value = int(raw_value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc

    if value <= 0:
        raise ValueError(f"{name} must be greater than zero")
    return value


def _choice_from_env(name: str, default: str, choices: set[str]) -> str:
    value = os.environ.get(name, default).strip().lower()
    if value not in choices:
        raise ValueError(f"{name} must be one of: {', '.join(sorted(choices))}")
    return value


@dataclass(frozen=True, slots=True)
class Settings:
    """Process configuration shared by the Web and Worker processes."""

    chat_password: str = ""
    admin_password: str = ""
    data_dir: Path = Path("/app/data")
    source_dir: Path = Path("/app/sources")
    source_display_root: Path | None = None
    timezone: str = "UTC"
    session_max_age: int = DEFAULT_SESSION_MAX_AGE
    job_heartbeat_timeout: int = DEFAULT_JOB_HEARTBEAT_TIMEOUT
    document_visual_concurrency: int = DEFAULT_DOCUMENT_VISUAL_CONCURRENCY
    navigation_max_selected_documents: int = (
        DEFAULT_NAVIGATION_MAX_SELECTED_DOCUMENTS
    )
    navigation_max_selected_parts: int = DEFAULT_NAVIGATION_MAX_SELECTED_PARTS
    navigation_max_rounds: int = DEFAULT_NAVIGATION_MAX_ROUNDS
    navigation_default_context_window: int = DEFAULT_NAVIGATION_CONTEXT_WINDOW
    navigation_default_max_output_tokens: int = (
        DEFAULT_NAVIGATION_MAX_OUTPUT_TOKENS
    )
    query_router_context_tokens: int = DEFAULT_QUERY_ROUTER_CONTEXT_TOKENS
    answer_context_tokens: int = DEFAULT_ANSWER_CONTEXT_TOKENS
    navigation_root_input_token_cap: int = (
        DEFAULT_NAVIGATION_ROOT_INPUT_TOKEN_CAP
    )
    navigation_folder_input_token_cap: int = (
        DEFAULT_NAVIGATION_FOLDER_INPUT_TOKEN_CAP
    )
    navigation_context_safety_percent: int = (
        DEFAULT_NAVIGATION_CONTEXT_SAFETY_PERCENT
    )
    lexical_candidate_parts: int = DEFAULT_LEXICAL_CANDIDATE_PARTS
    lexical_fallback_parts: int = DEFAULT_LEXICAL_FALLBACK_PARTS
    lexical_max_parts_per_document: int = (
        DEFAULT_LEXICAL_MAX_PARTS_PER_DOCUMENT
    )
    navigation_low_confidence_percent: int = (
        DEFAULT_NAVIGATION_LOW_CONFIDENCE_PERCENT
    )
    answer_max_output_tokens: int = DEFAULT_ANSWER_MAX_OUTPUT_TOKENS
    answer_verbosity: str = DEFAULT_ANSWER_VERBOSITY
    document_text_chars_per_part: int = DEFAULT_DOCUMENT_TEXT_CHARS_PER_PART
    document_excel_rows_per_part: int = DEFAULT_DOCUMENT_EXCEL_ROWS_PER_PART
    root_max_document_types: int = DEFAULT_ROOT_MAX_DOCUMENT_TYPES
    root_max_topics: int = DEFAULT_ROOT_MAX_TOPICS
    root_max_entities: int = DEFAULT_ROOT_MAX_ENTITIES
    root_max_representative_titles: int = (
        DEFAULT_ROOT_MAX_REPRESENTATIVE_TITLES
    )
    folder_summary_topics: int = DEFAULT_FOLDER_SUMMARY_TOPICS
    app_name: str = "知问"

    @property
    def database_path(self) -> Path:
        return self.data_dir / "app.db"

    @property
    def database_url(self) -> str:
        return f"sqlite:///{self.database_path}"

    @property
    def secret_path(self) -> Path:
        return self.data_dir / "app.secret"

    @property
    def queue_database_path(self) -> Path:
        return self.data_dir / "queue.db"

    @property
    def markdown_dir(self) -> Path:
        return self.data_dir / "md"

    @property
    def index_dir(self) -> Path:
        return self.data_dir / "index"

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            chat_password=os.environ.get("CHAT_PASSWORD", ""),
            admin_password=os.environ.get("ADMIN_PASSWORD", ""),
            data_dir=Path(os.environ.get("DATA_DIR", "/app/data")),
            source_dir=Path(os.environ.get("SOURCE_DIR", "/app/sources")),
            source_display_root=(
                Path(value)
                if (value := os.environ.get("SOURCE_DISPLAY_ROOT", "").strip())
                else None
            ),
            timezone=os.environ.get("TZ", "UTC"),
            session_max_age=_positive_int_from_env(
                "SESSION_MAX_AGE",
                DEFAULT_SESSION_MAX_AGE,
            ),
            job_heartbeat_timeout=_positive_int_from_env(
                "JOB_HEARTBEAT_TIMEOUT",
                DEFAULT_JOB_HEARTBEAT_TIMEOUT,
            ),
            document_visual_concurrency=_positive_int_from_env(
                "DOCUMENT_VISUAL_CONCURRENCY",
                DEFAULT_DOCUMENT_VISUAL_CONCURRENCY,
            ),
            navigation_max_selected_documents=_positive_int_from_env(
                "NAVIGATION_MAX_SELECTED_DOCUMENTS",
                DEFAULT_NAVIGATION_MAX_SELECTED_DOCUMENTS,
            ),
            navigation_max_selected_parts=_positive_int_from_env(
                "NAVIGATION_MAX_SELECTED_PARTS",
                DEFAULT_NAVIGATION_MAX_SELECTED_PARTS,
            ),
            navigation_max_rounds=_positive_int_from_env(
                "NAVIGATION_MAX_ROUNDS",
                DEFAULT_NAVIGATION_MAX_ROUNDS,
            ),
            navigation_default_context_window=_positive_int_from_env(
                "NAVIGATION_DEFAULT_CONTEXT_WINDOW",
                DEFAULT_NAVIGATION_CONTEXT_WINDOW,
            ),
            navigation_default_max_output_tokens=_positive_int_from_env(
                "NAVIGATION_DEFAULT_MAX_OUTPUT_TOKENS",
                DEFAULT_NAVIGATION_MAX_OUTPUT_TOKENS,
            ),
            query_router_context_tokens=_positive_int_from_env(
                "QUERY_ROUTER_CONTEXT_TOKENS",
                DEFAULT_QUERY_ROUTER_CONTEXT_TOKENS,
            ),
            answer_context_tokens=_positive_int_from_env(
                "ANSWER_CONTEXT_TOKENS",
                DEFAULT_ANSWER_CONTEXT_TOKENS,
            ),
            navigation_root_input_token_cap=_positive_int_from_env(
                "NAVIGATION_ROOT_INPUT_TOKEN_CAP",
                DEFAULT_NAVIGATION_ROOT_INPUT_TOKEN_CAP,
            ),
            navigation_folder_input_token_cap=_positive_int_from_env(
                "NAVIGATION_FOLDER_INPUT_TOKEN_CAP",
                DEFAULT_NAVIGATION_FOLDER_INPUT_TOKEN_CAP,
            ),
            navigation_context_safety_percent=_positive_int_from_env(
                "NAVIGATION_CONTEXT_SAFETY_PERCENT",
                DEFAULT_NAVIGATION_CONTEXT_SAFETY_PERCENT,
            ),
            lexical_candidate_parts=_positive_int_from_env(
                "LEXICAL_CANDIDATE_PARTS",
                DEFAULT_LEXICAL_CANDIDATE_PARTS,
            ),
            lexical_fallback_parts=_positive_int_from_env(
                "LEXICAL_FALLBACK_PARTS",
                DEFAULT_LEXICAL_FALLBACK_PARTS,
            ),
            lexical_max_parts_per_document=_positive_int_from_env(
                "LEXICAL_MAX_PARTS_PER_DOCUMENT",
                DEFAULT_LEXICAL_MAX_PARTS_PER_DOCUMENT,
            ),
            navigation_low_confidence_percent=_positive_int_from_env(
                "NAVIGATION_LOW_CONFIDENCE_PERCENT",
                DEFAULT_NAVIGATION_LOW_CONFIDENCE_PERCENT,
            ),
            answer_max_output_tokens=_positive_int_from_env(
                "ANSWER_MAX_OUTPUT_TOKENS",
                DEFAULT_ANSWER_MAX_OUTPUT_TOKENS,
            ),
            answer_verbosity=_choice_from_env(
                "ANSWER_VERBOSITY",
                DEFAULT_ANSWER_VERBOSITY,
                {"low", "medium", "high"},
            ),
            document_text_chars_per_part=_positive_int_from_env(
                "DOCUMENT_TEXT_CHARS_PER_PART",
                DEFAULT_DOCUMENT_TEXT_CHARS_PER_PART,
            ),
            document_excel_rows_per_part=_positive_int_from_env(
                "DOCUMENT_EXCEL_ROWS_PER_PART",
                DEFAULT_DOCUMENT_EXCEL_ROWS_PER_PART,
            ),
            root_max_document_types=_positive_int_from_env(
                "ROOT_MAX_DOCUMENT_TYPES",
                DEFAULT_ROOT_MAX_DOCUMENT_TYPES,
            ),
            root_max_topics=_positive_int_from_env(
                "ROOT_MAX_TOPICS",
                DEFAULT_ROOT_MAX_TOPICS,
            ),
            root_max_entities=_positive_int_from_env(
                "ROOT_MAX_ENTITIES",
                DEFAULT_ROOT_MAX_ENTITIES,
            ),
            root_max_representative_titles=_positive_int_from_env(
                "ROOT_MAX_REPRESENTATIVE_TITLES",
                DEFAULT_ROOT_MAX_REPRESENTATIVE_TITLES,
            ),
            folder_summary_topics=_positive_int_from_env(
                "FOLDER_SUMMARY_TOPICS",
                DEFAULT_FOLDER_SUMMARY_TOPICS,
            ),
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return one immutable settings snapshot for the current process."""
    return Settings.from_env()
