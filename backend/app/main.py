from contextlib import ExitStack, asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from app.auth_middleware import AuthMiddleware
from app.api.auth import router as auth_router
from sqlalchemy.orm import Session

from app.api.settings import router as settings_router
from app.api.knowledge import router as knowledge_router
from app.api.knowledge_documents import router as knowledge_documents_router
from app.api.calls import router as calls_router
from app.api.dashboard import router as dashboard_router
from app.api.reviews import router as reviews_router
from app.api.monitoring import router as monitoring_router
from app.config import Settings
from app.database import configure_database
from app.migrations import run_schema_migrations
from app.providers.ollama import OllamaClient
from app.providers.feature_router import FeatureProviderRouter
from app.providers.registry import ProviderRegistry
from app.seed import seed_demo_data
from app.services.inference import InferenceCoordinator
from app.services.knowledge_ingestion import (
    KnowledgeIngestionExecutor,
    KnowledgeIngestionService,
)
from app.services.knowledge_index import KnowledgeIndexService
from app.services.monitoring import ResourceSampler
from app.services.review_executor import ReviewExecutor, interrupt_unfinished_tasks


def create_app(settings: Settings | None = None) -> FastAPI:
    cfg = settings or Settings()
    cfg.runtime_dir.mkdir(parents=True, exist_ok=True)
    api_key = cfg.openai_api_key.get_secret_value() if cfg.openai_api_key else None
    registry = ProviderRegistry(
        mode=cfg.provider_mode,
        base_url=cfg.openai_base_url,
        model=cfg.openai_model,
        api_key=api_key,
        timeout_seconds=cfg.provider_timeout_seconds,
    )
    ollama_client = OllamaClient(
        base_url=cfg.ollama_base_url,
        model=cfg.ollama_model,
        timeout_seconds=cfg.ollama_timeout_seconds,
    )
    feature_provider_router = FeatureProviderRouter(None, ollama_client)
    knowledge_index_service = KnowledgeIndexService()
    cfg.openai_api_key = None

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        with ExitStack() as cleanup:
            cleanup.callback(ollama_client.close)
            engine = configure_database(cfg.resolved_database_url())
            cleanup.callback(engine.dispose)
            run_schema_migrations(engine)
            with Session(engine) as session:
                seed_demo_data(session)
                registry.load_from_settings(session)
                feature_provider_router.load(session)
                interrupt_unfinished_tasks(
                    session,
                    feature_provider_router.snapshot(
                        "document_review"
                    ).mode,
                )
                knowledge_index_service.rebuild(session)
            app.state.engine = engine
            app.state.session_factory = lambda: Session(engine)
            inference_coordinator = InferenceCoordinator(
                app.state.session_factory,
                telemetry_database_url=cfg.resolved_database_url(),
            )
            cleanup.callback(inference_coordinator.shutdown)
            app.state.inference_coordinator = inference_coordinator
            app.state.knowledge_index_service = knowledge_index_service
            review_executor = ReviewExecutor(
                app.state.session_factory,
                inference_coordinator,
            )
            cleanup.callback(review_executor.shutdown)
            app.state.review_executor = review_executor
            knowledge_ingestion_service = KnowledgeIngestionService(
                app.state.session_factory,
                feature_provider_router.snapshot("knowledge").mode,
                knowledge_index_service,
            )
            knowledge_ingestion_service.recover_interrupted()
            knowledge_ingestion_executor = KnowledgeIngestionExecutor(
                knowledge_ingestion_service
            )
            cleanup.callback(knowledge_ingestion_executor.shutdown)
            app.state.knowledge_ingestion_service = (
                knowledge_ingestion_service
            )
            app.state.knowledge_ingestion_executor = (
                knowledge_ingestion_executor
            )
            resource_sampler = ResourceSampler(
                app.state.session_factory,
                ollama_status=lambda: ollama_client.model_status(),
                inference_state=inference_coordinator.live_state,
            )
            app.state.resource_sampler = resource_sampler
            cleanup.callback(resource_sampler.stop)
            resource_sampler.start()
            yield

    app = FastAPI(
        title="本地化大模型应用平台",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.settings = cfg
    app.state.ollama_client = ollama_client
    app.state.provider_registry = registry
    app.state.feature_provider_router = feature_provider_router

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_: Request, __: RequestValidationError) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": "请求参数无效"})

    app.add_middleware(
        CORSMiddleware,
        allow_origins=cfg.cors_origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(AuthMiddleware)

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {
            "status": "ok",
            "mode": feature_provider_router.snapshot("call_assistant").mode,
            "version": "0.1.0",
        }

    app.include_router(auth_router)
    app.include_router(settings_router)
    app.include_router(knowledge_router)
    app.include_router(knowledge_documents_router)
    app.include_router(calls_router)
    app.include_router(reviews_router)
    app.include_router(dashboard_router)
    app.include_router(monitoring_router)
    return app


app = create_app()
