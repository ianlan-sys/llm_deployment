import asyncio
import json
from collections.abc import AsyncIterator, Iterator
from threading import Event, Lock, local
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, TypeAdapter, ValidationError

from app.providers.base import ProviderUnavailable, ProviderUsage
from app.schemas import (
    CallSummary,
    ContextChunk,
    KnowledgeAnswer,
    ProviderHealth,
    ReviewFindingInput,
    ReviewRuleInput,
)


StructuredModel = TypeVar("StructuredModel", bound=BaseModel)


class _OpenAIKnowledgeStream(Iterator[str]):
    """Runs one async request task on its coordinator worker thread."""

    def __init__(
        self,
        provider: "OpenAICompatibleProvider",
        messages: list[dict[str, str]],
    ):
        self._provider = provider
        self._messages = messages
        self._cancelled = Event()
        self._lock = Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task[str] | None = None
        self._closed = False
        self._iterator = self._run()

    def __iter__(self) -> "_OpenAIKnowledgeStream":
        return self

    def __next__(self) -> str:
        with self._lock:
            if self._closed or self._cancelled.is_set():
                should_stop = True
                loop = None
                task = None
            else:
                should_stop = False
                loop = self._loop
                if loop is None:
                    loop = asyncio.new_event_loop()
                    self._loop = loop
                task = loop.create_task(anext(self._iterator))
                self._task = task
        if should_stop:
            self.close()
            raise StopIteration

        exhausted = False
        try:
            return loop.run_until_complete(task)
        except StopAsyncIteration:
            exhausted = True
        finally:
            with self._lock:
                if self._task is task:
                    self._task = None
        if exhausted:
            self.close()
            raise StopIteration
        raise RuntimeError("unreachable stream state")

    def cancel(self) -> None:
        self._cancelled.set()
        with self._lock:
            loop = self._loop
            task = self._task
        if (
            loop is not None
            and task is not None
            and not task.done()
        ):
            loop.call_soon_threadsafe(task.cancel)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            loop = self._loop
            self._loop = None
            self._task = None
        if loop is None:
            return
        try:
            loop.run_until_complete(self._iterator.aclose())
        finally:
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            finally:
                loop.close()

    async def _run(self) -> AsyncIterator[str]:
        async for item in self._provider._stream_knowledge_completion(
            self._messages
        ):
            yield item


class _OpenAICallStream(_OpenAIKnowledgeStream):
    """Runs a cancellable local call request on its worker event loop."""

    async def _run(self) -> AsyncIterator[str]:
        async for item in self._provider._stream_call_completion(
            self._messages
        ):
            yield item


