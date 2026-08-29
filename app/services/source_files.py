"""Source-file path safety, inventory scanning, and atomic mutations."""

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import shutil
import tempfile
from typing import BinaryIO

from fastapi import UploadFile
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import Settings
from app.models.source_file import SourceFile
from app.schemas.source_file import (
    SourceFileRead,
    SourceFolderDeleteRead,
    SourceLibraryFileRead,
    SourceReferenceRead,
    SourceScanRead,
)
from app.source_files import ConversionStatus, IndexStatus, SourceStatus


HASH_CHUNK_SIZE = 1024 * 1024
IGNORED_SOURCE_FILE_NAMES = frozenset(
    {
        ".ds_store",
        ".localized",
        "desktop.ini",
        "ehthumbs.db",
        "icon\r",
        "thumbs.db",
    }
)
IGNORED_SOURCE_DIRECTORY_NAMES = frozenset(
    {
        "$recycle.bin",
        ".fseventsd",
        ".spotlight-v100",
        ".temporaryitems",
        ".trashes",
        "__macosx",
        "system volume information",
    }
)


class UnsafeSourcePathError(ValueError):
    """A logical source path would access something outside SOURCE_DIR."""


class SourceFileServiceError(RuntimeError):
    status_code = 500


class SourceFileNotFoundError(SourceFileServiceError):
    status_code = 404


class SourceFileConflictError(SourceFileServiceError):
    status_code = 409


class InvalidSourcePathError(SourceFileServiceError):
    status_code = 400


@dataclass(frozen=True, slots=True)
class DownloadTarget:
    path: Path
    filename: str


def normalize_source_folder_path(folder_path: str) -> str:
    """Validate and normalize one non-root logical source-folder path."""
    try:
        safe_source_path(Path("/source-folder-validation-root"), folder_path)
    except UnsafeSourcePathError as exc:
        raise InvalidSourcePathError(str(exc)) from exc
    return PurePosixPath(folder_path).as_posix()


def source_path_in_folder(relative_path: str, folder_path: str) -> bool:
    """Return whether a source-file path is a descendant of a folder path."""
    source_parts = PurePosixPath(relative_path).parts
    folder_parts = PurePosixPath(folder_path).parts
    return (
        len(source_parts) > len(folder_parts)
        and source_parts[: len(folder_parts)] == folder_parts
    )


def safe_source_path(source_root: Path, relative_path: str | Path) -> Path:
    """Validate a logical relative path and return its path under SOURCE_DIR.

    The configured source root is resolved, but the returned final path remains
    lexical so callers can safely replace or unlink an in-root symlink itself.
    Every existing symlink component is resolved for the containment check.
    """
    raw_path = os.fspath(relative_path)
    if not isinstance(raw_path, str):
        raise UnsafeSourcePathError("Source path must be text")
    if not raw_path or "\x00" in raw_path:
        raise UnsafeSourcePathError("Source path must not be empty")

    posix_path = PurePosixPath(raw_path)
    windows_path = PureWindowsPath(raw_path)
    if posix_path.is_absolute() or windows_path.is_absolute():
        raise UnsafeSourcePathError("Absolute source paths are not allowed")
    if ".." in posix_path.parts or ".." in windows_path.parts:
        raise UnsafeSourcePathError("Parent path segments are not allowed")
    if raw_path in {".", "./"}:
        raise UnsafeSourcePathError("Source path must identify a file")

    resolved_root = source_root.resolve(strict=False)
    candidate = resolved_root.joinpath(*posix_path.parts)
    resolved_candidate = candidate.resolve(strict=False)
    try:
        resolved_candidate.relative_to(resolved_root)
    except ValueError as exc:
        raise UnsafeSourcePathError(
            "Source path escapes the configured source root"
        ) from exc
    return candidate


