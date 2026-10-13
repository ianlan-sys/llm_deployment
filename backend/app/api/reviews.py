import csv
from datetime import datetime, timezone
from io import BytesIO, StringIO
import logging
from pathlib import Path
from time import perf_counter
import unicodedata
from uuid import uuid4
from zipfile import BadZipFile, ZipFile

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    Response,
    UploadFile,
    status,
)
from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session, selectinload

from app.database import get_session
from app.models import ReviewFinding, ReviewTask
from app.providers.feature_router import FeatureProviderSnapshot
from app.schemas import ReviewFindingResponse, ReviewTaskResponse
from app.services.review_parser import SUPPORTED_MEDIA_TYPES
from app.services.review_pipeline import ACTIVE_TASK_STATES, RETRYABLE_TASK_STATES
from app.services.audit import AuditService


router = APIRouter(prefix="/api/reviews", tags=["reviews"])
ALLOWED_DOCUMENT_TYPES = {
    "design_document",
    "technical_proposal",
    "tender_document",
}
CSV_HEADERS = [
    "风险等级",
    "问题描述",
    "原文位置",
    "原文摘录",
    "规则出处",
    "修改建议",
    "人工复核状态",
]
RISK_LABELS = {"high": "高", "medium": "中", "low": "低"}
HUMAN_REVIEW_LABELS = {
    "pending": "待复核",
    "confirmed": "已确认",
    "dismissed": "已忽略",
    "approved": "已通过",
    "rejected": "已驳回",
    "resolved": "已处理",
}
SUBMISSION_FAILURE_SUMMARY = "审核任务提交失败，请稍后重试。"
logger = logging.getLogger(__name__)


@router.post(
    "", response_model=ReviewTaskResponse, status_code=status.HTTP_202_ACCEPTED
)
async def create_review(
    request: Request,
    document_type: str = Form(...),
    file: UploadFile = File(...),
    session: Session = Depends(get_session),
) -> ReviewTaskResponse:
    started = perf_counter()
    if document_type not in ALLOWED_DOCUMENT_TYPES:
        raise HTTPException(status_code=422, detail="不支持的文档类型")
    filename = Path(file.filename or "").name
    extension = Path(filename).suffix.lower()
    expected_media_type = SUPPORTED_MEDIA_TYPES.get(extension)
    if expected_media_type is None or file.content_type != expected_media_type:
        raise HTTPException(
            status_code=415, detail="仅支持 PDF、DOCX 和 UTF-8 TXT 文件"
        )
    maximum = request.app.state.settings.max_upload_bytes
    content = await file.read(maximum + 1)
    if len(content) > maximum:
        raise HTTPException(
            status_code=413,
            detail="上传文件超过大小限制",
        )
    if not content:
        raise HTTPException(status_code=422, detail="上传文件不能为空")
    _validate_content(extension, content, maximum * 5)

    upload_dir = request.app.state.settings.runtime_dir / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    target = upload_dir / f"{uuid4()}{extension}"
    target.write_bytes(content)
    task = ReviewTask(
        document_filename=filename,
        document_type=document_type,
        file_path=str(target),
    )
    session.add(task)
    try:
        session.commit()
        session.refresh(task)
    except Exception:
        target.unlink(missing_ok=True)
        raise
    snapshot = request.app.state.feature_provider_router.snapshot(
        "document_review"
    )
    AuditService(session).record(
        "review.submitted",
        "queued",
        "Review submitted",
        snapshot.mode,
        task.id,
        int((perf_counter() - started) * 1000),
    )
    _submit_or_compensate(
        request,
        session,
        task.id,
        snapshot,
    )
    return _task_response(_get_task(session, task.id))


@router.get("", response_model=list[ReviewTaskResponse])
def list_reviews(session: Session = Depends(get_session)) -> list[ReviewTaskResponse]:
    tasks = session.scalars(
        select(ReviewTask)
        .options(selectinload(ReviewTask.findings).selectinload(ReviewFinding.rule))
        .order_by(ReviewTask.created_at.desc())
    ).all()
    return [_task_response(task) for task in tasks]