class OpenAICompatibleProvider:
    """Minimal adapter for local services implementing OpenAI chat completions."""

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str | None = None,
        timeout_seconds: float = 30.0,
        client: httpx.Client | None = None,
    ):
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero")
        if not base_url.strip() or not model.strip():
            raise ValueError("base_url and model are required")
        self.base_url = base_url.rstrip("/")
        self.model = model
        self._api_key = api_key
        self.timeout_seconds = timeout_seconds
        self._client = client or httpx.Client(timeout=timeout_seconds)
        self._usage_state = local()

    @property
    def last_usage(self) -> ProviderUsage:
        return getattr(
            self._usage_state,
            "value",
            ProviderUsage(input_tokens=None, output_tokens=None),
        )

    def reset_usage(self) -> None:
        self._set_usage(None)

    def health_check(self) -> ProviderHealth:
        self._completion([{"role": "user", "content": "Reply with OK."}])
        return ProviderHealth(available=True, detail="模型连接成功")

    def stream_call_suggestion(
        self,
        call_context: dict,
        templates: list[dict],
    ) -> Iterator[str]:
        prompt = json.dumps({"call_context": call_context, "templates": templates}, ensure_ascii=False)
        return _OpenAICallStream(
            self,
            [
                {
                    "role": "system",
                    "content": "你是客服话术助手，请只输出可直接使用的中文建议。",
                },
                {"role": "user", "content": prompt},
            ],
        )

    def generate_call_summary(self, call_context: dict) -> CallSummary:
        return self._structured(
            CallSummary,
            "结合场景上下文、原始通话记录和人工编辑后的采纳回复，总结为 JSON 对象，"
            "字段必须为 appeal、process、result、follow_up。",
            json.dumps(call_context, ensure_ascii=False),
        )

    def answer_with_context(self, question: str, chunks: list[ContextChunk]) -> KnowledgeAnswer:
        context = [chunk.model_dump() for chunk in chunks]
        return self._structured(
            KnowledgeAnswer,
            "仅使用所给资料回答。返回 JSON 对象，包含 answer 和 citations；citation 必须有 chunk_id、source_id、location。",
            json.dumps({"question": question, "chunks": context}, ensure_ascii=False),
        )

    def stream_answer_with_context(
        self, question: str, chunks: list[ContextChunk]
    ) -> Iterator[str]:
        context = [chunk.model_dump() for chunk in chunks]
        return _OpenAIKnowledgeStream(
            self,
            [
                {
                    "role": "system",
                    "content": (
                        "你是综合运维知识助手。只能依据用户提供的资料片段"
                        "回答，不得补充片段之外的事实。资料不足时只输出"
                        "“知识库中暂无可靠依据”。只输出中文回答正文，"
                        "不要输出引用编号、JSON、思考过程或 Markdown 引用。"
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {"question": question, "chunks": context},
                        ensure_ascii=False,
                    ),
                },
            ],
        )

    def review_section(
        self, document_type: str, section: str, rules: list[ReviewRuleInput]
    ) -> list[ReviewFindingInput]:
        raw = self._completion([
            {"role": "system", "content": "审查文本并只返回 JSON 数组，每项字段为 rule_id、risk、description、location、excerpt、recommendation。"},
            {"role": "user", "content": json.dumps({
                "document_type": document_type,
                "section": section,
                "rules": [rule.model_dump() for rule in rules],
            }, ensure_ascii=False)},
        ])
        return self._validate_json(raw, TypeAdapter(list[ReviewFindingInput]))

    def _structured(self, schema: type[StructuredModel], instruction: str, content: str) -> StructuredModel:
        raw = self._completion([
            {"role": "system", "content": instruction},
            {"role": "user", "content": content},
        ])
        return self._validate_json(raw, TypeAdapter(schema))

    def _completion(self, messages: list[dict[str, str]]) -> str:
        self._set_usage(None)
        response = self._request(messages, stream=False)
        try:
            payload: dict[str, Any] = response.json()
            self._set_usage(payload.get("usage"))
            content = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ProviderUnavailable("Provider returned an invalid chat completion") from exc
        if not isinstance(content, str):
            raise ProviderUnavailable("Provider returned an invalid chat completion")
        return content

    async def _stream_call_completion(
        self,
        messages: list[dict[str, str]],
    ) -> AsyncIterator[str]:
        async for item in self._stream_async_completion(
            messages,
            self._new_call_transport(),
        ):
            yield item

    async def _stream_knowledge_completion(
        self,
        messages: list[dict[str, str]],
    ) -> AsyncIterator[str]:
        async for item in self._stream_async_completion(
            messages,
            self._new_knowledge_transport(),
        ):
            yield item

    async def _stream_async_completion(
        self,
        messages: list[dict[str, str]],
        transport: httpx.AsyncBaseTransport | None,
    ) -> AsyncIterator[str]:
        self._set_usage(None)
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "reasoning_effort": "none",
            "stream_options": {"include_usage": True},
        }
        try:
            async with httpx.AsyncClient(
                transport=transport,
                timeout=self.timeout_seconds,
            ) as client:
                async with client.stream(
                    "POST",
                    self._chat_completions_url,
                    headers=self._headers(),
                    json=payload,
                    timeout=self.timeout_seconds,
                ) as response:
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        if not line or not line.startswith("data:"):
                            continue
                        data = line.removeprefix("data:").strip()
                        if data == "[DONE]":
                            return
                        try:
                            frame = json.loads(data)
                            if frame.get("usage") is not None:
                                self._set_usage(frame["usage"])
                            choices = frame.get("choices")
                            if not choices:
                                continue
                            content = choices[0]["delta"].get("content")
                        except (KeyError, IndexError, TypeError, ValueError) as exc:
                            raise ProviderUnavailable(
                                "Provider returned an invalid stream"
                            ) from exc
                        if content:
                            yield str(content)
        except httpx.HTTPError as exc:
            raise ProviderUnavailable("Provider request failed") from exc

    def _new_call_transport(
        self,
    ) -> httpx.AsyncBaseTransport | None:
        return None

    def _new_knowledge_transport(
        self,
    ) -> httpx.AsyncBaseTransport | None:
        return None

    def _request(self, messages: list[dict[str, str]], stream: bool) -> httpx.Response:
        try:
            response = self._client.post(
                self._chat_completions_url,
                headers=self._headers(),
                json={
                    "model": self.model,
                    "messages": messages,
                    "stream": stream,
                    "reasoning_effort": "none",
                },
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            return response
        except httpx.HTTPError as exc:
            raise ProviderUnavailable("Provider request failed") from exc

    @property
    def _chat_completions_url(self) -> str:
        return f"{self.base_url}/chat/completions"

    def _headers(self) -> dict[str, str]:
        headers = {"content-type": "application/json"}
        if self._api_key:
            headers["authorization"] = f"Bearer {self._api_key}"
        return headers

    def _set_usage(self, value: Any) -> None:
        if not isinstance(value, dict):
            self._usage_state.value = ProviderUsage(
                input_tokens=None, output_tokens=None
            )
            return
        input_tokens = value.get("prompt_tokens")
        output_tokens = value.get("completion_tokens")
        self._usage_state.value = ProviderUsage(
            input_tokens=input_tokens if isinstance(input_tokens, int) else None,
            output_tokens=output_tokens if isinstance(output_tokens, int) else None,
        )

    @staticmethod
    def _validate_json(raw: str, adapter: TypeAdapter[Any]) -> Any:
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else ""
            if cleaned.endswith("```"):
                cleaned = cleaned[:-3].strip()
        try:
            return adapter.validate_json(cleaned)
        except (ValidationError, ValueError) as exc:
            raise ProviderUnavailable("Provider returned invalid structured JSON") from exc