def sha256_file(path: Path, *, chunk_size: int = HASH_CHUNK_SIZE) -> str:
    """Hash a file incrementally without loading it into memory."""
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def is_ignored_source_path(relative_path: str | Path) -> bool:
    """Return whether a source path is operating-system metadata, not knowledge."""
    raw_path = os.fspath(relative_path)
    if not isinstance(raw_path, str):
        return False
    parts = tuple(
        part
        for part in PurePosixPath(raw_path.replace("\\", "/")).parts
        if part not in {"", "."}
    )
    if not parts:
        return False

    if any(
        part.casefold() in IGNORED_SOURCE_DIRECTORY_NAMES
        for part in parts[:-1]
    ):
        return True

    filename = parts[-1]
    folded_filename = filename.casefold()
    return (
        folded_filename in IGNORED_SOURCE_FILE_NAMES
        or folded_filename.startswith("._")
        or folded_filename.startswith("~$")
        or folded_filename.startswith(".~lock.")
    )


class SourceFileService:
    def __init__(self, session: Session, settings: Settings) -> None:
        self.session = session
        self.settings = settings
        self.source_root = settings.source_dir

    def list_files(self) -> list[SourceFileRead]:
        records = self.session.scalars(
            select(SourceFile).order_by(SourceFile.relative_path)
        ).all()
        return [self._to_read(record) for record in records]

    def list_library_files(self) -> list[SourceLibraryFileRead]:
        """Return the read-only inventory used by the chat knowledge browser."""
        records = self.session.scalars(
            select(SourceFile).order_by(SourceFile.relative_path)
        ).all()
        results: list[SourceLibraryFileRead] = []
        for record in records:
            available = record.source_status == SourceStatus.PRESENT
            results.append(
                SourceLibraryFileRead(
                    id=record.id,
                    relative_path=record.relative_path,
                    filename=record.filename,
                    extension=record.extension,
                    size=record.size,
                    index_status=record.index_status,
                    converted_at=record.converted_at,
                    available=available,
                    view_url=(
                        f"/api/files/{record.id}/view" if available else None
                    ),
                    download_url=(
                        f"/api/files/{record.id}/download" if available else None
                    ),
                )
            )
        return results

    def scan(self) -> SourceScanRead:
        records = {
            record.relative_path: record
            for record in self.session.scalars(select(SourceFile)).all()
        }
        seen: set[str] = set()
        scanned = new = changed = unchanged = unsafe_skipped = 0

        for relative_path, path in self._iter_source_files():
            try:
                stat_result = path.stat()
            except OSError:
                unsafe_skipped += 1
                continue

            seen.add(relative_path)
            scanned += 1
            record = records.get(relative_path)
            needs_hash = (
                record is None
                or record.source_status != SourceStatus.PRESENT
                or record.size != stat_result.st_size
                or record.mtime_ns != stat_result.st_mtime_ns
            )
            if not needs_hash:
                unchanged += 1
                continue

            try:
                content_hash = sha256_file(path)
                final_stat = path.stat()
            except OSError:
                unsafe_skipped += 1
                scanned -= 1
                seen.discard(relative_path)
                continue

            if record is None:
                record = SourceFile(
                    relative_path=relative_path,
                    filename=path.name,
                    extension=_extension(path.name),
                    size=final_stat.st_size,
                    mtime_ns=final_stat.st_mtime_ns,
                    sha256=content_hash,
                    source_status=SourceStatus.PRESENT,
                    conversion_status=ConversionStatus.NEW,
                    index_status=IndexStatus.NOT_INDEXED,
                )
                self.session.add(record)
                records[relative_path] = record
                new += 1
                continue

            content_changed = content_hash != record.sha256
            record.filename = path.name
            record.extension = _extension(path.name)
            record.size = final_stat.st_size
            record.mtime_ns = final_stat.st_mtime_ns
            record.source_status = SourceStatus.PRESENT
            if content_changed:
                record.sha256 = content_hash
                record.conversion_status = ConversionStatus.CHANGED
                record.index_status = _stale_if_indexed(record.index_status)
                record.last_error = None
                changed += 1
            else:
                unchanged += 1

        removed = missing = 0
        for relative_path, record in records.items():
            if relative_path in seen:
                continue
            if record.index_status == IndexStatus.NOT_INDEXED:
                self._delete_markdown_artifacts(record.id)
                self.session.delete(record)
                removed += 1
                continue
            missing += 1
            if record.source_status != SourceStatus.MISSING:
                record.source_status = SourceStatus.MISSING
                record.index_status = _stale_if_indexed(record.index_status)

        self.session.commit()
        return SourceScanRead(
            scanned=scanned,
            new=new,
            changed=changed,
            unchanged=unchanged,
            removed=removed,
            missing=missing,
            unsafe_skipped=unsafe_skipped,
        )

    def upload(
        self,
        upload: UploadFile,
        relative_path: str | None = None,
    ) -> SourceFileRead:
        logical_path = relative_path or upload.filename
        if not logical_path:
            raise InvalidSourcePathError("An upload filename is required")
        normalized_path = _normalize_relative_path(logical_path)
        self._reject_ignored_upload(normalized_path, upload.filename)
        target = self._safe_path(normalized_path)
        existing = self.session.scalar(
            select(SourceFile).where(SourceFile.relative_path == normalized_path)
        )
        if existing is not None or os.path.lexists(target):
            raise SourceFileConflictError(
                "A source file already exists at this relative path; use replace"
            )

        size, mtime_ns, content_hash = self._atomic_upload(upload.file, target)
        record = SourceFile(
            relative_path=normalized_path,
            filename=target.name,
            extension=_extension(target.name),
            size=size,
            mtime_ns=mtime_ns,
            sha256=content_hash,
            source_status=SourceStatus.PRESENT,
            conversion_status=ConversionStatus.NEW,
            index_status=IndexStatus.NOT_INDEXED,
        )
        self.session.add(record)
        try:
            self.session.commit()
        except IntegrityError as exc:
            self.session.rollback()
            raise SourceFileConflictError(
                "A source file already exists at this relative path"
            ) from exc
        self.session.refresh(record)
        return self._to_read(record)

    def replace(self, file_id: int, upload: UploadFile) -> SourceFileRead:
        record = self._require_record(file_id)
        self._reject_ignored_upload(record.relative_path, upload.filename)
        target = self._safe_path(record.relative_path)
        size, mtime_ns, content_hash = self._atomic_upload(upload.file, target)
        content_changed = content_hash != record.sha256

        record.filename = target.name
        record.extension = _extension(target.name)
        record.size = size
        record.mtime_ns = mtime_ns
        record.source_status = SourceStatus.PRESENT
        if content_changed:
            record.sha256 = content_hash
            record.conversion_status = ConversionStatus.CHANGED
            record.index_status = _stale_if_indexed(record.index_status)
            record.last_error = None
        self.session.commit()
        self.session.refresh(record)
        return self._to_read(record)

    def delete(self, file_id: int) -> None:
        record = self._require_record(file_id)
        target = self._safe_path(record.relative_path)
        remove_record = record.index_status == IndexStatus.NOT_INDEXED
        if remove_record:
            self._delete_markdown_artifacts(record.id)
        try:
            target.unlink(missing_ok=True)
        except IsADirectoryError as exc:
            raise SourceFileConflictError(
                "The source-file path no longer identifies a regular file"
            ) from exc
        except OSError as exc:
            raise SourceFileConflictError("The source file could not be deleted") from exc

        if remove_record:
            self.session.delete(record)
        else:
            record.source_status = SourceStatus.MISSING
            record.index_status = _stale_if_indexed(record.index_status)
        self.session.commit()

    def delete_folder(self, folder_path: str) -> SourceFolderDeleteRead:
        normalized_path = normalize_source_folder_path(folder_path)
        target = self._safe_path(normalized_path)
        records = self._folder_records(normalized_path)
        target_exists = os.path.lexists(target)

        if not records and not target_exists:
            raise SourceFileNotFoundError("Source folder was not found")
        if target_exists and target.is_symlink():
            raise SourceFileConflictError(
                "The source-folder path identifies a symbolic link"
            )
        if target_exists and not target.is_dir():
            raise SourceFileConflictError(
                "The source-folder path no longer identifies a directory"
            )

        for record in records:
            if record.index_status == IndexStatus.NOT_INDEXED:
                self._delete_markdown_artifacts(record.id)

        if target_exists:
            try:
                shutil.rmtree(target)
            except OSError as exc:
                raise SourceFileConflictError(
                    "The source folder could not be deleted"
                ) from exc

        deleted_records = marked_missing = 0
        for record in records:
            if record.index_status == IndexStatus.NOT_INDEXED:
                self.session.delete(record)
                deleted_records += 1
            else:
                record.source_status = SourceStatus.MISSING
                record.index_status = _stale_if_indexed(record.index_status)
                marked_missing += 1
        self.session.commit()
        return SourceFolderDeleteRead(
            folder_path=normalized_path,
            affected_files=len(records),
            deleted_records=deleted_records,
            marked_missing=marked_missing,
        )

    def download_target(self, file_id: int) -> DownloadTarget:
        record = self._require_record(file_id)
        if record.source_status != SourceStatus.PRESENT:
            raise SourceFileNotFoundError("Source file is missing")
        target = self._safe_path(record.relative_path)
        if not target.is_file():
            record.source_status = SourceStatus.MISSING
            record.index_status = _stale_if_indexed(record.index_status)
            self.session.commit()
            raise SourceFileNotFoundError("Source file is missing")
        return DownloadTarget(path=target, filename=record.filename)

    def source_references(self, document_ids: list[int]) -> list[SourceReferenceRead]:
        """Return safe original-file metadata for browser-local chat history."""
        if not document_ids:
            return []
        records = {
            record.id: record
            for record in self.session.scalars(
                select(SourceFile).where(SourceFile.id.in_(document_ids))
            ).all()
        }
        references: list[SourceReferenceRead] = []
        for document_id in document_ids:
            record = records.get(document_id)
            if record is None:
                continue
            parent = PurePosixPath(record.relative_path).parent.as_posix()
            available = record.source_status == SourceStatus.PRESENT
            display_root = self.settings.source_display_root
            references.append(
                SourceReferenceRead(
                    document_id=str(record.id),
                    filename=record.filename,
                    relative_path=record.relative_path,
                    relative_directory="" if parent == "." else parent,
                    display_path=(
                        str(display_root / Path(record.relative_path))
                        if display_root is not None
                        else record.relative_path
                    ),
                    download_url=(
                        f"/api/files/{record.id}/download" if available else None
                    ),
                    available=available,
                )
            )
        return references

    def _iter_source_files(self):
        root = self.source_root.resolve(strict=False)
        if not root.is_dir():
            return

        for directory, dirnames, filenames in os.walk(root, followlinks=False):
            directory_path = Path(directory)
            dirnames[:] = [
                name
                for name in dirnames
                if not (directory_path / name).is_symlink()
                and name.casefold() not in IGNORED_SOURCE_DIRECTORY_NAMES
            ]
            for filename in filenames:
                path = directory_path / filename
                relative_path = path.relative_to(root).as_posix()
                if is_ignored_source_path(relative_path):
                    continue
                try:
                    safe_path = safe_source_path(root, relative_path)
                except UnsafeSourcePathError:
                    continue
                if safe_path.is_file():
                    yield relative_path, safe_path

    def _atomic_upload(
        self,
        source: BinaryIO,
        target: Path,
    ) -> tuple[int, int, str]:
        self.source_root.mkdir(parents=True, exist_ok=True)
        resolved_root = self.source_root.resolve(strict=False)
        relative_target = target.relative_to(resolved_root).as_posix()
        target = self._safe_path(relative_target)
        target.parent.mkdir(parents=True, exist_ok=True)
        target = self._safe_path(relative_target)

        file_descriptor, temp_name = tempfile.mkstemp(
            prefix=".source-upload-",
            suffix=".tmp",
            dir=target.parent,
        )
        temp_path = Path(temp_name)
        digest = hashlib.sha256()
        total_size = 0
        try:
            with os.fdopen(file_descriptor, "wb") as temp_file:
                while chunk := source.read(HASH_CHUNK_SIZE):
                    temp_file.write(chunk)
                    digest.update(chunk)
                    total_size += len(chunk)
                temp_file.flush()
                os.fsync(temp_file.fileno())

            self._safe_path(relative_target)
            os.replace(temp_path, target)
            final_stat = target.stat()
            return final_stat.st_size, final_stat.st_mtime_ns, digest.hexdigest()
        finally:
            temp_path.unlink(missing_ok=True)

    def _safe_path(self, relative_path: str | Path) -> Path:
        try:
            return safe_source_path(self.source_root, relative_path)
        except UnsafeSourcePathError as exc:
            raise InvalidSourcePathError(str(exc)) from exc

    def _reject_ignored_upload(
        self,
        logical_path: str,
        upload_filename: str | None,
    ) -> None:
        if is_ignored_source_path(logical_path) or (
            upload_filename and is_ignored_source_path(upload_filename)
        ):
            raise InvalidSourcePathError(
                "System metadata files such as .DS_Store are not accepted"
            )

    def _delete_markdown_artifacts(self, file_id: int) -> None:
        """Remove regenerable artifacts before an unindexed record is purged."""
        artifact_path = self.settings.markdown_dir / str(file_id)
        try:
            if artifact_path.is_symlink() or artifact_path.is_file():
                artifact_path.unlink(missing_ok=True)
            elif artifact_path.is_dir():
                shutil.rmtree(artifact_path)
        except OSError as exc:
            raise SourceFileConflictError(
                "The derived Markdown artifacts could not be deleted"
            ) from exc

    def _require_record(self, file_id: int) -> SourceFile:
        record = self.session.get(SourceFile, file_id)
        if record is None:
            raise SourceFileNotFoundError("Source file was not found")
        return record

    def _folder_records(self, folder_path: str) -> list[SourceFile]:
        records = self.session.scalars(
            select(SourceFile).order_by(SourceFile.relative_path)
        ).all()
        return [
            record
            for record in records
            if source_path_in_folder(record.relative_path, folder_path)
        ]

    def _to_read(self, record: SourceFile) -> SourceFileRead:
        display_root = self.settings.source_display_root
        display_path = (
            str(display_root / Path(record.relative_path))
            if display_root is not None
            else record.relative_path
        )
        return SourceFileRead(
            id=record.id,
            relative_path=record.relative_path,
            filename=record.filename,
            extension=record.extension,
            size=record.size,
            mtime_ns=record.mtime_ns,
            sha256=record.sha256,
            source_status=record.source_status,
            conversion_status=record.conversion_status,
            index_status=record.index_status,
            last_error=record.last_error,
            converted_at=record.converted_at,
            created_at=record.created_at,
            updated_at=record.updated_at,
            display_path=display_path,
        )


def _normalize_relative_path(relative_path: str) -> str:
    try:
        safe_source_path(Path("/source-path-validation-root"), relative_path)
    except UnsafeSourcePathError as exc:
        raise InvalidSourcePathError(str(exc)) from exc
    return PurePosixPath(relative_path).as_posix()


def _extension(filename: str) -> str:
    return Path(filename).suffix.lower()


def _stale_if_indexed(index_status: str) -> str:
    if index_status == IndexStatus.INDEXED:
        return IndexStatus.STALE
    return index_status
