from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from fastapi import HTTPException, status
from langchain_core.messages import HumanMessage, SystemMessage
from sqlalchemy.exc import SQLAlchemyError

from app.core.config import Settings
from app.core.context import CurrentUser
from app.repositories.chunks import ChunkSearchHit
from app.schemas.rag import RagQueryResponse, SourceCitation
from app.services.confidence_filter import ConfidenceFilter
from app.services.context_trimmer import ContextTrimmer
from app.services.embedding import EmbeddingError
from app.services.enhanced_retriever import EnhancedRetrieveResult, EnhancedRetriever
from app.services.faithfulness_evaluator import FaithfulnessEvaluator, FaithfulnessStatus
from app.services.rag_prompt import build_v4_system_prompt
from app.services.rag_query import RAG_REFUSAL_ANSWER, RAG_REFUSAL_MARKER
from app.services.reranker import RerankerService
from app.services.source_builder import (
    CitationSelectionStatus,
    FinalizedAnswer,
    SourceBuilder,
)
from app.services.token_metrics import TokenMetrics, knowledge_base_scope, record_generation_usage

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PreparedRagContext:
    """V4 检索完成后可供回答模型使用的参考内容。"""

    context: str
    sources: list[SourceCitation]
    reranked_hits: list[ChunkSearchHit] = field(default_factory=list)
    reference_contexts: list[str] = field(default_factory=list)
    reranker_degraded: bool = False
    degraded_reason: str | None = None


@dataclass(frozen=True)
class PreparedRagHits:
    """精排结果及经过过滤、裁剪后可进入参考内容的候选。"""

    reranked_hits: list[ChunkSearchHit]
    context_hits: list[ChunkSearchHit]
    reranker_degraded: bool
    degraded_reason: str | None


@dataclass(frozen=True)
class RagExecution:
    """一次 V4 执行产生的公开响应和内部评估证据。"""

    public_response: RagQueryResponse
    reranked_hits: list[ChunkSearchHit]
    reference_contexts: list[str]
    prompt_context: str
    reranker_degraded: bool
    degraded_reason: str | None
    explicit_refusal: bool


