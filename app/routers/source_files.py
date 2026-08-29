"""Source-file management and authenticated source download APIs."""

from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Request,
    Response,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.auth import require_admin, require_chat
from app.db import get_db_session
from app.schemas.source_file import (
    SourceFileBatchRequest,
    SourceFileRead,
    SourceFolderDeleteRead,
    SourceFolderRequest,
    SourceLibraryFileRead,
    SourceReferenceBatchRequest,
    SourceReferenceRead,
    SourceScanRead,
)
from app.schemas.job import JobDetailRead
from app.services.jobs import JobService
from app.services.source_files import SourceFileService


admin_router = APIRouter(
    prefix="/api/admin/files",
    tags=["source-files"],
    dependencies=[Depends(require_admin)],
)
download_router = APIRouter(
    prefix="/api/files",
    tags=["source-files"],
    dependencies=[Depends(require_chat)],
)


def get_source_file_service(
    request: Request,
    session: Annotated[Session, Depends(get_db_session)],
) -> SourceFileService:
    return SourceFileService(session, request.app.state.settings)


ServiceDependency = Annotated[SourceFileService, Depends(get_source_file_service)]


def get_conversion_job_service(
    request: Request,
    session: Annotated[Session, Depends(get_db_session)],
) -> JobService:
    return JobService(session, request.app.state.job_task_queue.enqueue)


ConversionJobServiceDependency = Annotated[
    JobService,
    Depends(get_conversion_job_service),
]


@download_router.get("", response_model=list[SourceLibraryFileRead])
def list_library_files(service: ServiceDependency) -> list[SourceLibraryFileRead]:
    """List knowledge files without exposing management operations."""
    return service.list_library_files()


@admin_router.get("", response_model=list[SourceFileRead])
def list_files(service: ServiceDependency) -> list[SourceFileRead]:
    return service.list_files()


@admin_router.post("/scan", response_model=SourceScanRead)
def scan_files(service: ServiceDependency) -> SourceScanRead:
    return service.scan()


@admin_router.post(
    "/upload",
    response_model=SourceFileRead,
    status_code=status.HTTP_201_CREATED,
)
def upload_file(
    service: ServiceDependency,
    file: Annotated[UploadFile, File()],
    relative_path: Annotated[str | None, Form()] = None,
) -> SourceFileRead:
    return service.upload(file, relative_path)


@admin_router.put("/{id}/replace", response_model=SourceFileRead)
def replace_file(
    id: int,
    service: ServiceDependency,
    file: Annotated[UploadFile, File()],
) -> SourceFileRead:
    return service.replace(id, file)


@admin_router.post(
    "/folder/convert",
    response_model=JobDetailRead,
    status_code=status.HTTP_202_ACCEPTED,
)
def convert_folder(
    payload: SourceFolderRequest,
    service: ConversionJobServiceDependency,
) -> JobDetailRead:
    return service.create_folder_conversion_job(payload.folder_path)


@admin_router.post(
    "/folder/delete",
    response_model=SourceFolderDeleteRead,
)
def delete_folder(
    payload: SourceFolderRequest,
    service: ServiceDependency,
) -> SourceFolderDeleteRead:
    return service.delete_folder(payload.folder_path)


@admin_router.delete("/{id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_file(id: int, service: ServiceDependency) -> Response:
    service.delete(id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@admin_router.post(
    "/{id}/convert",
    response_model=JobDetailRead,
    status_code=status.HTTP_202_ACCEPTED,
)
def convert_file(
    id: int,
    service: ConversionJobServiceDependency,
) -> JobDetailRead:
    return service.create_file_conversion_job(id)


@admin_router.post(
    "/batch-convert",
    response_model=JobDetailRead,
    status_code=status.HTTP_202_ACCEPTED,
)
def batch_convert_files(
    payload: SourceFileBatchRequest,
    service: ConversionJobServiceDependency,
) -> JobDetailRead:
    return service.create_files_conversion_job(payload.file_ids)


@download_router.get("/{document_id}/download", response_class=FileResponse)
def download_file(
    document_id: int,
    service: ServiceDependency,
) -> FileResponse:
    target = service.download_target(document_id)
    return FileResponse(
        target.path,
        filename=target.filename,
        content_disposition_type="attachment",
    )


@download_router.get("/{document_id}/view", response_class=FileResponse)
def view_file(
    document_id: int,
    service: ServiceDependency,
) -> FileResponse:
    target = service.download_target(document_id)
    return FileResponse(
        target.path,
        filename=target.filename,
        content_disposition_type="inline",
        headers={
            "Content-Security-Policy": "sandbox",
            "X-Content-Type-Options": "nosniff",
        },
    )


@download_router.post("/references", response_model=list[SourceReferenceRead])
def source_references(
    payload: SourceReferenceBatchRequest,
    service: ServiceDependency,
) -> list[SourceReferenceRead]:
    """Resolve internal document ids to safe, user-facing original files."""
    return service.source_references(payload.document_ids)
