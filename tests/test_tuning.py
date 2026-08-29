"""Persisted knowledge-tuning API and runtime-overlay tests."""

from fastapi.testclient import TestClient

from app.config import Settings
from app.db import Base
from app.main import create_app
from app.services.tuning import effective_settings


def test_admin_can_persist_apply_and_reset_tuning(tmp_path) -> None:
    settings = Settings(
        admin_password="admin-tuning-test",
        data_dir=tmp_path / "data",
        source_dir=tmp_path / "sources",
    )
    settings.data_dir.mkdir()
    settings.source_dir.mkdir()
    application = create_app(settings)
    Base.metadata.create_all(application.state.database_engine)

    with TestClient(application) as client:
        assert client.post(
            "/api/auth/admin/login",
            json={"password": "admin-tuning-test"},
        ).status_code == 200
        defaults = client.get("/api/admin/tuning")
        assert defaults.status_code == 200
        payload = defaults.json()
        assert payload["query_router_context_tokens"] == 131_072
        assert payload["answer_context_tokens"] == 131_072
        assert payload["updated_at"] is None

        payload.update(
            {
                "query_router_context_tokens": 65_536,
                "answer_context_tokens": 98_304,
                "answer_max_output_tokens": 6_144,
                "navigation_max_selected_parts": 20,
                "lexical_fallback_parts": 10,
                "answer_verbosity": "high",
            }
        )
        payload.pop("updated_at")
        saved = client.put("/api/admin/tuning", json=payload)
        assert saved.status_code == 200, saved.text
        assert saved.json()["updated_at"] is not None

        with application.state.session_factory() as session:
            active = effective_settings(session, settings)
        assert active.query_router_context_tokens == 65_536
        assert active.answer_context_tokens == 98_304
        assert active.answer_max_output_tokens == 6_144
        assert active.navigation_max_selected_parts == 20
        assert active.answer_verbosity == "high"

        reset = client.delete("/api/admin/tuning")
        assert reset.status_code == 200
        assert reset.json()["updated_at"] is None
        assert reset.json()["answer_verbosity"] == "medium"


def test_tuning_rejects_inconsistent_candidate_limits(tmp_path) -> None:
    settings = Settings(
        admin_password="admin-tuning-test",
        data_dir=tmp_path / "data",
        source_dir=tmp_path / "sources",
    )
    settings.data_dir.mkdir()
    settings.source_dir.mkdir()
    application = create_app(settings)
    Base.metadata.create_all(application.state.database_engine)

    with TestClient(application) as client:
        client.post(
            "/api/auth/admin/login",
            json={"password": "admin-tuning-test"},
        )
        payload = client.get("/api/admin/tuning").json()
        payload.pop("updated_at")
        payload["lexical_candidate_parts"] = 5
        payload["lexical_max_parts_per_document"] = 6

        response = client.put("/api/admin/tuning", json=payload)

        assert response.status_code == 422
