import asyncio
import json
from dataclasses import dataclass
from threading import Event, Lock, Thread
from time import monotonic, sleep

import anyio
import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.api import knowledge as knowledge_api
from app.models import AuditEvent, ModelRequestMetric
from app.providers.base import ProviderUnavailable, ProviderUsage
from app.providers.feature_router import FeatureProviderSnapshot
from app.schemas import ContextChunk
from app.services.inference import InferenceCoordinator, InferenceQueueFull
from app.services import knowledge as knowledge_service_module
from app.services.knowledge import (
    ANSWER_DISCLAIMER,
    NO_EVIDENCE_ANSWER,
    KnowledgeService,
)


@dataclass(frozen=True)
class Frame:
    event: str
    data: dict


def _login(client) -> None:
    response = client.post(
        "/api/auth/login",
        json={"username": "demo", "password": "correct horse"},
    )
    assert response.status_code == 200


def _parse_sse(response) -> list[Frame]:
    frames = []
    event = None
    data = None
    for line in response.iter_lines():
        if line.startswith("event: "):
            event = line.removeprefix("event: ")
        elif line.startswith("data: "):
            data = json.loads(line.removeprefix("data: "))
        elif not line and event is not None and data is not None:
            frames.append(Frame(event, data))
            event = None
            data = None
    return frames


class AnswerProvider:
    def __init__(self, pieces=("VLAN ", "配置回答")):
        self.pieces = pieces
        self.received_chunks: list[ContextChunk] = []

    @property
    def last_usage(self) -> ProviderUsage:
        return ProviderUsage(input_tokens=7, output_tokens=3)

    def reset_usage(self) -> None:
        return None

    def stream_answer_with_context(
        self, question: str, chunks: list[ContextChunk]
    ):
        self.received_chunks = chunks
        yield from self.pieces


class RecordingCoordinator:
    def __init__(self):
        self.recorded_requests = 0
        self.snapshots = []

    def stream(
        self,
        feature,
        operation,
        snapshot,
        iterator_factory,
        cancellation=None,
    ):
        self.recorded_requests += 1
        self.snapshots.append(snapshot)
        return iterator_factory()

    def shutdown(self):
        return True


def _install_local_feature(auth_client, monkeypatch, provider, coordinator):
    snapshot = FeatureProviderSnapshot(
        feature="knowledge",
        mode="local",
        model="qwen3:1.7b-q4_K_M",
        provider=provider,
    )
    router = auth_client.app.state.feature_provider_router
    original_snapshot = router.snapshot
    monkeypatch.setattr(
        router,
        "snapshot",
        lambda feature: snapshot
        if feature == "knowledge"
        else original_snapshot(feature),
    )
    auth_client.app.state.inference_coordinator = coordinator
    return snapshot


def test_stream_returns_retrieval_before_local_generation(
    auth_client, monkeypatch
):
    _login(auth_client)
    provider = AnswerProvider()
    coordinator = RecordingCoordinator()
    snapshot = _install_local_feature(
        auth_client, monkeypatch, provider, coordinator
    )
    clock = iter([10.0, 10.125])
    monkeypatch.setattr(
        knowledge_service_module, "monotonic", clock.__next__, raising=False
    )

    with auth_client.stream(
        "POST",
        "/api/knowledge/query/stream",
        json={"question": "核心交换机 VLAN 如何划分？", "scope": "核心节点A"},
    ) as response:
        frames = _parse_sse(response)

    assert response.status_code == 200
    assert [frame.event for frame in frames][:2] == ["retrieval", "meta"]
    assert frames[0].data["citations"]
    assert frames[1].data == {
        "mode": "local",
        "model": "qwen3:1.7b-q4_K_M",
    }
    assert (
        "".join(
            frame.data["content"]
            for frame in frames
            if frame.event == "delta"
        )
        == "VLAN 配置回答"
    )
    assert frames[-1].event == "done"
    assert frames[-1].data["status"] == "answered"
    assert frames[-1].data["disclaimer"] == ANSWER_DISCLAIMER
    assert frames[-1].data["elapsed_ms"] == 125
    assert coordinator.recorded_requests == 1
    assert coordinator.snapshots == [snapshot]
    assert len(provider.received_chunks) <= 4


