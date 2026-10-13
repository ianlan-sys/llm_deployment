from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from time import perf_counter

from app.database import get_session
from app.providers.base import ProviderUnavailable
from app.providers.feature_router import FeatureProviderRouter
from app.providers.registry import ProviderRegistry
from app.schemas import (
    FeatureKey,
    FeatureProviderConnectionTestRequest,
    FeatureProviderSettingsResponse,
    FeatureProviderSettingsUpdate,
    ProviderConnectionTestRequest,
    ProviderHealth,
    ProviderSettingsResponse,
    ProviderSettingsUpdate,
)
from app.services.audit import AuditService

router = APIRouter(prefix="/api/settings", tags=["settings"])


def get_registry(request: Request) -> ProviderRegistry:
    return request.app.state.provider_registry


def get_feature_router(request: Request) -> FeatureProviderRouter:
    return request.app.state.feature_provider_router


def validate_real_provider(mode: str, base_url: str | None, model: str | None) -> None:
    if mode == "real" and (not base_url or not model):
        raise HTTPException(status_code=422, detail="真实模型模式需要填写服务地址和模型名称")


@router.get("/provider", response_model=ProviderSettingsResponse)
def get_provider_settings(registry: ProviderRegistry = Depends(get_registry)) -> ProviderSettingsResponse:
    return registry.settings_response()


@router.put("/provider", response_model=ProviderSettingsResponse)
def update_provider_settings(
    payload: ProviderSettingsUpdate,
    session: Session = Depends(get_session),
    registry: ProviderRegistry = Depends(get_registry),
) -> ProviderSettingsResponse:
    validate_real_provider(payload.mode, payload.base_url, payload.model)
    started = perf_counter()
    snapshot = registry.configure_and_persist(
        session, payload.mode, payload.base_url, payload.model
    )
    AuditService(session).record(
        "settings.provider.updated",
        "completed",
        "Provider settings updated",
        snapshot.mode,
        elapsed_ms=int((perf_counter() - started) * 1000),
    )
    return registry.settings_response()


@router.post("/provider/test", response_model=ProviderHealth)
def test_provider_connection(
    payload: ProviderConnectionTestRequest,
    session: Session = Depends(get_session),
    registry: ProviderRegistry = Depends(get_registry),
) -> ProviderHealth:
    validate_real_provider(payload.mode, payload.base_url, payload.model)
    started = perf_counter()
    result = registry.test(payload.mode, payload.base_url, payload.model, None)
    AuditService(session).record(
        "settings.provider.tested",
        "available" if result.available else "unavailable",
        "Provider connection tested",
        payload.mode,
        elapsed_ms=int((perf_counter() - started) * 1000),
    )
    return result


@router.get("/features", response_model=list[FeatureProviderSettingsResponse])
def get_feature_settings(
    feature_router: FeatureProviderRouter = Depends(get_feature_router),
) -> list[FeatureProviderSettingsResponse]:
    return [_feature_response(snapshot) for snapshot in feature_router.snapshots()]


@router.put(
    "/features/{feature}", response_model=FeatureProviderSettingsResponse
)
def update_feature_settings(
    feature: FeatureKey,
    payload: FeatureProviderSettingsUpdate,
    session: Session = Depends(get_session),
    feature_router: FeatureProviderRouter = Depends(get_feature_router),
) -> FeatureProviderSettingsResponse:
    started = perf_counter()
    try:
        snapshot = feature_router.configure_and_persist(
            session, feature, payload.mode, payload.model
        )
    except ProviderUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    AuditService(session).record(
        "settings.feature.updated",
        "completed",
        "Feature provider settings updated",
        snapshot.mode,
        elapsed_ms=int((perf_counter() - started) * 1000),
    )
    return _feature_response(snapshot)


@router.post(
    "/features/{feature}/test", response_model=ProviderHealth
)
def test_feature_connection(
    feature: FeatureKey,
    payload: FeatureProviderConnectionTestRequest,
    session: Session = Depends(get_session),
    feature_router: FeatureProviderRouter = Depends(get_feature_router),
) -> ProviderHealth:
    started = perf_counter()
    result = feature_router.test(feature, payload.mode, payload.model)
    AuditService(session).record(
        "settings.feature.tested",
        "available" if result.available else "unavailable",
        "Feature provider connection tested",
        payload.mode,
        elapsed_ms=int((perf_counter() - started) * 1000),
    )
    return result


def _feature_response(snapshot) -> FeatureProviderSettingsResponse:
    return FeatureProviderSettingsResponse(
        feature=snapshot.feature,
        mode=snapshot.mode,
        model=snapshot.model,
    )
