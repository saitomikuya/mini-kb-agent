"""Administration page and generated-artifact read API coverage."""

from collections.abc import Iterator
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from app.config import Settings
from app.db import Base
from app.main import create_app
from app.models.index_generation import IndexGeneration
from app.models.source_file import SourceFile


ADMIN_PASSWORD = "admin-ui-tests"


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    settings = Settings(
        admin_password=ADMIN_PASSWORD,
        chat_password="chat-ui-tests",
        data_dir=tmp_path / "data",
        source_dir=tmp_path / "sources",
        session_max_age=3600,
    )
    settings.data_dir.mkdir(parents=True)
    settings.source_dir.mkdir(parents=True)
    application = create_app(settings)
    Base.metadata.create_all(application.state.database_engine)
    with TestClient(application) as test_client:
        login = test_client.post(
            "/api/auth/admin/login",
            json={"password": ADMIN_PASSWORD},
        )
        assert login.status_code == 200
        yield test_client


def test_admin_page_exposes_login_and_merged_management_sections(tmp_path: Path) -> None:
    settings = Settings(
        admin_password=ADMIN_PASSWORD,
        data_dir=tmp_path / "data",
        source_dir=tmp_path / "sources",
    )
    settings.data_dir.mkdir(parents=True)
    application = create_app(settings)
    Base.metadata.create_all(application.state.database_engine)

    with TestClient(application) as unauthenticated:
        page = unauthenticated.get("/admin")
        assert page.status_code == 200
        assert 'id="login-form"' in page.text
        assert all(label in page.text for label in ("文件", "任务", "模型", "问答调优"))
        assert 'data-tab="index"' not in page.text
        assert 'id="panel-index"' not in page.text
        assert 'class="embedded-index-section"' in page.text
        assert all(
            label in page.text
            for label in ("知识库索引", "当前索引版本", "已索引文件", "索引文件夹", "最近生成时间")
        )
        assert "current generation" not in page.text
        assert "<th>文件 / 文件夹</th>" in page.text
        assert "<th>相对路径</th>" not in page.text
        assert 'id="file-browser-up"' in page.text
        assert 'id="file-breadcrumbs"' in page.text
        assert 'id="file-directory-summary"' in page.text
        admin_script = unauthenticated.get("/static/admin.js")
        assert admin_script.status_code == 200
        admin_style = unauthenticated.get("/static/admin.css")
        assert admin_style.status_code == 200
        assert 'href="/static/admin.css?' in page.text
        assert 'src="/static/admin.js?' in page.text
        assert "http://testserver/static/" not in page.text
        assert "buildFileTree" in admin_script.text
        assert "resolveFileTreeBranch" in admin_script.text
        assert "currentFileFolderPath" in admin_script.text
        assert "open-file-folder" in admin_script.text
        assert 'button("打开", "open-file-folder")' not in admin_script.text
        assert 'button("更新", "replace-file")' in admin_script.text
        assert "toggle-file-folder" not in admin_script.text
        assert "appendFolderRows" not in admin_script.text
        assert 'data-upload-picker="file"' in page.text
        assert 'data-upload-picker="folder"' in page.text
        assert "webkitdirectory" in page.text
        assert "webkitRelativePath" in admin_script.text
        assert "selectedUploadKind" in admin_script.text
        assert 'id="files-select-all"' in page.text
        assert 'data-action="batch-convert-files"' in page.text
        assert 'data-action="batch-delete-files"' in page.text
        assert 'id="job-items-dialog"' in page.text
        assert 'id="job-items-body"' in page.text
        assert 'id="delete-all-jobs"' in page.text
        assert 'data-action="delete-all-jobs"' in page.text
        assert 'id="jobs-summary"' in page.text
        assert 'class="table-card jobs-table-card"' in page.text
        assert 'id="current-job"' not in page.text
        assert "Admin session" not in page.text
        assert 'id="file-progress-dialog"' in page.text
        assert 'id="file-progress-metrics"' in page.text
        assert 'id="rebuild-index-button"' in page.text
        assert 'id="preview-readable-tab"' in page.text
        assert 'id="preview-source-tab"' in page.text
        assert "可读预览" in page.text
        assert "源文件内容" in page.text
        assert "renderReadableIndexJson" in admin_script.text
        assert "renderReadableMarkdown" in admin_script.text
        assert "setPreviewTab" in admin_script.text
        assert '<p class="eyebrow">索引概览</p>' not in page.text
        assert '<p class="eyebrow">Background work</p>' not in page.text
        assert '<p class="eyebrow">Model registry</p>' not in page.text
        assert page.text.count('<p class="eyebrow">Source library</p>') == 1
        assert 'data-action="retry-job-items"' in page.text
        assert "show-job-items" in admin_script.text
        assert "retry-job-item" in admin_script.text
        assert "retryJobItems" in admin_script.text
        assert all(
            action in admin_script.text
            for action in (
                "pause-job",
                "resume-job",
                "stop-job",
                "restart-job",
                "delete-job",
                "delete-all-jobs",
            )
        )
        assert "deleteAllJobs" in admin_script.text
        assert 'jsonApi("/api/admin/jobs", { method: "DELETE" })' in admin_script.text
        assert "jobManagementActions" in admin_script.text
        assert 'jsonApi("/api/admin/jobs/current")' not in admin_script.text
        assert "job-row-active" in admin_script.text
        assert "jobTableCell" in admin_script.text
        assert 'cell.dataset.label = label' in admin_script.text
        assert ".jobs-table thead { display: none; }" in admin_style.text
        assert ".file-progress-metrics { grid-template-columns: repeat(2, minmax(0, 1fr));" in admin_style.text
        assert "show-current-file-progress" in admin_script.text
        assert "progressMetrics" in admin_script.text
        assert "direct_text_pages" in admin_script.text
        assert "total_slides" in admin_script.text
        assert "show-index-progress" in admin_script.text
        assert "查看索引进度" not in admin_script.text
        assert "documents_to_refresh" in admin_script.text
        assert "model_requests_completed" in admin_script.text
        assert "差量更新索引" in admin_script.text
        assert "按当前分块配置重新转换全部文件" in admin_script.text
        assert all(
            role in admin_script.text
            for role in (
                "document_conversion",
                "index_generation",
                "query_router",
                "answer_generation",
            )
        )
        assert "Context Window" not in page.text
        assert "Max Output" not in page.text
        assert "Reasoning Effort" not in page.text
        assert "推理强度" in page.text
        assert "REASONING_EFFORT_OPTIONS" in admin_script.text
        assert 'value: "model_default"' in admin_script.text
        assert "data-role-prompt" in admin_script.text
        assert "save-role-prompts" in admin_script.text
        assert "reset-role-prompts" in admin_script.text
        assert "快捷应用" not in page.text
        assert 'id="apply-profile"' not in page.text
        assert "apply-all-roles" not in admin_script.text
        assert "}, 1000);" in admin_script.text
        assert "job.elapsed_seconds" in admin_script.text
        assert "运行期间每 1 秒刷新" in page.text
        assert "Profile 中的上下文和输出表示模型能力上限" in page.text
        assert 'id="tuning-form"' in page.text
        assert 'name="answer_context_tokens"' in page.text
        assert 'name="lexical_max_parts_per_document"' in page.text
        assert 'name="document_text_chars_per_part"' in page.text
        assert 'name="context_window"' in page.text
        assert 'name="max_output_tokens"' in page.text
        assert 'name="max_output_tokens" type="number" min="256" max="128000"' in page.text
        assert 'name="answer_max_output_tokens" type="number" min="256" max="128000"' in page.text
        assert "1,050,000" in page.text
        assert "128,000 tokens" in page.text
        assert "实际上下文四分之一" in page.text
        assert 'data-action="reconvert-all"' in page.text
        assert 'jsonApi("/api/admin/jobs/reconvert-all"' in admin_script.text
        assert "能力上限：" in admin_script.text
        assert "loadTuning" in admin_script.text
        assert "submitTuning" in admin_script.text
        assert "convert-file-folder" in admin_script.text
        assert "delete-file-folder" in admin_script.text
        assert "last_completed_document_name" in admin_script.text
        assert "最近完成文档" in admin_script.text
        assert unauthenticated.get("/api/admin/index").status_code == 401