def test_no_evidence_does_not_enter_inference_gate(
    auth_client, monkeypatch
):
    _login(auth_client)
    coordinator = RecordingCoordinator()
    _install_local_feature(
        auth_client, monkeypatch, AnswerProvider(), coordinator
    )
    monkeypatch.setattr(
        auth_client.app.state.knowledge_index_service,
        "search",
        lambda *_args, **_kwargs: [],
    )
    clock = iter([20.0, 20.25])
    monkeypatch.setattr(
        knowledge_service_module, "monotonic", clock.__next__, raising=False
    )

    with auth_client.stream(
        "POST",
        "/api/knowledge/query/stream",
        json={"question": "不存在的量子网络参数", "scope": None},
    ) as response:
        frames = _parse_sse(response)

    assert [frame.event for frame in frames] == ["retrieval", "done"]
    assert frames[0].data["citations"] == []
    assert frames[-1].data == {
        "status": "no_evidence",
        "answer": NO_EVIDENCE_ANSWER,
        "elapsed_ms": 250,
    }
    assert coordinator.recorded_requests == 0


def test_stream_provider_error_is_fixed_and_redacted(
    auth_client, monkeypatch
):
    class FailingProvider(AnswerProvider):
        def stream_answer_with_context(self, question, chunks):
            raise ProviderUnavailable(
                "SECRET upstream URL http://ollama:11434"
            )
            yield

    _login(auth_client)
    provider = FailingProvider()
    coordinator = RecordingCoordinator()
    _install_local_feature(
        auth_client, monkeypatch, provider, coordinator
    )
    question = "核心交换机 VLAN 如何划分？"

    with auth_client.stream(
        "POST",
        "/api/knowledge/query/stream",
        json={"question": question, "scope": "核心节点A"},
    ) as response:
        frames = _parse_sse(response)

    assert [frame.event for frame in frames] == [
        "retrieval",
        "meta",
        "error",
    ]
    assert frames[-1].data == {
        "status": "error",
        "message": "模型服务暂不可用，请重试或手动切换为 Mock 模式。",
    }
    serialized = json.dumps(
        [frame.data for frame in frames if frame.event == "error"],
        ensure_ascii=False,
    )
    assert question not in serialized
    assert "ollama" not in serialized.lower()
    with auth_client.app.state.session_factory() as session:
        audits = list(
            session.query(AuditEvent).filter(
                AuditEvent.event_type == "knowledge.query.failed"
            )
        )
    assert len(audits) == 1
    assert audits[0].request_summary == "Knowledge query processed"


class CloseTrackingIterator:
    def __init__(self):
        self.closed = Event()
        self._sent = False

    def __iter__(self):
        return self

    def __next__(self):
        if not self._sent:
            self._sent = True
            return "first"
        while not self.closed.wait(0.01):
            pass
        raise StopIteration

    def close(self):
        self.closed.set()

    def cancel(self):
        self.closed.set()


class CloseTrackingProvider(AnswerProvider):
    def __init__(self):
        super().__init__()
        self.iterator = CloseTrackingIterator()

    def stream_answer_with_context(self, question, chunks):
        self.received_chunks = chunks
        return self.iterator


class BlockingRequest:
    async def is_disconnected(self):
        return False


class BlockingEventIterator:
    def __init__(self):
        self.started = Event()
        self.closed = Event()

    def __iter__(self):
        return self

    def __next__(self):
        self.started.set()
        self.closed.wait()
        raise StopIteration

    def close(self):
        self.closed.set()


class BlockingHTTPStream(httpx.AsyncByteStream):
    def __init__(self):
        self.started = Event()
        self.closed = Event()
        self.cancel_observed = Event()
        self._lock = Lock()
        self._loop = None
        self._future = None

    def __aiter__(self):
        return self

    async def __anext__(self):
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        with self._lock:
            self._loop = loop
            self._future = future
        self.started.set()
        try:
            await future
        except asyncio.CancelledError:
            self.cancel_observed.set()
            raise
        raise StopAsyncIteration

    async def aclose(self):
        self.closed.set()
        self.release_for_test_cleanup()

    def release_for_test_cleanup(self):
        with self._lock:
            loop = self._loop
            future = self._future
        if (
            loop is not None
            and future is not None
            and not future.done()
        ):
            loop.call_soon_threadsafe(future.cancel)


