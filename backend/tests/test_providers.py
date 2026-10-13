import asyncio
import json

import httpx
import pytest

from app.providers.base import ProviderUnavailable, ProviderUsage
from app.providers.mock import MockProvider
from app.providers.openai_compatible import OpenAICompatibleProvider
from app.schemas import ContextChunk, ReviewRuleInput


def test_mock_provider_is_deterministic():
    provider = MockProvider(delay_ms=0)

    first = list(provider.stream_call_suggestion(call_context={"intent": "故障报修"}, templates=[]))
    second = list(provider.stream_call_suggestion(call_context={"intent": "故障报修"}, templates=[]))

    assert first == second
    assert "故障" in "".join(first)


def test_mock_provider_returns_schema_valid_results():
    provider = MockProvider(delay_ms=0)
    chunks = [ContextChunk(chunk_id="chunk-1", source_id="source-1", text="设备故障请登记工单。", location="第 1 节")]
    rules = [
        ReviewRuleInput(
            id="rule-1",
            document_type="技术方案",
            check_item="应急响应",
            criterion="应包含应急响应时限",
            source="要求",
            default_risk="high",
        )
    ]

    assert provider.health_check().available is True
    assert provider.generate_call_summary({
        "scenario": {"id": "scenario-1", "name": "故障报修"},
        "original_transcript": "客户反馈故障",
        "accepted_reply": "已登记并承诺回访",
    }).appeal
    assert provider.answer_with_context("如何报修？", chunks).citations[0].chunk_id == "chunk-1"
    assert provider.review_section("技术方案", "本项目服务方案。", rules)[0].rule_id == "rule-1"
    assert provider.last_usage == ProviderUsage(
        input_tokens=None, output_tokens=None
    )


def test_openai_provider_converts_http_errors_to_provider_unavailable():
    def unavailable(_: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": {"message": "offline"}})

    provider = OpenAICompatibleProvider(
        base_url="http://model.test/v1",
        model="demo-model",
        api_key="secret",
        client=httpx.Client(transport=httpx.MockTransport(unavailable)),
    )

    with pytest.raises(ProviderUnavailable):
        provider.health_check()


def test_openai_provider_validates_structured_json_response():
    def completion(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer secret"
        body = json.loads(request.content)
        assert body["model"] == "demo-model"
        assert body["reasoning_effort"] == "none"
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": json.dumps({
                    "appeal": "报修",
                    "process": "登记",
                    "result": "已受理",
                    "follow_up": "跟进",
                })}}],
                "usage": {"prompt_tokens": 17, "completion_tokens": 8},
            },
        )

    provider = OpenAICompatibleProvider(
        base_url="http://model.test/v1/",
        model="demo-model",
        api_key="secret",
        client=httpx.Client(transport=httpx.MockTransport(completion)),
    )

    summary_context = {
        "scenario": {"id": "scenario-1", "name": "故障报修", "category": "故障报修"},
        "original_transcript": "客户来电反映网络故障",
        "accepted_reply": "已人工编辑：安排两小时内回访",
    }
    assert provider.generate_call_summary(summary_context).result == "已受理"
    assert provider.last_usage == ProviderUsage(input_tokens=17, output_tokens=8)


def test_openai_summary_prompt_uses_scenario_transcript_and_edited_reply():
    captured = {}

    def completion(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        captured.update(json.loads(body["messages"][1]["content"]))
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({
            "appeal": "报修", "process": "登记", "result": "已受理", "follow_up": "回访"
        }, ensure_ascii=False)}}]})

    provider = OpenAICompatibleProvider(
        base_url="http://model.test/v1",
        model="demo-model",
        client=httpx.Client(transport=httpx.MockTransport(completion)),
    )
    context = {
        "scenario": {"id": "scenario-1", "name": "服务投诉", "category": "服务投诉"},
        "original_transcript": "客户对服务时效不满",
        "accepted_reply": "已编辑并承诺明日回访",
    }

    provider.generate_call_summary(context)

    assert captured == context


def test_openai_provider_rejects_non_positive_timeout():
    with pytest.raises(ValueError, match="timeout"):
        OpenAICompatibleProvider(base_url="http://model.test/v1", model="demo", timeout_seconds=0)


def test_openai_provider_converts_malformed_structured_json_to_provider_unavailable():
    def malformed(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "not-json"}}]})

    provider = OpenAICompatibleProvider(
        base_url="http://model.test/v1",
        model="demo-model",
        client=httpx.Client(transport=httpx.MockTransport(malformed)),
    )

    with pytest.raises(ProviderUnavailable, match="structured JSON"):
        provider.generate_call_summary({
            "scenario": {"id": "scenario-1"},
            "original_transcript": "customer call",
            "accepted_reply": "accepted reply",
        })


