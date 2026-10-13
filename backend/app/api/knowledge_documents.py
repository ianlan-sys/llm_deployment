from datetime import date
from pathlib import Path
from uuid import uuid4

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.database import get_session
from app.models import KnowledgeSource
from app.schemas import (
    KnowledgeChunkPreview,
    KnowledgeDocumentDetail,
    KnowledgeDocumentSummary,
)
from app.services.audit import AuditService
from app.services.knowledge_ingestion import (
    InvalidKnowledgeTransition,
    KnowledgeDocumentNotFound,
    KnowledgeIndexingFailed,
)
from app.services.review_parser import SUPPORTED_MEDIA_TYPES


router = APIRouter(prefix="/api/knowledge-documents", tags=["knowledge-documents"])
VALID_STATUSES = {
    "uploaded",
    "parsing",
    "pending_review",
    "indexing",
    "active",
    "parse_failed",
    "index_failed",
    "rejected",
    "archived",
}


@router.post(
    "",
    response_model=KnowledgeDocumentSummary,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_document(
    request: Request,
    title: str = Form(...),
    category: str = Form(...),
    scope: str = Form(...),
    version: str = Form(...),
    effective_date: date | None = Form(default=None),
    file: UploadFile = File(...),
    session: Session = Depends(get_session),
) -> KnowledgeDocumentSummary:
    for value, maximum in (
        (title, 200),
        (category, 100),
        (scope, 150),
        (version, 50),
    ):
        if not value.strip() or len(value) > maximum:
            raise HTTPException(status_code=422, detail="资料元数据无效")

    original_filename = Path(file.filename or "").name
    suffix = Path(original_filename).suffix.lower()
    expected_media_type = SUPPORTED_MEDIA_TYPES.get(suffix)
    if expected_media_type is None or file.content_type != expected_media_type:
        raise HTTPException(
            status_code=415,
            detail="仅支持 PDF、DOCX 和 UTF-8 TXT 文件，且类型必须匹配",
        )
    maximum = request.app.state.settings.max_upload_bytes
    content = await file.read(maximum + 1)
    if len(content) > maximum:
        raise HTTPException(status_code=413, detail="上传文件超过大小限制")
    if not content:
        raise HTTPException(status_code=422, detail="上传文件不能为空")

    upload_dir = (
        request.app.state.settings.runtime_dir / "knowledge_uploads"
    )
    document_id = str(uuid4())
    target = upload_dir / f"{document_id}{suffix}"
    source = KnowledgeSource(
        id=document_id,
        title=title.strip(),
        source_type="uploaded_document",
        scope=scope.strip(),
        content="",
        status="uploaded",
        category=category.strip(),
        version=version.strip(),
        effective_date=effective_date,
        original_filename=original_filename,
        file_path=str(target),
        media_type=expected_media_type,
        file_size=len(content),
        parse_warnings=[],
    )
    try:
        upload_dir.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        session.add(source)
        mode = request.app.state.feature_provider_router.snapshot(
            "knowledge"
        ).mode
        AuditService(session).add(
            "knowledge_document.uploaded",
            "uploaded",
            "Knowledge document uploaded",
            mode,
            source.id,
        )
        session.flush()
        session.refresh(source)
        response = _summary(source)
        session.commit()
    except Exception:
        session.rollback()
        target.unlink(missing_ok=True)
        raise

    try:
        request.app.state.knowledge_ingestion_executor.submit(document_id)
    except Exception as exc:
        request.app.state.knowledge_ingestion_service.recover_cancelled(
            document_id
        )
        raise HTTPException(status_code=503, detail="文档解析服务暂不可用") from exc
    return response


@router.get("", response_model=list[KnowledgeDocumentSummary])
def list_documents(
    status_filter: str | None = Query(default=None, alias="status"),
    session: Session = Depends(get_session),
) -> list[KnowledgeDocumentSummary]:
    if status_filter is not None and status_filter not in VALID_STATUSES:
        raise HTTPException(status_code=422, detail="资料状态无效")
    statement = select(KnowledgeSource).order_by(
        KnowledgeSource.created_at.desc(),
        KnowledgeSource.id,
    )
    if status_filter is not None:
        statement = statement.where(KnowledgeSource.status == status_filter)
    return [_summary(source) for source in session.scalars(statement)]


@router.get("/{document_id}", response_model=KnowledgeDocumentDetail)
def get_document(
    document_id: str,
    session: Session = Depends(get_session),
) -> KnowledgeDocumentDetail:
    return _detail(_get_source(session, document_id))


@router.post("/{document_id}/approve", response_model=KnowledgeDocumentDetail)
def approve_document(
    document_id: str,
    request: Request,
    session: Session = Depends(get_session),
) -> KnowledgeDocumentDetail:
    reviewer = _reviewer(request)
    _run_action(
        request.app.state.knowledge_ingestion_service.approve,
        document_id,
        reviewer,
    )
    session.expire_all()
    return _detail(_get_source(session, document_id))


@router.post("/{document_id}/reject", response_model=KnowledgeDocumentDetail)
def reject_document(
    document_id: str,
    request: Request,
    session: Session = Depends(get_session),
) -> KnowledgeDocumentDetail:
    reviewer = _reviewer(request)
    _run_action(
        request.app.state.knowledge_ingestion_service.reject,
        document_id,
        reviewer,
    )
    session.expire_all()
    return _detail(_get_source(session, document_id))


@router.post("/{document_id}/archive", response_model=KnowledgeDocumentDetail)
def archive_document(
    document_id: str,
    request: Request,
    session: Session = Depends(get_session),
) -> KnowledgeDocumentDetail:
    reviewer = _reviewer(request)
    _run_action(
        request.app.state.knowledge_ingestion_service.archive,
        document_id,
        reviewer,
    )
    session.expire_all()
    return _detail(_get_source(session, document_id))


@router.post(
    "/{document_id}/retry",
    response_model=KnowledgeDocumentSummary,
    status_code=status.HTTP_202_ACCEPTED,
)
def retry_document(
    document_id: str,
    request: Request,
    session: Session = Depends(get_session),
) -> KnowledgeDocumentSummary:
    source = _get_source(session, document_id)
    if source.status == "parse_failed":
        try:
            request.app.state.knowledge_ingestion_executor.submit(document_id)
        except InvalidKnowledgeTransition as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return _summary(source)
    if source.status == "index_failed":
        _run_action(
            request.app.state.knowledge_ingestion_service.approve,
            document_id,
            _reviewer(request),
        )
        session.expire_all()
        return _summary(_get_source(session, document_id))
    raise HTTPException(status_code=409, detail="资料当前状态不可重试")


def _get_source(session: Session, document_id: str) -> KnowledgeSource:
    source = session.scalar(
        select(KnowledgeSource)
        .where(KnowledgeSource.id == document_id)
        .options(selectinload(KnowledgeSource.chunks))
    )
    if source is None:
        raise HTTPException(status_code=404, detail="未找到知识资料")
    return source


def _summary(source: KnowledgeSource) -> KnowledgeDocumentSummary:
    return KnowledgeDocumentSummary.model_validate(source)


def _detail(source: KnowledgeSource) -> KnowledgeDocumentDetail:
    summary = _summary(source).model_dump()
    return KnowledgeDocumentDetail(
        **summary,
        media_type=source.media_type,
        file_size=source.file_size,
        parse_warnings=source.parse_warnings or [],
        chunks=[
            KnowledgeChunkPreview.model_validate(chunk)
            for chunk in sorted(source.chunks, key=lambda item: (item.position, item.id))
        ],
    )


def _reviewer(request: Request) -> str:
    principal = getattr(request.state, "principal", None)
    if principal is None:
        raise HTTPException(status_code=401, detail="请先登录")
    return principal.username


def _run_action(action, document_id: str, reviewer: str) -> None:
    try:
        action(document_id, reviewer)
    except KnowledgeDocumentNotFound as exc:
        raise HTTPException(status_code=404, detail="未找到知识资料") from exc
    except InvalidKnowledgeTransition as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except KnowledgeIndexingFailed as exc:
        raise HTTPException(
            status_code=503,
            detail="资料索引失败，请重试审核。",
        ) from exc
