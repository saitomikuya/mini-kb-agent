"""Final cross-layer acceptance scenarios for the frozen product."""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from io import BytesIO
import json
import os
from pathlib import Path
import re
from typing import Any

from fastapi.testclient import TestClient
import httpx
from PIL import Image
import pytest
from sqlalchemy import select

from app.config import Settings
from app.db import Base, build_engine, build_session_factory
from app.jobs import JobStatus
from app.llm.types import ModelRole
from app.main import create_app
from app.models.job import Job, JobItem
from app.models.model_config import APIProvider, ModelProfile, ModelRoleBinding
from app.models.source_file import SourceFile
from app.services.conversion_worker import DocumentConversionItemProcessor
from app.services.jobs import JobService, execute_job, recover_stale_jobs, utc_now
from app.services.secrets import APIKeyCipher
from app.services.source_files import (
    SourceFileService,
    UnsafeSourcePathError,
    safe_source_path,
)
from app.source_files import ConversionStatus, IndexStatus
from app.tasks.queue import JobTaskQueue, build_job_task_queue
import app.models  # noqa: F401  # Register all SQLAlchemy metadata.


ADMIN_PASSWORD = "final-admin-password"
CHAT_PASSWORD = "final-chat-password"


def _png_bytes(color: tuple[int, int, int]) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (32, 18), color=color).save(buffer, format="PNG")
    return buffer.getvalue()


def _run_next(queue: JobTaskQueue) -> None:
    task = queue.huey.dequeue()
    assert task is not None
    queue.huey.execute(task)


def _sse_events(response_text: str) -> list[tuple[str, dict[str, Any]]]:
    events: list[tuple[str, dict[str, Any]]] = []
    for block in response_text.strip().split("\n\n"):
        lines = block.splitlines()
        event_type = next(line[7:] for line in lines if line.startswith("event: "))
        raw_data = next(line[6:] for line in lines if line.startswith("data: "))
        events.append((event_type, json.loads(raw_data)))
    return events


