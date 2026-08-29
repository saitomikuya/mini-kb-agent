"""Administration page for the local knowledge base."""

from pathlib import Path

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.auth import require_admin
from app.db import get_db_session
from app.schemas.admin import IndexSummaryRead, SourceMarkdownPreviewRead, TextPreviewRead
from app.services.admin import AdminArtifactService


router = APIRouter(tags=["admin-ui"])
api_router = APIRouter(
    prefix="/api/admin",
    tags=["admin-artifacts"],
    dependencies=[Depends(require_admin)],
)
templates = Jinja2Templates(directory=Path(__file__).resolve().parents[1] / "templates")


def get_admin_artifact_service(
    request: Request,
    session: Annotated[Session, Depends(get_db_session)],
) -> AdminArtifactService:
    return AdminArtifactService(session, request.app.state.settings)


ServiceDependency = Annotated[
    AdminArtifactService,
    Depends(get_admin_artifact_service),
]


@router.get("/admin", response_class=HTMLResponse, include_in_schema=False)
def admin_page(request: Request) -> HTMLResponse:
    """Render the login-gated Admin workspace.

    The shell is intentionally public so an administrator can submit the Admin
    password. Every data read and mutation remains protected by require_admin.
    """
    return templates.TemplateResponse(
        request=request,
        name="admin.html",
        context={"app_name": request.app.state.settings.app_name},
    )


@api_router.get(
    "/files/{source_file_id}/markdown",
    response_model=SourceMarkdownPreviewRead,
)
def source_markdown_preview(
    source_file_id: int,
    service: ServiceDependency,
) -> SourceMarkdownPreviewRead:
    return service.source_markdown_preview(source_file_id)


@api_router.get("/index", response_model=IndexSummaryRead)
def index_summary(service: ServiceDependency) -> IndexSummaryRead:
    return service.index_summary()


@api_router.get("/index/root.json", response_model=TextPreviewRead)
def root_json_preview(service: ServiceDependency) -> TextPreviewRead:
    return service.root_json_preview()


@api_router.get("/index/root.md", response_model=TextPreviewRead)
def root_markdown_preview(service: ServiceDependency) -> TextPreviewRead:
    return service.root_markdown_preview()