def _add_source(client: TestClient, relative_path: str) -> SourceFile:
    content = b"source"
    with client.app.state.session_factory() as session:
        source = SourceFile(
            relative_path=relative_path,
            filename=Path(relative_path).name,
            extension=Path(relative_path).suffix,
            size=len(content),
            mtime_ns=1,
            sha256=hashlib.sha256(content).hexdigest(),
            source_status="PRESENT",
            conversion_status="READY",
            index_status="NOT_INDEXED",
            converted_at=datetime.now(timezone.utc),
        )
        session.add(source)
        session.commit()
        session.refresh(source)
        session.expunge(source)
        return source


def test_markdown_preview_returns_all_manifest_parts(client: TestClient) -> None:
    source = _add_source(client, "产品/说明.md")
    artifact_dir = client.app.state.settings.markdown_dir / str(source.id)
    artifact_dir.mkdir(parents=True)
    parts = []
    for number, body in enumerate(("# 第一部分\n", "# 第二部分\n"), start=1):
        part_id = f"part-{number:03d}"
        filename = f"{part_id}.md"
        (artifact_dir / filename).write_text(body, encoding="utf-8")
        parts.append(
            {
                "part_id": part_id,
                "path": filename,
                "anchors": {"section": number},
                "sha256": hashlib.sha256(body.encode()).hexdigest(),
            }
        )
    (artifact_dir / "manifest.json").write_text(
        json.dumps(
            {
                "document_id": source.id,
                "source_path": source.relative_path,
                "source_sha256": source.sha256,
                "converted_at": "2026-08-28T08:00:00+00:00",
                "converter_version": "test",
                "status": "READY",
                "parts": parts,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    response = client.get(f"/api/admin/files/{source.id}/markdown")

    assert response.status_code == 200, response.text
    preview = response.json()
    assert preview["relative_path"] == "产品/说明.md"
    assert [part["part_id"] for part in preview["parts"]] == [
        "part-001",
        "part-002",
    ]
    assert preview["parts"][1]["content"] == "# 第二部分\n"


def test_markdown_preview_rejects_symlinked_artifact_directory(
    client: TestClient,
) -> None:
    source = _add_source(client, "unsafe.txt")
    outside = client.app.state.settings.data_dir.parent / "outside-artifact"
    outside.mkdir()
    (outside / "manifest.json").write_text("{}", encoding="utf-8")
    client.app.state.settings.markdown_dir.mkdir(parents=True)
    (client.app.state.settings.markdown_dir / str(source.id)).symlink_to(
        outside,
        target_is_directory=True,
    )

    response = client.get(f"/api/admin/files/{source.id}/markdown")

    assert response.status_code == 409
    assert "unsafe" in response.json()["detail"]


def test_index_summary_and_root_previews_follow_current_pointer(
    client: TestClient,
) -> None:
    empty = client.get("/api/admin/index")
    assert empty.status_code == 200
    assert empty.json() == {
        "current_generation": None,
        "document_count": 0,
        "folder_count": 0,
        "last_generated": None,
    }

    settings = client.app.state.settings
    generation_dir = settings.index_dir / "generations" / "3"
    generation_dir.mkdir(parents=True)
    root = {
        "folders": [
            {"folder_id": "a", "document_count": 2},
            {"folder_id": "b", "document_count": 3},
        ]
    }
    (generation_dir / "root.json").write_text(
        json.dumps(root, ensure_ascii=False),
        encoding="utf-8",
    )
    (generation_dir / "root.md").write_text("# Root preview\n", encoding="utf-8")
    activated_at = datetime(2026, 8, 28, 8, 30, tzinfo=timezone.utc)
    settings.index_dir.mkdir(parents=True, exist_ok=True)
    (settings.index_dir / "current.json").write_text(
        json.dumps(
            {
                "generation_number": 3,
                "root_index_path": "generations/3/root.json",
                "activated_at": activated_at.isoformat(),
            }
        ),
        encoding="utf-8",
    )
    with client.app.state.session_factory() as session:
        session.add(
            IndexGeneration(
                generation_number=3,
                status="ACTIVE",
                root_index_path="generations/3/root.json",
                document_count=5,
                activated_at=activated_at,
            )
        )
        session.commit()

    summary = client.get("/api/admin/index")
    root_json = client.get("/api/admin/index/root.json")
    root_markdown = client.get("/api/admin/index/root.md")

    assert summary.status_code == 200, summary.text
    assert summary.json()["current_generation"] == 3
    assert summary.json()["document_count"] == 5
    assert summary.json()["folder_count"] == 2
    assert summary.json()["last_generated"].startswith("2026-08-28T08:30:00")
    assert json.loads(root_json.json()["content"]) == root
    assert root_json.json()["filename"] == "root.json"
    assert root_markdown.json()["content"] == "# Root preview\n"