class RagQueryServiceV4:
    """编排 HyDE、Reranker、Token 裁剪和精确引用的 V4 查询服务。"""

    def __init__(
        self,
        *,
        retriever: EnhancedRetriever,
        reranker: RerankerService,
        confidence_filter: ConfidenceFilter,
        context_trimmer: ContextTrimmer,
        source_builder: SourceBuilder,
        chat_model: Any,
        token_metrics: TokenMetrics,
        settings: Settings,
        faithfulness_evaluator: FaithfulnessEvaluator | None = None,
    ) -> None:
        """初始化 v4 查询服务依赖。"""
        self.retriever = retriever
        self.reranker = reranker
        self.confidence_filter = confidence_filter
        self.context_trimmer = context_trimmer
        self.source_builder = source_builder
        self.chat_model = chat_model
        self.token_metrics = token_metrics
        self.settings = settings
        self.faithfulness_evaluator = faithfulness_evaluator

    async def query(
        self,
        *,
        question: str,
        kb_ids: list[int],
        user: CurrentUser,
    ) -> RagQueryResponse:
        """执行 v4 RAG 查询并返回回答、引用和耗时统计。"""
        execution = await self.execute(question=question, kb_ids=kb_ids, user=user)
        if not execution.explicit_refusal:
            self._schedule_faithfulness_observation(
                question=question.strip(),
                answer=execution.public_response.answer,
                context=execution.prompt_context,
                kb_id=knowledge_base_scope(kb_ids),
            )
        return execution.public_response

    async def execute(
        self,
        *,
        question: str,
        kb_ids: list[int],
        user: CurrentUser,
    ) -> RagExecution:
        """执行一次 V4 RAG，并返回公开响应和内部评估证据。"""
        started_at = time.perf_counter()
        normalized_question = question.strip()
        logger.info("RAG v4 query started: kb_count=%s", len(kb_ids))

        prepared_context = await self._prepare_execution_context(
            question=normalized_question,
            kb_ids=kb_ids,
            user=user,
            started_at=started_at,
        )
        if not prepared_context.context or not prepared_context.sources:
            return self._refusal_execution(started_at, prepared_context)

        answer = await self._generate_answer(
            normalized_question,
            prepared_context,
            kb_ids=kb_ids,
        )
        finalized = self._resolve_answer_sources(
            answer=answer,
            prepared_context=prepared_context,
        )
        if finalized is None:
            return self._refusal_execution(started_at, prepared_context)
        answer = finalized.answer
        sources = finalized.sources

        latency_ms = self._elapsed_ms(started_at)
        logger.info(
            "RAG v4 query completed: kb_count=%s hit_count=%s latency_ms=%s",
            len(kb_ids),
            len(sources),
            latency_ms,
        )
        return RagExecution(
            public_response=RagQueryResponse(
                answer=answer,
                sources=sources,
                hit_count=len(sources),
                latency_ms=latency_ms,
            ),
            reranked_hits=prepared_context.reranked_hits,
            reference_contexts=prepared_context.reference_contexts,
            prompt_context=prepared_context.context,
            reranker_degraded=prepared_context.reranker_degraded,
            degraded_reason=prepared_context.degraded_reason,
            explicit_refusal=False,
        )

    def finalize_answer(
        self,
        *,
        question: str,
        answer: str,
        prepared_context: PreparedRagContext,
        user: CurrentUser,
        kb_ids: list[int],
    ) -> FinalizedAnswer | None:
        """解析回答引用并触发忠实性观测，拒答时返回 None。"""
        finalized = self._resolve_answer_sources(
            answer=answer,
            prepared_context=prepared_context,
        )
        if finalized is not None:
            self._schedule_faithfulness_observation(
                question=question,
                answer=finalized.answer,
                context=prepared_context.context,
                kb_id=knowledge_base_scope(kb_ids),
            )
        return finalized

    def _resolve_answer_sources(
        self,
        *,
        answer: str,
        prepared_context: PreparedRagContext,
    ) -> FinalizedAnswer | None:
        """按模型回答解析最终引用，明确拒答时返回 None。"""
        if self._is_explicit_refusal(answer):
            logger.info(
                (
                    "RAG v4 citations resolved: citation_status=%s "
                    "referenced_count=0 valid_count=0 invalid_count=0"
                ),
                CitationSelectionStatus.REFUSAL,
            )
            return None

        citation_result = self.source_builder.resolve_citations(answer, prepared_context.sources)
        sources = citation_result.sources
        logger.info(
            (
                "RAG v4 citations resolved: citation_status=%s "
                "referenced_count=%s valid_count=%s invalid_count=%s"
            ),
            citation_result.status,
            citation_result.referenced_count,
            citation_result.valid_count,
            citation_result.invalid_count,
        )
        return FinalizedAnswer(answer=citation_result.answer or answer, sources=sources)

    async def prepare_context(
        self,
        *,
        question: str,
        kb_ids: list[int],
        user: CurrentUser,
        started_at: float,
    ) -> PreparedRagContext | None:
        """准备本次回答的参考内容，供同步和流式链路共同使用。"""
        prepared_context = await self._prepare_execution_context(
            question=question,
            kb_ids=kb_ids,
            user=user,
            started_at=started_at,
        )
        if not prepared_context.context or not prepared_context.sources:
            return None
        return prepared_context

    async def _prepare_execution_context(
        self,
        *,
        question: str,
        kb_ids: list[int],
        user: CurrentUser,
        started_at: float,
    ) -> PreparedRagContext:
        """准备生成上下文，并保留拒答前已经形成的精排诊断。"""
        retrieve_result = await self._retrieve_hits(question, kb_ids, started_at)
        if not retrieve_result.hits:
            logger.info(
                "RAG v4 query refused: reason=no_hits kb_count=%s", len(kb_ids)
            )
            return PreparedRagContext(context="", sources=[])

        prepared_hits = await self._prepare_context_hits(question, retrieve_result.hits, kb_ids)
        if not prepared_hits.context_hits:
            logger.info(
                "RAG v4 query refused: reason=no_context_hits kb_count=%s",
                len(kb_ids),
            )
            return PreparedRagContext(
                context="",
                sources=[],
                reranked_hits=prepared_hits.reranked_hits,
                reranker_degraded=prepared_hits.reranker_degraded,
                degraded_reason=prepared_hits.degraded_reason,
            )

        built_context = self.source_builder.build_context(
            prepared_hits.context_hits,
            return_top_n=self.settings.rag_return_top_n,
        )
        if not built_context.context or not built_context.sources:
            logger.info(
                "RAG v4 query refused: reason=empty_context kb_count=%s",
                len(kb_ids),
            )
            return PreparedRagContext(
                context="",
                sources=[],
                reranked_hits=prepared_hits.reranked_hits,
                reranker_degraded=prepared_hits.reranker_degraded,
                degraded_reason=prepared_hits.degraded_reason,
            )

        return PreparedRagContext(
            context=built_context.context,
            sources=built_context.sources,
            reranked_hits=prepared_hits.reranked_hits,
            reference_contexts=built_context.reference_contexts,
            reranker_degraded=prepared_hits.reranker_degraded,
            degraded_reason=prepared_hits.degraded_reason,
        )

    async def _prepare_context_hits(
        self,
        question: str,
        candidates: list[ChunkSearchHit],
        kb_ids: list[int],
    ) -> PreparedRagHits:
        """执行精排、成功态过滤和上下文裁剪占位。"""
        rerank_result = await self.reranker.rerank(question=question, candidates=candidates)
        hits = rerank_result.hits
        # 只有真实精排成功时才套用 Reranker 阈值；跳过精排时仍是 RRF 分数尺度。
        if (
            not rerank_result.degraded
            and rerank_result.degraded_reason != "skipped_not_enough_candidates"
        ):
            hits = self.confidence_filter.filter(hits)

        logger.debug(
            (
                "RAG v4 reranker completed: input_count=%s output_count=%s elapsed_ms=%s "
                "degraded=%s reason=%s total_tokens=%s confidence_count=%s"
            ),
            rerank_result.input_count,
            rerank_result.output_count,
            rerank_result.elapsed_ms,
            rerank_result.degraded,
            rerank_result.degraded_reason,
            rerank_result.total_tokens,
            len(hits),
        )
        if not rerank_result.degraded:
            if rerank_result.total_tokens is not None:
                await self._record_reranker_usage(
                    tokens=rerank_result.total_tokens,
                    kb_ids=kb_ids,
                )
            elif rerank_result.degraded_reason != "skipped_not_enough_candidates":
                self._record_reranker_usage_unavailable(kb_ids=kb_ids)
        # 裁剪器只统计最终允许进入 SourceBuilder 的候选，确保 Context Token 与引用口径一致。
        context_candidates = hits[: self.settings.rag_return_top_n]
        context_hits = await self.context_trimmer.trim(context_candidates)
        return PreparedRagHits(
            reranked_hits=rerank_result.hits,
            context_hits=context_hits,
            reranker_degraded=rerank_result.degraded,
            degraded_reason=rerank_result.degraded_reason,
        )

    async def _record_reranker_usage(
        self,
        *,
        tokens: int,
        kb_ids: list[int],
    ) -> None:
        """记录成功 Reranker 调用的 provider usage，降级路径不生成估算。"""
        if not hasattr(self.token_metrics, "record_usage"):
            return
        await self.token_metrics.record_usage(  # type: ignore[attr-defined]
            tokens=tokens,
            model=getattr(self.settings, "reranker_model", "unknown"),
            token_type="reranker",
            kb_id=knowledge_base_scope(kb_ids),
        )

    def _record_reranker_usage_unavailable(self, *, kb_ids: list[int]) -> None:
        """成功调用但 provider 未返回 Reranker usage 时只写 unavailable 观测。"""
        if hasattr(self.token_metrics, "record_usage_unavailable"):
            self.token_metrics.record_usage_unavailable(  # type: ignore[attr-defined]
                model=getattr(self.settings, "reranker_model", "unknown"),
                token_type="reranker",
                kb_id=knowledge_base_scope(kb_ids),
            )

    async def _retrieve_hits(
        self,
        question: str,
        kb_ids: list[int],
        started_at: float,
    ) -> EnhancedRetrieveResult:
        """执行增强检索，沿用 v3 的关键异常语义。"""
        retrieval_started_at = time.perf_counter()
        try:
            result = await self.retriever.retrieve(question=question, kb_ids=kb_ids)
        except EmbeddingError as exc:
            logger.warning(
                "RAG v4 query embedding failed: elapsed_ms=%s error_type=%s",
                self._elapsed_ms(started_at),
                type(exc).__name__,
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="向量化服务暂时不可用"
            ) from exc
        except RuntimeError as exc:
            logger.warning(
                "RAG v4 query embedding dependency failed: elapsed_ms=%s error_type=%s",
                self._elapsed_ms(started_at),
                type(exc).__name__,
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="向量化服务暂时不可用"
            ) from exc
        except SQLAlchemyError as exc:
            logger.exception("RAG v4 retrieval failed")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="检索服务暂时不可用"
            ) from exc

        logger.debug(
            (
                "RAG v4 retrieval completed: hit_count=%s original_count=%s "
                "hyde_count=%s merged_count=%s elapsed_ms=%s"
            ),
            len(result.hits),
            result.original_count,
            result.hyde_count,
            result.merged_count,
            self._elapsed_ms(retrieval_started_at),
        )
        return result

    async def _generate_answer(
        self,
        question: str,
        prepared_context: PreparedRagContext,
        *,
        kb_ids: list[int],
    ) -> str:
        """调用聊天模型生成答案，并校验模型返回内容可用。"""
        generation_started_at = time.perf_counter()
        messages = self.build_generation_messages(question=question, prepared_context=prepared_context)
        try:
            response = await self.chat_model.ainvoke(messages)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "RAG v4 generation failed: elapsed_ms=%s error_type=%s",
                self._elapsed_ms(generation_started_at),
                type(exc).__name__,
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="生成服务暂时不可用"
            ) from exc

        await record_generation_usage(
            recorder=self.token_metrics,
            response=response,
            pipeline="v4",
            model=getattr(self.settings, "chat_model", "unknown"),
            kb_id=knowledge_base_scope(kb_ids),
        )

        content = getattr(response, "content", None)
        if not isinstance(content, str) or not content.strip():
            logger.warning("RAG v4 generation returned empty content")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="生成服务暂时不可用"
            )

        logger.debug(
            "RAG v4 generation completed: elapsed_ms=%s", self._elapsed_ms(generation_started_at)
        )
        return content.strip()

    def build_generation_messages(
        self,
        *,
        question: str,
        prepared_context: PreparedRagContext,
        history: list[object] | None = None,
    ) -> list[object]:
        """组装回答模型消息，流式链路可在系统消息后注入对话历史。"""
        system_prompt = build_v4_system_prompt(
            prepared_context.context,
            reference_count=len(prepared_context.sources),
        )
        return [
            SystemMessage(content=system_prompt),
            *(history or []),
            HumanMessage(content=question),
        ]

    def _schedule_faithfulness_observation(
        self,
        *,
        question: str,
        answer: str,
        context: str,
        kb_id: str | int,
    ) -> None:
        """在后台记录忠实性评估，避免质量观测增加用户响应延迟。"""
        if self.faithfulness_evaluator is None:
            return

        task = asyncio.create_task(
            self._observe_faithfulness(
                question=question,
                answer=answer,
                context=context,
                kb_id=kb_id,
            ),
            name="rag-faithfulness-evaluation",
        )
        task.add_done_callback(self._log_faithfulness_task_failure)

    async def _observe_faithfulness(
        self,
        *,
        question: str,
        answer: str,
        context: str,
        kb_id: str | int,
    ) -> None:
        """记录正常回答的忠实性观测，任何异常均不得影响查询响应。"""
        if self.faithfulness_evaluator is None:
            return

        try:
            result = await self.faithfulness_evaluator.evaluate(
                question=question,
                answer=answer,
                context=context,
                kb_id=kb_id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "RAG v4 faithfulness evaluation failed: error_type=%s",
                type(exc).__name__,
            )
            return

        log_level = logging.WARNING if result.status is FaithfulnessStatus.UNFAITHFUL else logging.INFO
        logger.log(
            log_level,
            (
                "RAG v4 faithfulness evaluated: status=%s score=%s "
                "elapsed_ms=%s sampled=%s"
            ),
            result.status.value,
            result.score,
            result.elapsed_ms,
            result.sampled,
        )

    def _log_faithfulness_task_failure(self, task: asyncio.Task[None]) -> None:
        """兜底记录后台任务未被内部隔离的异常，避免未检索异常告警。"""
        if task.cancelled():
            return
        exception = task.exception()
        if exception is not None:
            logger.warning(
                "RAG v4 faithfulness task failed: error_type=%s",
                type(exception).__name__,
            )

    def _is_explicit_refusal(self, answer: str) -> bool:
        """仅识别完整拒答，避免把局部信息缺失误判为整体拒答。"""
        normalized = answer.strip().rstrip("。.!！?？")
        for prefix in ("很抱歉", "抱歉"):
            if normalized.startswith(prefix):
                normalized = normalized[len(prefix) :].lstrip("，,。.!！?？ ")
                break
        return normalized in {
            RAG_REFUSAL_MARKER,
            RAG_REFUSAL_ANSWER.rstrip("。.!！?？"),
        }

    def _refusal_response(self, started_at: float) -> RagQueryResponse:
        """构建固定拒答响应，返回空引用并记录真实耗时。"""
        return RagQueryResponse(
            answer=RAG_REFUSAL_ANSWER,
            sources=[],
            hit_count=0,
            latency_ms=self._elapsed_ms(started_at),
        )

    def _refusal_execution(
        self,
        started_at: float,
        prepared_context: PreparedRagContext | None = None,
    ) -> RagExecution:
        """构建拒答执行结果，并保留已经形成的内部检索证据。"""
        return RagExecution(
            public_response=self._refusal_response(started_at),
            reranked_hits=prepared_context.reranked_hits if prepared_context else [],
            reference_contexts=prepared_context.reference_contexts if prepared_context else [],
            prompt_context=prepared_context.context if prepared_context else "",
            reranker_degraded=(prepared_context.reranker_degraded if prepared_context else False),
            degraded_reason=prepared_context.degraded_reason if prepared_context else None,
            explicit_refusal=True,
        )

    def _elapsed_ms(self, started_at: float) -> int:
        """根据起始时间计算非负毫秒耗时。"""
        return max(0, int((time.perf_counter() - started_at) * 1000))
