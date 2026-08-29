"""Source inventory, change detection, file APIs, and path-safety tests."""

from collections.abc import Iterator
import hashlib
import io
import os
from pathlib import Path
from urllib.parse import quote

from fastapi.testclient import TestClient
import pytest

from app.config import Settings
from app.db import Base
from app.main import create_app
from app.models.source_file import SourceFile
from app.services.source_files import (
    HASH_CHUNK_SIZE,
    UnsafeSourcePathError,
    safe_source_path,
    sha256_file,
)


ADMIN_PASSWORD = "admin-source-file-tests"
CHAT_PASSWORD = "chat-source-file-tests"


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    source_dir = tmp_path / "真实源目录"
    settings = Settings(
        chat_password=CHAT_PASSWORD,
        admin_password=ADMIN_PASSWORD,
        data_dir=tmp_path / "data",
        source_dir=source_dir,
        source_display_root=Path("/宿主机/知识库"),
        session_max_age=3600,
    )
    settings.data_dir.mkdir(parents=True)
    source_dir.mkdir(parents=True)
    application = create_app(settings)
    Base.metadata.create_all(application.state.database_engine)
    with TestClient(application) as test_client:
        login = test_client.post(
            "/api/auth/admin/login",
            json={"password": ADMIN_PASSWORD},
        )
        assert login.status_code == 200
        yield test_client


def _scan(client: TestClient) -> dict:
    response = client.post("/api/admin/files/scan")
    assert response.status_code == 200, response.text
    return response.json()


def _only_file(client: TestClient) -> dict:
    response = client.get("/api/admin/files")
    assert response.status_code == 200
    files = response.json()
    assert len(files) == 1
    return files[0]


def test_scan_new_unicode_file_in_chinese_directory(client: TestClient) -> None:
    source_dir = client.app.state.settings.source_dir
    path = source_dir / "中文目录" / "Résumé 知识.TXT"
    path.parent.mkdir()
    path.write_text("知识内容", encoding="utf-8")

    result = _scan(client)
    record = _only_file(client)

    assert result == {
        "scanned": 1,
        "new": 1,
        "changed": 0,
        "unchanged": 0,
        "removed": 0,
        "missing": 0,
        "unsafe_skipped": 0,
    }
    assert record["relative_path"] == "中文目录/Résumé 知识.TXT"
    assert record["filename"] == "Résumé 知识.TXT"
    assert record["extension"] == ".txt"
    assert record["sha256"] == hashlib.sha256("知识内容".encode()).hexdigest()
    assert record["source_status"] == "PRESENT"
    assert record["conversion_status"] == "NEW"
    assert record["index_status"] == "NOT_INDEXED"
    assert record["display_path"] == "/宿主机/知识库/中文目录/Résumé 知识.TXT"
    assert not client.app.state.settings.source_display_root.exists()


def test_scan_ignores_operating_system_metadata_files(client: TestClient) -> None:
    source_dir = client.app.state.settings.source_dir
    ignored_files = (
        ".DS_Store",
        "资料/._使用手册.pdf",
        "资料/Thumbs.db",
        "__MACOSX/资料/metadata",
        ".Spotlight-V100/index/store.db",
        "资料/~$编辑中的文档.docx",
    )
    for relative_path in ignored_files:
        path = source_dir / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"system metadata")
    knowledge_path = source_dir / "资料/使用手册.pdf"
    knowledge_path.write_bytes(b"knowledge")

    result = _scan(client)
    record = _only_file(client)

    assert result["scanned"] == 1
    assert result["new"] == 1
    assert result["unsafe_skipped"] == 0
    assert record["relative_path"] == "资料/使用手册.pdf"