class FourRoleModelServer:
    """One deterministic HTTP transport exposing four independently named models."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.conversion_outputs = [
            "产品A一页图。统一指标：10。",
            "产品B资料。统一指标：12。",
            "产品A一页图（更新版）。统一指标：11。",
        ]

    def factory(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handle))

    def handle(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        model = payload["model"]
        self.calls.append(model)
        if model == "conversion-model":
            index = self.calls.count("conversion-model") - 1
            return self._response(self.conversion_outputs[index])
        if model == "index-model":
            return self._response(json.dumps(self._index_card(payload["input"])))
        if model == "router-model":
            return self._response(json.dumps(self._route(payload["input"])))
        if model == "answer-model":
            return self._response(json.dumps(self._answer(payload["input"])))
        return httpx.Response(500, json={"error": "unexpected model"})

    @staticmethod
    def _response(text: str) -> httpx.Response:
        return httpx.Response(200, json={"output_text": text})

    @staticmethod
    def _index_card(prompt: str) -> dict[str, Any]:
        source_line = next(
            line for line in prompt.splitlines() if line.startswith("source_path: ")
        )
        source_path = json.loads(source_line.removeprefix("source_path: "))
        part_ids = [
            json.loads(value)
            for value in re.findall(r"<markdown-part id=(.+)>", prompt)
        ]
        return {
            "title": Path(source_path).stem,
            "document_type": "image",
            "summary": f"{Path(source_path).stem} source material",
            "topics": ["product", "metric"],
            "entities": [Path(source_path).stem],
            "parts": [
                {
                    "part_id": part_id,
                    "label": "Image evidence",
                    "summary": "Visible product facts and metrics.",
                }
                for part_id in part_ids
            ],
        }

    @staticmethod
    def _route(prompt: str) -> dict[str, Any]:
        question = prompt.split("user_question:\n", 1)[1].split("\n\n", 1)[0]
        wants_download = "一页图" in question
        if "phase 1" in prompt:
            root = json.loads(prompt.split("current_root_json:\n", 1)[1])
            return {
                "intent": "download" if wants_download else "answer",
                "selected_folders": [entry["folder_id"] for entry in root["folders"]],
                "display_reason": "Select matching product folders.",
                "need_more_information": False,
            }

        folder = json.loads(prompt.split("folder_index:\n", 1)[1])
        selected = []
        for document in folder["documents"]:
            if wants_download and "产品A" not in document["title"]:
                continue
            selected.append(
                {
                    "document_id": document["document_id"],
                    "part_ids": ["part-001"],
                    "display_reason": "Use the matching source image.",
                }
            )
        return {"selected_documents": selected, "confidence": 0.99}

    @staticmethod
    def _answer(raw_prompt: str) -> dict[str, Any]:
        prompt = json.loads(raw_prompt)
        metadata = prompt["source_metadata"]
        citations = []
        for source in metadata:
            part = source["parts"][0]
            citations.append(
                {
                    "document_id": source["document_id"],
                    "part_id": part["part_id"],
                    "anchor": part["anchors"][0],
                    "label": part["citation_label"],
                }
            )

        if "一页图" in prompt["user_question"]:
            product_a = next(source for source in metadata if "产品A" in source["title"])
            return {
                "answer_markdown": "已找到产品A一页图，请下载原文件。",
                "citations": [
                    citation
                    for citation in citations
                    if citation["document_id"] == product_a["document_id"]
                ],
                "conflicts": [],
                "downloads": [{"document_id": product_a["document_id"]}],
                "research_handoff": None,
            }

        conflict_values = ["10", "12"]
        return {
            "answer_markdown": "两个来源对同一指标分别给出 10 和 12，存在冲突。",
            "citations": citations,
            "conflicts": [
                {
                    "subject": "统一指标",
                    "values": [
                        {
                            "value": value,
                            "document_id": source["document_id"],
                            "anchor": source["parts"][0]["anchors"][0],
                        }
                        for value, source in zip(conflict_values, metadata, strict=True)
                    ],
                    "analysis": "来源数值不一致，未平均也未擅自选择。",
                }
            ],
            "downloads": [],
            "research_handoff": None,
        }


def _configure_four_roles(application: Any) -> None:
    cipher = application.state.api_key_cipher
    with application.state.session_factory() as session:
        for role, model_name in (
            (ModelRole.DOCUMENT_CONVERSION, "conversion-model"),
            (ModelRole.INDEX_GENERATION, "index-model"),
            (ModelRole.QUERY_ROUTER, "router-model"),
            (ModelRole.ANSWER_GENERATION, "answer-model"),
        ):
            provider = APIProvider(
                name=f"{role.value} mock provider",
                provider_type="openai_compatible",
                base_url=f"https://{role.value}.final.test/v1",
                encrypted_api_key=cipher.encrypt(f"sk-{role.value}"),
                protocol_preference="responses",
                extra_headers_json={},
                azure_mode="v1",
                enabled=True,
            )
            profile = ModelProfile(
                provider=provider,
                name=f"{role.value} mock profile",
                remote_model_name=model_name,
                context_window=32_768,
                max_output_tokens=2_048,
                supports_text=True,
                supports_vision=role is ModelRole.DOCUMENT_CONVERSION,
                supports_structured_output=True,
                tested_protocol="responses",
                last_test_status="passed",
                enabled=True,
                extra_request_json={},
            )
            session.add(profile)
            session.flush()
            session.add(
                ModelRoleBinding(role=role.value, model_profile_id=profile.id)
            )
        session.commit()


def _current_generation_files(settings: Settings) -> tuple[Path, dict[str, Any]]:
    pointer = json.loads((settings.index_dir / "current.json").read_text())
    generation_dir = (settings.index_dir / pointer["root_index_path"]).parent
    root = json.loads((generation_dir / "root.json").read_text())
    return generation_dir, root


def test_scenarios_a_to_e_full_pipeline_incremental_conflict_download_and_roles(
    tmp_path: Path,
) -> None:
    settings = Settings(
        chat_password=CHAT_PASSWORD,
        admin_password=ADMIN_PASSWORD,
        data_dir=tmp_path / "data",
        source_dir=tmp_path / "knowledge",
        session_max_age=3_600,
    )
    settings.data_dir.mkdir()
    settings.source_dir.mkdir()  # Scenario A starts from an empty mounted folder.
    engine = build_engine(settings)
    Base.metadata.create_all(engine)
    engine.dispose()

    models = FourRoleModelServer()
    http_factory: Callable[[], httpx.AsyncClient] = models.factory
    queue = build_job_task_queue(
        settings,
        model_http_client_factory=http_factory,
        retry_delay=0,
    )
    application = create_app(
        settings,
        job_task_queue=queue,
        model_http_client_factory=http_factory,
    )

    with TestClient(application) as client:
        assert client.post(
            "/api/auth/admin/login", json={"password": ADMIN_PASSWORD}
        ).status_code == 200
        assert client.get("/api/admin/files").json() == []
        _configure_four_roles(application)

        source_a = _png_bytes((10, 20, 30))
        source_b = _png_bytes((40, 50, 60))
        for filename, relative_path, body in (
            ("产品A.png", "产品/产品A.png", source_a),
            ("产品B.png", "竞品/产品B.png", source_b),
        ):
            uploaded = client.post(
                "/api/admin/files/upload",
                data={"relative_path": relative_path},
                files={"file": (filename, body, "image/png")},
            )
            assert uploaded.status_code == 201, uploaded.text

        scan = client.post("/api/admin/files/scan")
        assert scan.status_code == 200
        records = client.get("/api/admin/files").json()
        assert {record["conversion_status"] for record in records} == {"NEW"}

        conversion = client.post("/api/admin/jobs/convert-changed")
        assert conversion.status_code == 202
        assert conversion.json()["total_items"] == 2
        _run_next(queue)
        records = client.get("/api/admin/files").json()
        assert {record["conversion_status"] for record in records} == {"READY"}
        assert all(
            (settings.markdown_dir / str(record["id"]) / "manifest.json").is_file()
            for record in records
        )

        indexing = client.post("/api/admin/jobs/generate-index")
        assert indexing.status_code == 202
        _run_next(queue)
        records = client.get("/api/admin/files").json()
        assert {record["index_status"] for record in records} == {"INDEXED"}
        by_name = {record["filename"]: record for record in records}

        client.post("/api/auth/logout")
        assert client.post(
            "/api/auth/chat/login", json={"password": CHAT_PASSWORD}
        ).status_code == 200

        conflict_response = client.post(
            "/api/chat/stream",
            json={"question": "同一指标的数值是多少？"},
        )
        assert conflict_response.status_code == 200
        conflict_events = _sse_events(conflict_response.text)
        assert "conflict_detected" in [event for event, _data in conflict_events]
        conflict_answer = conflict_events[-1][1]["answer"]
        assert len(conflict_answer["citations"]) == 2
        assert [
            value["value"]
            for value in conflict_answer["conflicts"][0]["values"]
        ] == ["10", "12"]

        download_response = client.post(
            "/api/chat/stream",
            json={"question": "给我产品A一页图"},
        )
        download_events = _sse_events(download_response.text)
        download_answer = download_events[-1][1]["answer"]
        assert download_answer["downloads"] == [
            {
                "document_id": str(by_name["产品A.png"]["id"]),
                "filename": "产品A.png",
                "relative_directory": "产品",
                "relative_path": "产品/产品A.png",
                "display_path": None,
                "download_url": (
                    f"/api/files/{by_name['产品A.png']['id']}/download"
                ),
            }
        ]
        downloaded = client.get(download_answer["downloads"][0]["download_url"])
        assert downloaded.status_code == 200
        assert downloaded.content == source_a

        initial_generation, initial_root = _current_generation_files(settings)
        initial_entries = {
            entry["source_directory"]: entry for entry in initial_root["folders"]
        }
        untouched_folder = (
            initial_generation / initial_entries["竞品"]["index_path"]
        ).read_bytes()
        untouched_card = (
            settings.markdown_dir
            / str(by_name["产品B.png"]["id"])
            / "card.json"
        ).read_bytes()
        changed_card_path = (
            settings.markdown_dir
            / str(by_name["产品A.png"]["id"])
            / "card.json"
        )
        changed_card = changed_card_path.read_bytes()

        assert client.post(
            "/api/auth/admin/login", json={"password": ADMIN_PASSWORD}
        ).status_code == 200
        changed_source = settings.source_dir / "产品" / "产品A.png"
        changed_source.write_bytes(_png_bytes((70, 80, 90)))
        os.utime(changed_source, ns=(changed_source.stat().st_atime_ns, 2_000_000_000))
        changed_scan = client.post("/api/admin/files/scan")
        assert changed_scan.json()["changed"] == 1
        changed_records = client.get("/api/admin/files").json()
        changed_by_name = {record["filename"]: record for record in changed_records}
        assert changed_by_name["产品A.png"]["conversion_status"] == "CHANGED"
        assert changed_by_name["产品B.png"]["conversion_status"] == "READY"

        conversion_calls = models.calls.count("conversion-model")
        incremental = client.post("/api/admin/jobs/convert-changed")
        assert [
            item["source_file_id"] for item in incremental.json()["items"]
        ] == [by_name["产品A.png"]["id"]]
        _run_next(queue)
        assert models.calls.count("conversion-model") == conversion_calls + 1

        index_calls = models.calls.count("index-model")
        rebuild = client.post("/api/admin/jobs/generate-index")
        assert rebuild.status_code == 202
        _run_next(queue)
        assert models.calls.count("index-model") == index_calls + 1

        next_generation, next_root = _current_generation_files(settings)
        assert next_generation != initial_generation
        next_entries = {
            entry["source_directory"]: entry for entry in next_root["folders"]
        }
        assert (
            next_generation / next_entries["竞品"]["index_path"]
        ).read_bytes() == untouched_folder
        assert (
            settings.markdown_dir
            / str(by_name["产品B.png"]["id"])
            / "card.json"
        ).read_bytes() == untouched_card
        assert changed_card_path.read_bytes() != changed_card
        assert (next_generation / "root.json").is_file()

    assert models.calls.count("conversion-model") == 3
    assert models.calls.count("index-model") == 3
    assert models.calls.count("router-model") == 6
    assert models.calls.count("answer-model") == 2
    assert set(models.calls) == {
        "conversion-model",
        "index-model",
        "router-model",
        "answer-model",
    }


def test_scenario_f_real_conversion_recovers_without_repeating_completed_artifact(
    tmp_path: Path,
) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        source_dir=tmp_path / "knowledge",
        job_heartbeat_timeout=1,
    )
    settings.data_dir.mkdir()
    settings.source_dir.mkdir()
    (settings.source_dir / "a.txt").write_text("artifact A", encoding="utf-8")
    (settings.source_dir / "b.txt").write_text("artifact B", encoding="utf-8")
    engine = build_engine(settings)
    Base.metadata.create_all(engine)
    session_factory = build_session_factory(engine)

    submitted: list[int] = []
    with session_factory() as session:
        SourceFileService(session, settings).scan()
        job = JobService(session, submitted.append).create_changed_conversion_job()
    assert submitted == [job.id]

    processor = DocumentConversionItemProcessor(
        settings,
        session_factory,
        APIKeyCipher(settings.secret_path),
    )
    item_number = 0

    def crash_during_second_item(item: JobItem, heartbeat: Callable[[], None]) -> None:
        nonlocal item_number
        item_number += 1
        if item_number == 2:
            raise KeyboardInterrupt("simulated hard worker crash")
        processor(item, heartbeat)

    with pytest.raises(KeyboardInterrupt, match="hard worker crash"):
        execute_job(
            job.id,
            session_factory,
            item_processor=crash_during_second_item,
            heartbeat_timeout=1,
        )

    first_artifact = settings.markdown_dir / "1" / "part-001.md"
    first_bytes = first_artifact.read_bytes()
    with session_factory() as session:
        crashed = session.get(Job, job.id)
        assert crashed is not None and crashed.status == JobStatus.RUNNING
        items = session.scalars(
            select(JobItem).where(JobItem.job_id == job.id).order_by(JobItem.id)
        ).all()
        assert [item.status for item in items] == [
            JobStatus.COMPLETED,
            JobStatus.RUNNING,
        ]

    recovered: list[int] = []
    assert recover_stale_jobs(
        session_factory,
        recovered.append,
        heartbeat_timeout=1,
        now=utc_now() + timedelta(seconds=5),
    ) == [job.id]
    assert recovered == [job.id]
    execute_job(
        job.id,
        session_factory,
        item_processor=processor,
        retry_unexpected=False,
        heartbeat_timeout=1,
    )

    assert first_artifact.read_bytes() == first_bytes
    assert (settings.markdown_dir / "2" / "manifest.json").is_file()
    with session_factory() as session:
        completed = session.get(Job, job.id)
        assert completed is not None and completed.status == JobStatus.COMPLETED
        items = session.scalars(
            select(JobItem).where(JobItem.job_id == job.id).order_by(JobItem.id)
        ).all()
        assert [item.attempts for item in items] == [1, 2]
        sources = session.scalars(select(SourceFile).order_by(SourceFile.id)).all()
        assert [source.conversion_status for source in sources] == [
            ConversionStatus.READY,
            ConversionStatus.READY,
        ]
    engine.dispose()


def test_scenario_g_rejects_parent_absolute_and_symlink_escape(tmp_path: Path) -> None:
    source_root = tmp_path / "knowledge"
    source_root.mkdir()
    with pytest.raises(UnsafeSourcePathError):
        safe_source_path(source_root, "../outside.txt")
    with pytest.raises(UnsafeSourcePathError):
        safe_source_path(source_root, str((tmp_path / "outside.txt").resolve()))

    outside = tmp_path / "outside"
    outside.mkdir()
    link = source_root / "escape"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks are unavailable on this filesystem")
    with pytest.raises(UnsafeSourcePathError):
        safe_source_path(source_root, "escape/secret.txt")
