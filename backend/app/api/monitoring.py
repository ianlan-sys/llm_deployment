import re

from fastapi import APIRouter, Query, Request

from app.providers.feature_router import FEATURES
from app.schemas import (
    MonitoringFeatureSnapshot,
    MonitoringHistoryResponse,
    MonitoringInferenceStatus,
    MonitoringLiveResponse,
    MonitoringOllamaStatus,
)
from app.services.monitoring import MonitoringService


router = APIRouter(prefix="/api/monitoring", tags=["monitoring"])
MODEL_DISPLAY_LABEL = "Qwen3 1.7B Q4"
MAX_MODEL_SIZE_BYTES = 10_995_116_277_760
SEMVER_LIKE = re.compile(
    r"^(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z]+(?:[.-][0-9A-Za-z]+)*)?"
    r"(?:\+[0-9A-Za-z]+(?:[.-][0-9A-Za-z]+)*)?$"
)


def _safe_version(value: object) -> str | None:
    if (
        isinstance(value, str)
        and len(value) <= 64
        and SEMVER_LIKE.fullmatch(value)
    ):
        return value
    return None


def _safe_size_bytes(value: object) -> int | None:
    if (
        isinstance(value, int)
        and not isinstance(value, bool)
        and 0 <= value <= MAX_MODEL_SIZE_BYTES
    ):
        return value
    return None


@router.get("/live", response_model=MonitoringLiveResponse)
def live(request: Request) -> MonitoringLiveResponse:
    try:
        health = request.app.state.ollama_client.model_status()
        ollama = MonitoringOllamaStatus(
            available=bool(health.available),
            installed=bool(health.installed),
            loaded=bool(health.loaded),
            display_label=MODEL_DISPLAY_LABEL,
            version=_safe_version(getattr(health, "version", None)),
            size_bytes=_safe_size_bytes(
                getattr(health, "size_bytes", None)
            ),
        )
    except Exception:
        ollama = MonitoringOllamaStatus(
            available=False,
            installed=False,
            loaded=False,
            display_label=MODEL_DISPLAY_LABEL,
            version=None,
            size_bytes=None,
        )
    inference = request.app.state.inference_coordinator.live_state()
    feature_router = request.app.state.feature_provider_router
    return MonitoringLiveResponse(
        sampling_status=request.app.state.resource_sampler.status,
        ollama=ollama,
        inference=MonitoringInferenceStatus(
            running=inference.running,
            queued=inference.queued,
            concurrency=inference.concurrency,
            max_queue=inference.max_queue,
        ),
        features=[
            MonitoringFeatureSnapshot(
                feature=feature,
                mode=feature_router.snapshot(feature).mode,
            )
            for feature in FEATURES
        ],
    )


@router.get("/history", response_model=MonitoringHistoryResponse)
def history(
    request: Request,
    hours: int = Query(default=24, ge=1, le=24),
) -> MonitoringHistoryResponse:
    return MonitoringService(
        request.app.state.session_factory
    ).history(hours=hours)