def test_openai_provider_reads_sse_chat_completion_chunks():
    def stream(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["reasoning_effort"] == "none"
        assert body["stream_options"] == {"include_usage": True}
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=(
                b'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n'
                b'data: {"choices":[{"delta":{"content":"."}}]}\n\n'
                b'data: {"choices":[],"usage":{"prompt_tokens":11,"completion_tokens":2}}\n\n'
                b'data: [DONE]\n\n'
            ),
        )

    class TestProvider(OpenAICompatibleProvider):
        def _new_call_transport(self):
            return httpx.MockTransport(stream)

    provider = TestProvider(
        base_url="http://model.test/v1",
        model="demo-model",
    )

    assert list(provider.stream_call_suggestion({"intent": "consult"}, [])) == ["hello", "."]
    assert provider.last_usage == ProviderUsage(input_tokens=11, output_tokens=2)


def test_openai_provider_streams_knowledge_answer_text_without_model_citations():
    def stream(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        system = body["messages"][0]["content"]
        user = json.loads(body["messages"][1]["content"])
        assert "只输出中文回答正文" in system
        assert "不要输出引用编号" in system
        assert user == {
            "question": "VLAN 如何配置？",
            "chunks": [
                {
                    "chunk_id": "chunk-1",
                    "source_id": "source-1",
                    "text": "VLAN 410 使用网关 10.0.0.1。",
                    "location": "网络参数",
                }
            ],
        }
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=(
                b'data: {"choices":[{"delta":{"content":"answer"}}]}\n\n'
                b"data: [DONE]\n\n"
            ),
        )

    class TestProvider(OpenAICompatibleProvider):
        def _new_knowledge_transport(self):
            return httpx.MockTransport(stream)

    provider = TestProvider(
        base_url="http://model.test/v1",
        model="demo-model",
        client=httpx.Client(transport=httpx.MockTransport(stream)),
    )
    chunks = [
        ContextChunk(
            chunk_id="chunk-1",
            source_id="source-1",
            text="VLAN 410 使用网关 10.0.0.1。",
            location="网络参数",
        )
    ]

    assert list(
        provider.stream_answer_with_context("VLAN 如何配置？", chunks)
    ) == ["answer"]


def test_openai_knowledge_stream_drains_async_generator_finalizers_before_closing_loop(
    monkeypatch,
):
    finalized = []
    shutdown_calls = []
    create_loop = asyncio.new_event_loop

    def create_tracked_loop():
        loop = create_loop()
        shutdown_asyncgens = loop.shutdown_asyncgens

        async def tracked_shutdown_asyncgens():
            shutdown_calls.append(True)
            await shutdown_asyncgens()

        loop.shutdown_asyncgens = tracked_shutdown_asyncgens
        return loop

    monkeypatch.setattr(asyncio, "new_event_loop", create_tracked_loop)

    class TestProvider(OpenAICompatibleProvider):
        async def _stream_knowledge_completion(self, messages):
            async def resource():
                try:
                    yield "answer"
                finally:
                    await asyncio.sleep(0)
                    finalized.append(True)

            abandoned = resource()
            yield await anext(abandoned)

    provider = TestProvider(
        base_url="http://model.test/v1",
        model="demo-model",
    )
    chunks = [
        ContextChunk(
            chunk_id="chunk-1",
            source_id="source-1",
            text="VLAN 410",
            location="网络参数",
        )
    ]

    assert list(
        provider.stream_answer_with_context("VLAN 如何配置？", chunks)
    ) == ["answer"]
    assert finalized == [True]
    assert shutdown_calls == [True]


def test_cancelled_openai_knowledge_stream_never_starts_http_request():
    requests = []

    def stream(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=b"data: [DONE]\n\n",
        )

    class TestProvider(OpenAICompatibleProvider):
        def _new_knowledge_transport(self):
            return httpx.MockTransport(stream)

    provider = TestProvider(
        base_url="http://model.test/v1",
        model="demo-model",
    )
    chunks = [
        ContextChunk(
            chunk_id="chunk-1",
            source_id="source-1",
            text="VLAN 410",
            location="网络参数",
        )
    ]
    answer = provider.stream_answer_with_context(
        "VLAN 如何配置？", chunks
    )

    answer.cancel()

    assert list(answer) == []
    assert requests == []