class _BlockingBodyTransport(httpx.AsyncBaseTransport):
    def __init__(self, stream: BlockingHTTPStream):
        self.stream = stream

    async def handle_async_request(self, request):
        return httpx.Response(
            200,
            request=request,
            headers={"content-type": "text/event-stream"},
            stream=self.stream,
        )

    async def aclose(self):
        return None


class _StaticAsyncSSEStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield (
            b'data: {"choices":[{"delta":{"content":"after-cancel"}}]}'
            b"\n\ndata: [DONE]\n\n"
        )

    async def aclose(self):
        return None


class _PreResponseScenario:
    def __init__(self):
        self.request_started = Event()
        self.cancel_observed = Event()
        self.operation_finished = Event()
        self.body_started = Event()
        self.sync_release = Event()
        self._lock = Lock()
        self._calls = 0
        self.async_transport_closes = 0

    def next_call(self):
        with self._lock:
            self._calls += 1
            return self._calls

    def record_async_close(self):
        with self._lock:
            self.async_transport_closes += 1

    @property
    def calls(self):
        with self._lock:
            return self._calls


class _BlockingSyncSetupTransport(httpx.BaseTransport):
    def __init__(self, scenario: _PreResponseScenario):
        self.scenario = scenario

    def handle_request(self, request):
        assert self.scenario.next_call() == 1
        self.scenario.request_started.set()
        self.scenario.sync_release.wait()
        self.scenario.operation_finished.set()
        return httpx.Response(
            200,
            request=request,
            headers={"content-type": "text/event-stream"},
            content=b"data: [DONE]\n\n",
        )


class _CancellableAsyncSetupTransport(httpx.AsyncBaseTransport):
    def __init__(self, scenario: _PreResponseScenario):
        self.scenario = scenario

    async def handle_async_request(self, request):
        call = self.scenario.next_call()
        if call == 1:
            self.scenario.request_started.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                self.scenario.cancel_observed.set()
                raise
            finally:
                self.scenario.operation_finished.set()
        self.scenario.body_started.set()
        return httpx.Response(
            200,
            request=request,
            headers={"content-type": "text/event-stream"},
            stream=_StaticAsyncSSEStream(),
        )

    async def aclose(self):
        self.scenario.record_async_close()


@pytest.mark.asyncio
async def test_cancel_scope_shields_stream_close_before_waiting_for_next_worker():
    source = BlockingEventIterator()
    events = knowledge_api._event_stream(source, BlockingRequest())
    scopes = []

    async def consume():
        with anyio.CancelScope() as scope:
            scopes.append(scope)
            await anext(events)

    try:
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(consume)
            assert await anyio.to_thread.run_sync(source.started.wait, 1)
            scopes[0].cancel()
        assert source.closed.is_set()
    finally:
        source.close()
        await events.aclose()