@router.get("/{task_id}", response_model=ReviewTaskResponse)
def get_review(
    task_id: str, session: Session = Depends(get_session)
) -> ReviewTaskResponse:
    return _task_response(_get_task(session, task_id))


@router.post(
    "/{task_id}/retry",
    response_model=ReviewTaskResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def retry_review(
    task_id: str,
    request: Request,
    session: Session = Depends(get_session),
) -> ReviewTaskResponse:
    started = perf_counter()
    task = _get_task(session, task_id)
    if task.status not in RETRYABLE_TASK_STATES:
        raise HTTPException(
            status_code=409,
            detail="仅失败、已取消或已中断的审核任务可以重试",
        )
    result = session.execute(
        update(ReviewTask)
        .where(
            ReviewTask.id == task_id,
            ReviewTask.status.in_(RETRYABLE_TASK_STATES),
        )
        .values(
            status="queued",
            stage="queued",
            progress=0,
            error_summary=None,
            started_at=None,
            completed_at=None,
        )
    )
    if result.rowcount != 1:
        session.rollback()
        raise HTTPException(status_code=409, detail="审核任务状态已变化，请刷新后重试")
    session.execute(
        delete(ReviewFinding).where(ReviewFinding.review_task_id == task_id)
    )
    session.commit()
    snapshot = request.app.state.feature_provider_router.snapshot(
        "document_review"
    )
    AuditService(session).record(
        "review.retried",
        "queued",
        "Review retried",
        snapshot.mode,
        task_id,
        int((perf_counter() - started) * 1000),
    )
    _submit_or_compensate(
        request,
        session,
        task_id,
        snapshot,
    )
    return _task_response(_get_task(session, task_id))


@router.post("/{task_id}/cancel", response_model=ReviewTaskResponse)
def cancel_review(
    task_id: str,
    request: Request,
    session: Session = Depends(get_session),
) -> ReviewTaskResponse:
    started = perf_counter()
    task = _get_task(session, task_id)
    if task.status not in ACTIVE_TASK_STATES:
        raise HTTPException(status_code=409, detail="审核任务当前不可取消")
    result = session.execute(
        update(ReviewTask)
        .where(
            ReviewTask.id == task_id,
            ReviewTask.status.in_(ACTIVE_TASK_STATES),
        )
        .values(status="cancelled", completed_at=datetime.now(timezone.utc))
    )
    if result.rowcount != 1:
        session.rollback()
        raise HTTPException(status_code=409, detail="审核任务状态已变化，请刷新后重试")
    session.commit()
    request.app.state.review_executor.cancel(task_id)
    mode = request.app.state.feature_provider_router.snapshot(
        "document_review"
    ).mode
    AuditService(session).record(
        "review.cancelled",
        "cancelled",
        "Review cancelled",
        mode,
        task_id,
        int((perf_counter() - started) * 1000),
    )
    return _task_response(_get_task(session, task_id))


@router.get("/{task_id}/export.csv")
def export_review(task_id: str, session: Session = Depends(get_session)) -> Response:
    task = _get_task(session, task_id)
    if task.status != "completed":
        raise HTTPException(status_code=409, detail="仅已完成的审核任务可以导出")
    output = StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(CSV_HEADERS)
    for finding in task.findings:
        writer.writerow(
            [
                _spreadsheet_safe(_risk_label(finding.risk)),
                _spreadsheet_safe(finding.description),
                _spreadsheet_safe(finding.location),
                _spreadsheet_safe(finding.excerpt),
                _spreadsheet_safe(finding.rule.source),
                _spreadsheet_safe(finding.recommendation),
                _spreadsheet_safe(_human_review_label(finding.human_review_status)),
            ]
        )
    return Response(
        content=output.getvalue().encode("utf-8-sig"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="review-{task.id}.csv"'},
    )


def _get_task(session: Session, task_id: str) -> ReviewTask:
    task = session.scalar(
        select(ReviewTask)
        .where(ReviewTask.id == task_id)
        .options(selectinload(ReviewTask.findings).selectinload(ReviewFinding.rule))
    )
    if task is None:
        raise HTTPException(status_code=404, detail="未找到审核任务")
    return task


def _task_response(task: ReviewTask) -> ReviewTaskResponse:
    return ReviewTaskResponse(
        id=task.id,
        document_filename=task.document_filename,
        document_type=task.document_type,
        status=task.status,
        stage=task.stage,
        progress=task.progress,
        error_summary=task.error_summary,
        started_at=task.started_at,
        completed_at=task.completed_at,
        created_at=task.created_at,
        updated_at=task.updated_at,
        findings=[
            ReviewFindingResponse(
                id=finding.id,
                rule_id=finding.review_rule_id,
                rule_source=finding.rule.source,
                risk=finding.risk,
                description=finding.description,
                location=finding.location,
                excerpt=finding.excerpt,
                recommendation=finding.recommendation,
                human_review_status=finding.human_review_status,
            )
            for finding in task.findings
        ],
    )


def _validate_content(extension: str, content: bytes, max_expanded_bytes: int) -> None:
    if extension == ".txt":
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise HTTPException(
                status_code=415, detail="TXT 文件必须使用 UTF-8 编码"
            ) from exc
        if "\x00" in text:
            raise HTTPException(status_code=415, detail="TXT 文件包含二进制内容")
        return
    if extension == ".pdf":
        if not content[:1024].lstrip().startswith(b"%PDF-"):
            raise HTTPException(status_code=415, detail="上传内容不是有效的 PDF 文件")
        return
    try:
        with ZipFile(BytesIO(content)) as archive:
            names = set(archive.namelist())
            if not {"[Content_Types].xml", "word/document.xml"} <= names:
                raise HTTPException(
                    status_code=415, detail="上传内容不是有效的 DOCX 文件"
                )
            if sum(item.file_size for item in archive.infolist()) > max_expanded_bytes:
                raise HTTPException(
                    status_code=413, detail="DOCX 解压后内容超过大小限制"
                )
    except BadZipFile as exc:
        raise HTTPException(
            status_code=415, detail="上传内容不是有效的 DOCX 文件"
        ) from exc


def _spreadsheet_safe(value: object) -> str:
    text = str(value)
    for character in text:
        if character.isspace() or unicodedata.category(character).startswith("C"):
            continue
        if character in "=+-@":
            return f"'{text}"
        break
    return text


def _risk_label(value: str) -> str:
    return RISK_LABELS.get(value, "未知")


def _human_review_label(value: str) -> str:
    return HUMAN_REVIEW_LABELS.get(value, "未知状态")


def _submit_or_compensate(
    request: Request,
    session: Session,
    task_id: str,
    snapshot: FeatureProviderSnapshot,
) -> None:
    try:
        request.app.state.review_executor.submit(
            task_id,
            snapshot,
        )
        return
    except Exception:
        logger.exception("审核任务 %s 提交到执行器失败", task_id)

    try:
        result = session.execute(
            update(ReviewTask)
            .where(ReviewTask.id == task_id, ReviewTask.status == "queued")
            .values(
                status="failed",
                error_summary=SUBMISSION_FAILURE_SUMMARY,
                completed_at=datetime.now(timezone.utc),
            )
        )
        if result.rowcount != 1:
            session.rollback()
            raise RuntimeError("无法将提交失败的审核任务更新为失败状态")
        session.commit()
        AuditService(session).record(
            "review.failed",
            "failed",
            "Review failed",
            snapshot.mode,
            task_id,
        )
    except Exception as exc:
        logger.exception("审核任务 %s 的失败补偿持久化失败", task_id)
        raise HTTPException(
            status_code=500,
            detail="审核任务提交失败，且无法保存失败状态，请稍后重试",
        ) from exc
