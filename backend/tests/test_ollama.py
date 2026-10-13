import httpx
import pytest

from app.providers.base import ProviderUnavailable
from app.providers.ollama import (
    OllamaClient,
    OllamaHealth,
    OllamaModel,
    OllamaRunningModel,
)


MODEL = "qwen3:1.7b-q4_K_M"


def test_ollama_health_requires_exact_model_tag():
    def responder(request: httpx.Request) -> httpx.Response:
        payloads = {
            "/api/version": {"version": "0.32.1"},
            "/api/tags": {"models": [
                {"name": "qwen3:1.7b-q4_K_M:latest", "size": 1},
                {"name": MODEL, "size": 1_400_000_000},
            ]},
            "/api/ps": {"models": [{"name": MODEL, "size": 1_400_000_000}]},
        }
        return httpx.Response(200, json=payloads[request.url.path])

    client = OllamaClient(
        "http://ollama:11434",
        MODEL,
        client=httpx.Client(transport=httpx.MockTransport(responder)),
    )

    assert client.model_status() == OllamaHealth(
        available=True,
        version="0.32.1",
        model=MODEL,
        installed=True,
        loaded=True,
        size_bytes=1_400_000_000,
    )


def test_ollama_returns_typed_tag_and_running_model_lists():
    def responder(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": MODEL, "size": 42}]})
        return httpx.Response(200, json={"models": [{"name": MODEL, "size": 24}]})

    client = OllamaClient(
        "http://ollama:11434/",
        MODEL,
        client=httpx.Client(transport=httpx.MockTransport(responder)),
    )

    assert client.tags() == [OllamaModel(name=MODEL, size_bytes=42)]
    assert client.running_models() == [OllamaRunningModel(name=MODEL, size_bytes=24)]


@pytest.mark.parametrize(
    ("path", "response", "expected_message"),
    [
        ("/api/version", httpx.Response(200, content=b"not-json"), "本地模型服务响应无效"),
        ("/api/version", httpx.Response(200, json={}), "本地模型服务响应无效"),
        ("/api/tags", httpx.Response(200, json={"models": [{}]}), "本地模型服务响应无效"),
        ("/api/ps", httpx.Response(200, json={"models": "not-a-list"}), "本地模型服务响应无效"),
        ("/api/version", httpx.Response(503, text="super-secret upstream body"), "本地模型服务暂不可用"),
    ],
)
def test_ollama_normalizes_invalid_upstream_responses_without_body_leakage(
    path: str, response: httpx.Response, expected_message: str,
):
    def responder(request: httpx.Request) -> httpx.Response:
        if request.url.path == path:
            return response
        if request.url.path == "/api/version":
            return httpx.Response(200, json={"version": "0.32.1"})
        return httpx.Response(200, json={"models": []})

    client = OllamaClient(
        "http://ollama:11434",
        MODEL,
        client=httpx.Client(transport=httpx.MockTransport(responder)),
    )

    with pytest.raises(ProviderUnavailable) as raised:
        client.model_status()

    assert "super-secret" not in str(raised.value)
    assert str(raised.value) == expected_message


@pytest.mark.parametrize("failure", [
    httpx.ConnectError("network unavailable"),
    httpx.ReadTimeout("upstream timeout"),
])
def test_ollama_normalizes_connection_and_timeout_failures(failure: httpx.HTTPError):
    def unavailable(_: httpx.Request) -> httpx.Response:
        raise failure

    client = OllamaClient(
        "http://ollama:11434",
        MODEL,
        client=httpx.Client(transport=httpx.MockTransport(unavailable)),
    )

    with pytest.raises(ProviderUnavailable, match="本地模型服务暂不可用"):
        client.model_status()


def test_ollama_client_close_delegates_to_http_client():
    class CloseSpy:
        closed = False

        def close(self) -> None:
            self.closed = True

    spy = CloseSpy()
    client = OllamaClient("http://ollama:11434", MODEL, client=spy)  # type: ignore[arg-type]

    client.close()

    assert spy.closed is True