@pytest.mark.asyncio
async def test_pre_response_cancel_ends_request_and_preserves_next_stream(
    db_session,
):
    from app.providers.openai_compatible import OpenAICompatibleProvider
    from app.seed import seed_demo_data
    from app.services.knowledge_index import KnowledgeIndexService

    seed_demo_data(db_session)
    index = KnowledgeIndexService()
    index.rebuild(db_session)
    scenario = _PreResponseScenario()
    sync_client = httpx.Client(
        transport=_BlockingSyncSetupTransport(scenario)
    )

    class ScenarioProvider(OpenAICompatibleProvider):
        def _new_knowledge_transport(self):
            return _CancellableAsyncSetupTransport(scenario)

    provider = ScenarioProvider(
        base_url="http://model.test/v1",
        model="qwen3:1.7b-q4_K_M",
        client=sync_client,
    )
    snapshot = FeatureProviderSnapshot(
        feature="knowledge",
        mode="local",
        model="qwen3:1.7b-q4_K_M",
        provider=provider,
    )
    coordinator = InferenceCoordinator(
        sessionmaker(bind=db_session.get_bind()),
        timeout_seconds=2,
    )
    knowledge_stream = KnowledgeService(
        db_session,
        index,
        coordinator,
        snapshot,
    ).stream("核心交换机 VLAN 如何划分？", "核心节点A")
    events = knowledge_api._event_stream(
        knowledge_stream, BlockingRequest()
    )
    pending = None
    second_events = None
    second_stream = None

    try:
        assert "event: retrieval" in await anext(events)
        assert "event: meta" in await anext(events)
        pending = asyncio.create_task(anext(events))
        assert await anyio.to_thread.run_sync(
            scenario.request_started.wait, 1
        )

        pending.cancel()
        done, _ = await asyncio.wait({pending}, timeout=0.3)

        assert pending in done
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert scenario.cancel_observed.wait(0.3)
        assert scenario.operation_finished.is_set()
        assert not scenario.body_started.is_set()
        assert coordinator.live_state().running == 0
        assert coordinator.live_state().queued == 0

        deadline = monotonic() + 1
        metrics = []
        while monotonic() < deadline:
            db_session.rollback()
            metrics = list(
                db_session.scalars(select(ModelRequestMetric))
            )
            if metrics:
                break
            await asyncio.sleep(0.01)
        assert len(metrics) == 1
        assert metrics[0].result_status == "cancelled"
        assert metrics[0].operation == "knowledge_answer"

        second_stream = KnowledgeService(
            db_session,
            index,
            coordinator,
            snapshot,
        ).stream("核心交换机 VLAN 如何划分？", "核心节点A")
        second_events = knowledge_api._event_stream(
            second_stream, BlockingRequest()
        )
        frames = []
        async for frame in second_events:
            frames.append(frame)

        assert any("event: delta" in frame for frame in frames)
        assert any("after-cancel" in frame for frame in frames)
        assert frames[-1].startswith("event: done")
        assert scenario.calls == 2
        assert scenario.async_transport_closes == 2
        assert coordinator.live_state().running == 0
        assert coordinator.live_state().queued == 0
    finally:
        scenario.sync_release.set()
        if pending is not None and not pending.done():
            await asyncio.wait({pending}, timeout=1)
        if pending is not None and pending.done():
            try:
                await pending
            except (asyncio.CancelledError, StopAsyncIteration):
                pass
        await events.aclose()
        knowledge_stream.close()
        if second_events is not None:
            await second_events.aclose()
        if second_stream is not None:
            second_stream.close()
        coordinator.shutdown(timeout_seconds=1)
        sync_client.close()


@pytest.mark.asyncio
async def test_realistic_http_stream_cancellation_aborts_transport_and_slot(
    db_session,
):
    from app.providers.openai_compatible import OpenAICompatibleProvider
    from app.seed import seed_demo_data
    from app.services.knowledge_index import KnowledgeIndexService

    seed_demo_data(db_session)
    index = KnowledgeIndexService()
    index.rebuild(db_session)
    transport_stream = BlockingHTTPStream()
    client = httpx.Client()

    class ScenarioProvider(OpenAICompatibleProvider):
        def _new_knowledge_transport(self):
            return _BlockingBodyTransport(transport_stream)

    provider = ScenarioProvider(
        base_url="http://model.test/v1",
        model="qwen3:1.7b-q4_K_M",
        client=client,
    )
    snapshot = FeatureProviderSnapshot(
        feature="knowledge",
        mode="local",
        model="qwen3:1.7b-q4_K_M",
        provider=provider,
    )
    coordinator = InferenceCoordinator(
        lambda: db_session,
        timeout_seconds=2,
    )
    knowledge_stream = KnowledgeService(
        db_session,
        index,
        coordinator,
        snapshot,
    ).stream("核心交换机 VLAN 如何划分？", "核心节点A")
    events = knowledge_api._event_stream(
        knowledge_stream, BlockingRequest()
    )
    pending = None

    try:
        assert "event: retrieval" in await anext(events)
        assert "event: meta" in await anext(events)
        pending = asyncio.create_task(anext(events))
        assert await anyio.to_thread.run_sync(
            transport_stream.started.wait, 1
        )
        assert coordinator.live_state().running == 1

        pending.cancel()
        done, _ = await asyncio.wait({pending}, timeout=0.3)

        assert pending in done
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert transport_stream.closed.is_set()
        assert transport_stream.cancel_observed.is_set()
        assert coordinator.live_state().running == 0
    finally:
        transport_stream.release_for_test_cleanup()
        if pending is not None and not pending.done():
            await asyncio.wait({pending}, timeout=1)
        if pending is not None and pending.done():
            try:
                await pending
            except (asyncio.CancelledError, StopAsyncIteration):
                pass
        await events.aclose()
        knowledge_stream.close()
        coordinator.shutdown(timeout_seconds=1)
        client.close()