def test_unchanged_file_uses_metadata_and_does_not_hash_again(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = client.app.state.settings.source_dir / "stable.txt"
    path.write_bytes(b"stable")
    assert _scan(client)["new"] == 1

    def unexpected_hash(_path: Path) -> str:
        raise AssertionError("unchanged files must not be hashed")

    monkeypatch.setattr("app.services.source_files.sha256_file", unexpected_hash)

    result = _scan(client)

    assert result["unchanged"] == 1
    assert result["changed"] == 0


def test_changed_content_marks_conversion_changed_and_index_stale(
    client: TestClient,
) -> None:
    path = client.app.state.settings.source_dir / "changed.md"
    path.write_bytes(b"before")
    _scan(client)
    record = _only_file(client)
    old_hash = record["sha256"]

    with client.app.state.session_factory() as session:
        stored = session.get(SourceFile, record["id"])
        assert stored is not None
        stored.conversion_status = "READY"
        stored.index_status = "INDEXED"
        session.commit()

    path.write_bytes(b"after content")
    result = _scan(client)
    updated = _only_file(client)

    assert result["changed"] == 1
    assert updated["sha256"] != old_hash
    assert updated["conversion_status"] == "CHANGED"
    assert updated["index_status"] == "STALE"


def test_mtime_change_with_same_hash_preserves_processing_states(
    client: TestClient,
) -> None:
    path = client.app.state.settings.source_dir / "same.txt"
    path.write_bytes(b"same content")
    _scan(client)
    record = _only_file(client)

    with client.app.state.session_factory() as session:
        stored = session.get(SourceFile, record["id"])
        assert stored is not None
        stored.conversion_status = "READY"
        stored.index_status = "INDEXED"
        session.commit()

    previous_mtime_ns = path.stat().st_mtime_ns
    os.utime(path, ns=(previous_mtime_ns + 1_000_000_000,) * 2)
    result = _scan(client)
    updated = _only_file(client)

    assert result["changed"] == 0
    assert result["unchanged"] == 1
    assert updated["mtime_ns"] != record["mtime_ns"]
    assert updated["sha256"] == record["sha256"]
    assert updated["conversion_status"] == "READY"
    assert updated["index_status"] == "INDEXED"


def test_scan_removes_unindexed_deleted_file_database_row(
    client: TestClient,
) -> None:
    path = client.app.state.settings.source_dir / "removed.pdf"
    path.write_bytes(b"pdf")
    _scan(client)
    _only_file(client)

    path.unlink()
    result = _scan(client)

    assert result["removed"] == 1
    assert result["missing"] == 0
    assert client.get("/api/admin/files").json() == []


def test_scan_marks_indexed_deleted_file_missing_without_deleting_record(
    client: TestClient,
) -> None:
    path = client.app.state.settings.source_dir / "indexed-removed.pdf"
    path.write_bytes(b"pdf")
    _scan(client)
    record_id = _only_file(client)["id"]
    with client.app.state.session_factory() as session:
        stored = session.get(SourceFile, record_id)
        assert stored is not None
        stored.conversion_status = "READY"
        stored.index_status = "INDEXED"
        session.commit()

    path.unlink()
    result = _scan(client)
    record = _only_file(client)

    assert result["removed"] == 0
    assert result["missing"] == 1
    assert record["id"] == record_id
    assert record["source_status"] == "MISSING"
    assert record["index_status"] == "STALE"


def test_upload_replace_download_and_delete(client: TestClient) -> None:
    upload = client.post(
        "/api/admin/files/upload",
        data={"relative_path": "中文目录/上传资料.txt"},
        files={"file": ("browser-name.txt", b"version one", "text/plain")},
    )

    assert upload.status_code == 201, upload.text
    record = upload.json()
    file_id = record["id"]
    physical_path = client.app.state.settings.source_dir / "中文目录/上传资料.txt"
    assert physical_path.read_bytes() == b"version one"
    assert record["conversion_status"] == "NEW"

    download = client.get(f"/api/files/{file_id}/download")
    assert download.status_code == 200
    assert download.content == b"version one"
    assert download.headers["content-disposition"] == (
        "attachment; filename*=utf-8''" + quote("上传资料.txt")
    )

    replace = client.put(
        f"/api/admin/files/{file_id}/replace",
        files={"file": ("ignored-name.bin", b"version two", "application/octet-stream")},
    )
    assert replace.status_code == 200, replace.text
    assert replace.json()["relative_path"] == "中文目录/上传资料.txt"
    assert replace.json()["filename"] == "上传资料.txt"
    assert replace.json()["conversion_status"] == "CHANGED"
    assert physical_path.read_bytes() == b"version two"
    assert client.get(f"/api/files/{file_id}/download").content == b"version two"

    library = client.get("/api/files")
    assert library.status_code == 200
    assert library.json() == [
        {
            "id": file_id,
            "relative_path": "中文目录/上传资料.txt",
            "filename": "上传资料.txt",
            "extension": ".txt",
            "size": len(b"version two"),
            "index_status": "NOT_INDEXED",
            "converted_at": None,
            "available": True,
            "view_url": f"/api/files/{file_id}/view",
            "download_url": f"/api/files/{file_id}/download",
        }
    ]
    view = client.get(f"/api/files/{file_id}/view")
    assert view.status_code == 200
    assert view.content == b"version two"
    assert view.headers["content-disposition"].startswith("inline;")
    assert view.headers["content-security-policy"] == "sandbox"
    assert view.headers["x-content-type-options"] == "nosniff"

    references = client.post(
        "/api/files/references",
        json={"document_ids": [file_id, 999_999, file_id]},
    )
    assert references.status_code == 200
    assert references.json() == [
        {
            "document_id": str(file_id),
            "filename": "上传资料.txt",
            "relative_path": "中文目录/上传资料.txt",
            "relative_directory": "中文目录",
            "display_path": "/宿主机/知识库/中文目录/上传资料.txt",
            "download_url": f"/api/files/{file_id}/download",
            "available": True,
        }
    ]
    artifact_dir = client.app.state.settings.markdown_dir / str(file_id)
    artifact_dir.mkdir(parents=True)
    (artifact_dir / "manifest.json").write_text("{}", encoding="utf-8")

    deleted = client.delete(f"/api/admin/files/{file_id}")
    assert deleted.status_code == 204
    assert not physical_path.exists()
    assert not artifact_dir.exists()
    assert client.get("/api/admin/files").json() == []
    assert client.get(f"/api/files/{file_id}/download").status_code == 404


def test_chat_user_source_library_access_is_read_only(client: TestClient) -> None:
    uploaded = client.post(
        "/api/admin/files/upload",
        files={"file": ("只读资料.txt", b"read only", "text/plain")},
    ).json()
    client.post("/api/auth/logout")
    login = client.post(
        "/api/auth/chat/login",
        json={"password": CHAT_PASSWORD},
    )
    assert login.status_code == 200

    library = client.get("/api/files")
    assert library.status_code == 200
    assert library.json()[0]["filename"] == "只读资料.txt"
    assert client.get(f"/api/files/{uploaded['id']}/view").content == b"read only"
    assert client.get(f"/api/files/{uploaded['id']}/download").content == b"read only"

    assert client.get("/api/admin/files").status_code == 403
    assert client.post("/api/admin/files/scan").status_code == 403
    assert client.delete(f"/api/admin/files/{uploaded['id']}").status_code == 403


def test_delete_indexed_file_retains_missing_record(client: TestClient) -> None:
    uploaded = client.post(
        "/api/admin/files/upload",
        files={"file": ("indexed.txt", b"indexed", "text/plain")},
    ).json()
    with client.app.state.session_factory() as session:
        stored = session.get(SourceFile, uploaded["id"])
        assert stored is not None
        stored.conversion_status = "READY"
        stored.index_status = "INDEXED"
        session.commit()

    deleted = client.delete(f"/api/admin/files/{uploaded['id']}")

    assert deleted.status_code == 204
    record = _only_file(client)
    assert record["source_status"] == "MISSING"
    assert record["index_status"] == "STALE"


def test_delete_folder_removes_descendants_and_preserves_indexed_records(
    client: TestClient,
) -> None:
    uploads = {
        "资料包/说明.txt": b"unindexed",
        "资料包/产品/参数.md": b"indexed",
        "保留/其他.txt": b"outside",
    }
    records_by_path = {}
    for relative_path, content in uploads.items():
        response = client.post(
            "/api/admin/files/upload",
            data={"relative_path": relative_path},
            files={"file": (Path(relative_path).name, content, "text/plain")},
        )
        assert response.status_code == 201, response.text
        records_by_path[relative_path] = response.json()

    indexed = records_by_path["资料包/产品/参数.md"]
    with client.app.state.session_factory() as session:
        stored = session.get(SourceFile, indexed["id"])
        assert stored is not None
        stored.conversion_status = "READY"
        stored.index_status = "INDEXED"
        session.commit()

    unindexed = records_by_path["资料包/说明.txt"]
    artifact_dir = client.app.state.settings.markdown_dir / str(unindexed["id"])
    artifact_dir.mkdir(parents=True)
    (artifact_dir / "manifest.json").write_text("{}", encoding="utf-8")
    metadata = client.app.state.settings.source_dir / "资料包/.DS_Store"
    metadata.write_bytes(b"untracked metadata")

    deleted = client.post(
        "/api/admin/files/folder/delete",
        json={"folder_path": "资料包"},
    )

    assert deleted.status_code == 200, deleted.text
    assert deleted.json() == {
        "folder_path": "资料包",
        "affected_files": 2,
        "deleted_records": 1,
        "marked_missing": 1,
    }
    assert not (client.app.state.settings.source_dir / "资料包").exists()
    assert not artifact_dir.exists()
    assert (client.app.state.settings.source_dir / "保留/其他.txt").is_file()
    remaining = {
        record["relative_path"]: record
        for record in client.get("/api/admin/files").json()
    }
    assert set(remaining) == {"资料包/产品/参数.md", "保留/其他.txt"}
    assert remaining["资料包/产品/参数.md"]["source_status"] == "MISSING"
    assert remaining["资料包/产品/参数.md"]["index_status"] == "STALE"


@pytest.mark.parametrize("unsafe_path", [".", "../escape", "/tmp/escape"])
def test_delete_folder_rejects_root_and_unsafe_paths(
    client: TestClient,
    unsafe_path: str,
) -> None:
    protected = client.app.state.settings.source_dir / "protected/file.txt"
    protected.parent.mkdir()
    protected.write_bytes(b"keep")
    _scan(client)

    response = client.post(
        "/api/admin/files/folder/delete",
        json={"folder_path": unsafe_path},
    )

    assert response.status_code == 400
    assert protected.read_bytes() == b"keep"


def test_folder_upload_paths_preserve_nested_directory_structure(
    client: TestClient,
) -> None:
    uploads = {
        "资料包/说明.txt": b"root document",
        "资料包/产品/参数.md": b"nested document",
    }

    for relative_path, content in uploads.items():
        response = client.post(
            "/api/admin/files/upload",
            data={"relative_path": relative_path},
            files={"file": (Path(relative_path).name, content, "text/plain")},
        )
        assert response.status_code == 201, response.text

    records = client.get("/api/admin/files").json()
    assert {record["relative_path"] for record in records} == set(uploads)
    for relative_path, content in uploads.items():
        assert (
            client.app.state.settings.source_dir / relative_path
        ).read_bytes() == content


@pytest.mark.parametrize(
    ("upload_filename", "relative_path"),
    [
        (".DS_Store", None),
        ("safe-name.bin", "资料/.DS_Store"),
        ("._使用手册.pdf", "资料/._使用手册.pdf"),
        ("Thumbs.db", "资料/Thumbs.db"),
        ("metadata", "__MACOSX/metadata"),
        ("~$编辑中的文档.docx", "资料/~$编辑中的文档.docx"),
    ],
)
def test_upload_rejects_operating_system_metadata_files(
    client: TestClient,
    upload_filename: str,
    relative_path: str | None,
) -> None:
    response = client.post(
        "/api/admin/files/upload",
        data={"relative_path": relative_path} if relative_path else {},
        files={"file": (upload_filename, b"system metadata", "application/octet-stream")},
    )

    assert response.status_code == 400
    assert ".DS_Store" in response.json()["detail"]
    assert client.get("/api/admin/files").json() == []
    assert not any(client.app.state.settings.source_dir.rglob("*"))


def test_replace_rejects_operating_system_metadata_file(
    client: TestClient,
) -> None:
    uploaded = client.post(
        "/api/admin/files/upload",
        files={"file": ("guide.txt", b"knowledge", "text/plain")},
    ).json()
    physical_path = client.app.state.settings.source_dir / "guide.txt"

    response = client.put(
        f"/api/admin/files/{uploaded['id']}/replace",
        files={"file": (".DS_Store", b"system metadata", "application/octet-stream")},
    )

    assert response.status_code == 400
    assert physical_path.read_bytes() == b"knowledge"


def test_replace_with_identical_content_does_not_mark_changed(
    client: TestClient,
) -> None:
    upload = client.post(
        "/api/admin/files/upload",
        files={"file": ("identical.txt", b"same bytes", "text/plain")},
    ).json()
    with client.app.state.session_factory() as session:
        stored = session.get(SourceFile, upload["id"])
        assert stored is not None
        stored.conversion_status = "READY"
        stored.index_status = "INDEXED"
        session.commit()

    replaced = client.put(
        f"/api/admin/files/{upload['id']}/replace",
        files={"file": ("different-name.txt", b"same bytes", "text/plain")},
    )

    assert replaced.status_code == 200
    assert replaced.json()["conversion_status"] == "READY"
    assert replaced.json()["index_status"] == "INDEXED"


@pytest.mark.parametrize(
    "unsafe_path",
    ["../escape.txt", "folder/../../escape.txt", "/tmp/escape.txt", "C:\\escape.txt"],
)
def test_upload_rejects_traversal_and_absolute_paths(
    client: TestClient,
    unsafe_path: str,
) -> None:
    response = client.post(
        "/api/admin/files/upload",
        data={"relative_path": unsafe_path},
        files={"file": ("safe.txt", b"must not escape", "text/plain")},
    )

    assert response.status_code == 400
    assert not (client.app.state.settings.source_dir.parent / "escape.txt").exists()


def test_safe_source_path_rejects_symlink_escape(client: TestClient) -> None:
    source_root = client.app.state.settings.source_dir
    outside = source_root.parent / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_bytes(b"outside secret")
    (source_root / "escape-link").symlink_to(outside, target_is_directory=True)

    with pytest.raises(UnsafeSourcePathError):
        safe_source_path(source_root, "escape-link/secret.txt")

    response = client.post(
        "/api/admin/files/upload",
        data={"relative_path": "escape-link/created.txt"},
        files={"file": ("created.txt", b"blocked", "text/plain")},
    )
    assert response.status_code == 400
    assert not (outside / "created.txt").exists()

    (source_root / "outside-file-link.txt").symlink_to(outside / "secret.txt")
    result = _scan(client)
    assert result["scanned"] == 0
    assert client.get("/api/admin/files").json() == []


def test_download_requires_database_id_and_authentication(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        source_dir=tmp_path / "sources",
    )
    settings.data_dir.mkdir()
    application = create_app(settings)
    Base.metadata.create_all(application.state.database_engine)

    with TestClient(application) as unauthenticated:
        assert unauthenticated.get("/api/files").status_code == 401
        assert unauthenticated.get("/api/files/1/view").status_code == 401
        assert unauthenticated.get("/api/files/1/download").status_code == 401
        assert unauthenticated.get("/api/files/download?path=/etc/passwd").status_code in {
            404,
            422,
        }


def test_large_file_hash_is_streamed_in_bounded_chunks() -> None:
    payload = b"x" * (HASH_CHUNK_SIZE * 3 + 73)

    class TrackingReader(io.BytesIO):
        requests: list[int]

        def __init__(self, data: bytes) -> None:
            super().__init__(data)
            self.requests = []

        def read(self, size: int = -1) -> bytes:
            self.requests.append(size)
            assert 0 < size <= HASH_CHUNK_SIZE
            return super().read(size)

    class StreamPath:
        def __init__(self, reader: TrackingReader) -> None:
            self.reader = reader

        def open(self, mode: str) -> TrackingReader:
            assert mode == "rb"
            return self.reader

    reader = TrackingReader(payload)

    digest = sha256_file(StreamPath(reader))  # type: ignore[arg-type]

    assert digest == hashlib.sha256(payload).hexdigest()
    assert len(reader.requests) >= 5
