from collections.abc import Iterator
from time import sleep

from app.providers.base import ProviderUsage
from app.schemas import (
    CallSummary,
    ContextChunk,
    KnowledgeAnswer,
    KnowledgeCitation,
    ProviderHealth,
    ReviewFindingInput,
    ReviewRuleInput,
)


class MockProvider:
    """A deterministic, entirely local provider for the demonstration mode."""

    def __init__(self, delay_ms: int = 20):
        self.delay_ms = max(delay_ms, 0)

    @property
    def last_usage(self) -> ProviderUsage:
        return ProviderUsage(input_tokens=None, output_tokens=None)

    def reset_usage(self) -> None:
        return None

    def health_check(self) -> ProviderHealth:
        return ProviderHealth(available=True, detail="演示模型可用")

    def stream_call_suggestion(self, call_context: dict, templates: list[dict]) -> Iterator[str]:
        intent = str(call_context.get("intent", ""))
        if "故障" in intent or "报修" in intent:
            response = "我已记录设备故障情况，马上为您创建报修工单，并安排工程师跟进。"
        elif "投诉" in intent:
            response = "很抱歉给您带来不便，我会登记问题并优先协调处理进度。"
        elif "咨询" in intent:
            response = "我来为您核对相关流程，并向您说明下一步办理方式。"
        else:
            response = "我已记录您的诉求，会根据服务流程为您继续处理。"
        if templates:
            response += " 当前建议已结合可用服务话术。"
        yield from self._stream_text(response)

    def generate_call_summary(self, call_context: dict) -> CallSummary:
        transcript = str(call_context.get("original_transcript", ""))
        accepted_reply = str(call_context.get("accepted_reply", ""))
        appeal = "客户反馈设备故障" if "故障" in transcript else "客户咨询服务事项"
        return CallSummary(
            appeal=appeal,
            process="客服已核对信息、完成登记并采纳编辑后回复"
            if accepted_reply
            else "客服已核对信息并完成登记",
            result="请求已受理，等待后续处理",
            follow_up="在承诺时限内向客户反馈进展",
        )

    def answer_with_context(self, question: str, chunks: list[ContextChunk]) -> KnowledgeAnswer:
        if not chunks:
            return KnowledgeAnswer(answer="未找到可用于回答该问题的知识依据。", citations=[])
        citations = [
            KnowledgeCitation(chunk_id=chunk.chunk_id, source_id=chunk.source_id, location=chunk.location)
            for chunk in chunks[:2]
        ]
        return KnowledgeAnswer(answer=f"根据已检索到的资料：{chunks[0].text}", citations=citations)

    def stream_answer_with_context(
        self, question: str, chunks: list[ContextChunk]
    ) -> Iterator[str]:
        if not chunks:
            return
        yield from self._stream_text(
            f"根据已检索到的资料：{chunks[0].text}"
        )

    def review_section(
        self, document_type: str, section: str, rules: list[ReviewRuleInput]
    ) -> list[ReviewFindingInput]:
        findings: list[ReviewFindingInput] = []
        normalized = section.replace(" ", "")
        for rule in rules:
            keyword = rule.check_item.replace(" ", "")
            if keyword and keyword not in normalized:
                findings.append(
                    ReviewFindingInput(
                        rule_id=rule.id,
                        risk=rule.default_risk,
                        description=f"未发现“{rule.check_item}”相关说明。",
                        location="当前章节",
                        excerpt=section[:200],
                        recommendation=f"请补充满足以下要求的内容：{rule.criterion}",
                    )
                )
        return findings

    def _stream_text(self, text: str) -> Iterator[str]:
        for index in range(0, len(text), 12):
            if self.delay_ms:
                sleep(self.delay_ms / 1000)
            yield text[index : index + 12]