@pytest.mark.asyncio
async def test_async_api_cancellation_closes_busy_knowledge_upstream(
    db_session,
):
    from app.seed import seed_demo_data
    from app.services.knowledge_index import KnowledgeIndexService

    seed_demo_data(db_session)
    index = KnowledgeIndexService()
    index.rebuild(db_session)
    provider = CloseTrackingProvider()
    snapshot = FeatureProviderSnapshot(
        feature="knowledge",
        mode="local",
        model="qwen3:1.7b-q4_K_M",
        provider=provider,
    )
    coordinator = InferenceCoordinator(lambda: db_session, timeout_seconds=2)
    stream = KnowledgeService(
        db_session,
        index,
        coordinator,
        snapshot,
    ).stream("核心交换机 VLAN 如何划分？", "核心节点A")
    events = knowledge_api._event_stream(stream, BlockingRequest())

    try:
        assert "event: retrieval" in await anext(events)
        assert "event: meta" in await anext(events)
        assert "event: delta" in await anext(events)
        pending = asyncio.create_task(anext(events))
        await asyncio.sleep(0.02)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending

        deadline = monotonic() + 0.5
        while monotonic() < deadline and (
            not provider.iterator.closed.is_set()
            or coordinator.live_state().running
        ):
            await asyncio.sleep(0.01)
        assert provider.iterator.closed.is_set()
        assert coordinator.live_state().running == 0
    finally:
        provider.iterator.close()
        await events.aclose()
        stream.close()
        coordinator.shutdown(timeout_seconds=1)


@pytest.mark.asyncio
async def test_async_api_cancellation_wakes_queued_request_without_provider(
    db_session,
):
    from app.seed import seed_demo_data
    from app.services.knowledge_index import KnowledgeIndexService

    class EagerProvider(AnswerProvider):
        def __init__(self):
            super().__init__()
            self.calls = 0

        def stream_answer_with_context(self, question, chunks):
            self.calls += 1
            return iter(("must not start",))

    seed_demo_data(db_session)
    index = KnowledgeIndexService()
    index.rebuild(db_session)
    provider = EagerProvider()
    snapshot = FeatureProviderSnapshot(
        feature="knowledge",
        mode="local",
        model="qwen3:1.7b-q4_K_M",
        provider=provider,
    )
    coordinator = InferenceCoordinator(
        lambda: db_session,
        concurrency=1,
        max_queue=1,
        timeout_seconds=2,
    )
    owner_release = Event()

    def owner_source():
        yield "owner"
        assert owner_release.wait(1)

    owner = coordinator.stream(
        "knowledge",
        "knowledge_answer",
        snapshot,
        owner_source,
    )
    assert next(owner) == "owner"
    stream = KnowledgeService(
        db_session,
        index,
        coordinator,
        snapshot,
    ).stream("核心交换机 VLAN 如何划分？", "核心节点A")
    events = knowledge_api._event_stream(stream, BlockingRequest())

    try:
        assert "event: retrieval" in await anext(events)
        assert "event: meta" in await anext(events)
        pending = asyncio.create_task(anext(events))
        deadline = monotonic() + 0.5
        while (
            monotonic() < deadline
            and coordinator.live_state().queued != 1
        ):
            await asyncio.sleep(0.005)
        assert coordinator.live_state().queued == 1

        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending

        assert provider.calls == 0
        assert coordinator.live_state().running == 1
        assert coordinator.live_state().queued == 0
    finally:
        await events.aclose()
        stream.close()
        owner_release.set()
        list(owner)
        owner.close()
        coordinator.shutdown(timeout_seconds=1)


def test_queue_full_never_constructs_eager_knowledge_provider_iterator(
    db_session,
):
    from app.seed import seed_demo_data
    from app.services.knowledge_index import KnowledgeIndexService

    class EagerProvider(AnswerProvider):
        def __init__(self):
            super().__init__()
            self.calls = 0

        def stream_answer_with_context(self, question, chunks):
            self.calls += 1
            return iter(("must not start",))

    seed_demo_data(db_session)
    index = KnowledgeIndexService()
    index.rebuild(db_session)
    provider = EagerProvider()
    snapshot = FeatureProviderSnapshot(
        feature="knowledge",
        mode="local",
        model="qwen3:1.7b-q4_K_M",
        provider=provider,
    )
    coordinator = InferenceCoordinator(
        lambda: db_session,
        concurrency=1,
        max_queue=0,
        timeout_seconds=2,
    )
    owner_started = Event()
    release_owner = Event()

    def owner_iterator():
        owner_started.set()
        assert release_owner.wait(1)
        yield "owner complete"

    owner = coordinator.stream(
        "knowledge",
        "knowledge_answer",
        snapshot,
        owner_iterator,
    )
    owner_thread = Thread(target=lambda: list(owner))
    owner_thread.start()
    assert owner_started.wait(1)
    stream = KnowledgeService(
        db_session,
        index,
        coordinator,
        snapshot,
    ).stream("核心交换机 VLAN 如何划分？", "核心节点A")

    try:
        assert next(stream).event == "retrieval"
        assert next(stream).event == "meta"
        with pytest.raises(InferenceQueueFull):
            next(stream)
        assert provider.calls == 0
    finally:
        stream.close()
        release_owner.set()
        owner_thread.join(timeout=1)
        owner.close()
        coordinator.shutdown(timeout_seconds=1)


def test_cancelling_knowledge_stream_closes_upstream_and_releases_admission(
    db_session,
):
    from app.seed import seed_demo_data
    from app.services.knowledge_index import KnowledgeIndexService

    seed_demo_data(db_session)
    index = KnowledgeIndexService()
    index.rebuild(db_session)
    provider = CloseTrackingProvider()
    snapshot = FeatureProviderSnapshot(
        feature="knowledge",
        mode="local",
        model="qwen3:1.7b-q4_K_M",
        provider=provider,
    )
    coordinator = InferenceCoordinator(lambda: db_session, timeout_seconds=2)
    stream = KnowledgeService(
        db_session,
        index,
        coordinator,
        snapshot,
    ).stream("核心交换机 VLAN 如何划分？", "核心节点A")

    assert next(stream).event == "retrieval"
    assert next(stream).event == "meta"
    assert next(stream).event == "delta"
    stream.close()

    deadline = monotonic() + 1
    while monotonic() < deadline and (
        not provider.iterator.closed.is_set()
        or coordinator.live_state().running
    ):
        sleep(0.01)
    try:
        assert provider.iterator.closed.is_set()
        assert coordinator.live_state().running == 0
        assert coordinator.live_state().queued == 0
    finally:
        coordinator.shutdown(timeout_seconds=1)


def test_retrieval_citations_are_server_validated_and_audits_are_redacted(
    db_session,
):
    from sqlalchemy import select

    from app.seed import seed_demo_data
    from app.services.knowledge_index import KnowledgeIndexService

    question = "核心交换机 VLAN 如何划分？"
    index = KnowledgeIndexService()
    seed_demo_data(db_session)
    index.rebuild(db_session)
    provider = AnswerProvider(("回答中伪造 [invented-chunk]",))
    coordinator = RecordingCoordinator()
    snapshot = FeatureProviderSnapshot(
        feature="knowledge",
        mode="local",
        model="qwen3:1.7b-q4_K_M",
        provider=provider,
    )
    events = list(
        KnowledgeService(
            db_session,
            index,
            coordinator,
            snapshot,
        ).stream(question, "核心节点A")
    )

    citations = events[0].data["citations"]
    retrieved_ids = {chunk.chunk_id for chunk in provider.received_chunks}
    assert citations
    assert {citation["chunk_id"] for citation in citations} == retrieved_ids
    assert "invented-chunk" not in {
        citation["chunk_id"] for citation in citations
    }
    audits = list(db_session.scalars(select(AuditEvent)))
    assert audits
    serialized = json.dumps(
        [
            {
                "summary": event.request_summary,
                "status": event.result_status,
                "reference": event.reference_id,
            }
            for event in audits
        ],
        ensure_ascii=False,
    )
    assert question not in serialized
    assert "伪造" not in serialized
