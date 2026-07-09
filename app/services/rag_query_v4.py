from __future__ import annotations

import logging
import time
from typing import Any

from fastapi import HTTPException, status
from langchain_core.messages import HumanMessage, SystemMessage
from sqlalchemy.exc import SQLAlchemyError

from app.core.config import Settings
from app.core.context import CurrentUser
from app.repositories.chunks import ChunkSearchHit
from app.schemas.rag import RagQueryResponse
from app.services.confidence_filter import ConfidenceFilter
from app.services.context_trimmer import ContextTrimmer
from app.services.embedding import EmbeddingError
from app.services.enhanced_retriever import EnhancedRetrieveResult, EnhancedRetriever
from app.services.rag_query import RAG_REFUSAL_ANSWER, SYSTEM_PROMPT_TEMPLATE
from app.services.reranker import RerankerService
from app.services.source_builder import SourceBuilder

logger = logging.getLogger(__name__)


class RagQueryServiceV4:
    """HyDE 增强 + Reranker 精排 RAG 查询服务。"""

    def __init__(
        self,
        *,
        retriever: EnhancedRetriever,
        reranker: RerankerService,
        confidence_filter: ConfidenceFilter,
        context_trimmer: ContextTrimmer,
        source_builder: SourceBuilder,
        chat_model: Any,
        settings: Settings,
    ) -> None:
        """初始化 v4 查询服务依赖。"""
        self.retriever = retriever
        self.reranker = reranker
        self.confidence_filter = confidence_filter
        self.context_trimmer = context_trimmer
        self.source_builder = source_builder
        self.chat_model = chat_model
        self.settings = settings

    async def query(
        self,
        *,
        question: str,
        kb_ids: list[int],
        user: CurrentUser,
    ) -> RagQueryResponse:
        """执行 v4 RAG 查询并返回回答、引用和耗时统计。"""
        started_at = time.perf_counter()
        normalized_question = question.strip()
        logger.info("RAG v4 query started: user_id=%s kb_ids=%s", user.user_id, kb_ids)

        retrieve_result = await self._retrieve_hits(normalized_question, kb_ids, user, started_at)
        if not retrieve_result.hits:
            logger.info("RAG v4 query refused: reason=no_hits user_id=%s kb_ids=%s", user.user_id, kb_ids)
            return self._refusal_response(started_at)

        hits = await self._prepare_context_hits(normalized_question, retrieve_result.hits)
        if not hits:
            logger.info("RAG v4 query refused: reason=no_context_hits user_id=%s kb_ids=%s", user.user_id, kb_ids)
            return self._refusal_response(started_at)

        context, sources = self.source_builder.build(
            hits,
            return_top_n=self.settings.rag_return_top_n,
        )
        if not context or not sources:
            logger.info("RAG v4 query refused: reason=empty_context user_id=%s kb_ids=%s", user.user_id, kb_ids)
            return self._refusal_response(started_at)

        answer = await self._generate_answer(normalized_question, context, user, kb_ids)
        latency_ms = self._elapsed_ms(started_at)
        logger.info(
            "RAG v4 query completed: user_id=%s kb_ids=%s hit_count=%s latency_ms=%s",
            user.user_id,
            kb_ids,
            len(sources),
            latency_ms,
        )
        return RagQueryResponse(
            answer=answer,
            sources=sources,
            hit_count=len(sources),
            latency_ms=latency_ms,
        )

    async def _prepare_context_hits(
        self,
        question: str,
        candidates: list[ChunkSearchHit],
    ) -> list[ChunkSearchHit]:
        """执行精排、成功态过滤和上下文裁剪占位。"""
        rerank_result = await self.reranker.rerank(question=question, candidates=candidates)
        hits = rerank_result.hits
        # 只有真实精排成功时才套用 Reranker 阈值；跳过精排时仍是 RRF 分数尺度。
        if not rerank_result.degraded and rerank_result.degraded_reason != "skipped_not_enough_candidates":
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
        return self.context_trimmer.trim(hits)

    async def _retrieve_hits(
        self,
        question: str,
        kb_ids: list[int],
        user: CurrentUser,
        started_at: float,
    ) -> EnhancedRetrieveResult:
        """执行增强检索，沿用 v3 的关键异常语义。"""
        retrieval_started_at = time.perf_counter()
        try:
            result = await self.retriever.retrieve(question=question, kb_ids=kb_ids)
        except EmbeddingError as exc:
            logger.warning(
                "RAG v4 query embedding failed: user_id=%s kb_ids=%s elapsed_ms=%s error=%s",
                user.user_id,
                kb_ids,
                self._elapsed_ms(started_at),
                exc,
            )
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="向量化服务暂时不可用") from exc
        except RuntimeError as exc:
            logger.warning(
                "RAG v4 query embedding dependency failed: user_id=%s kb_ids=%s elapsed_ms=%s error=%s",
                user.user_id,
                kb_ids,
                self._elapsed_ms(started_at),
                exc,
            )
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="向量化服务暂时不可用") from exc
        except SQLAlchemyError as exc:
            logger.exception("RAG v4 retrieval failed: kb_ids=%s", kb_ids)
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="检索服务暂时不可用") from exc

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
        context: str,
        user: CurrentUser,
        kb_ids: list[int],
    ) -> str:
        """调用聊天模型生成答案，并校验模型返回内容可用。"""
        generation_started_at = time.perf_counter()
        system_prompt = SYSTEM_PROMPT_TEMPLATE.format(context=context)
        messages = [
            SystemMessage(content=system_prompt),
            HumanMessage(content=question),
        ]
        try:
            response = await self.chat_model.ainvoke(messages)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "RAG v4 generation failed: user_id=%s kb_ids=%s elapsed_ms=%s error=%s",
                user.user_id,
                kb_ids,
                self._elapsed_ms(generation_started_at),
                exc,
            )
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="生成服务暂时不可用") from exc

        content = getattr(response, "content", None)
        if not isinstance(content, str) or not content.strip():
            logger.warning("RAG v4 generation returned empty content: user_id=%s kb_ids=%s", user.user_id, kb_ids)
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="生成服务暂时不可用")

        logger.debug("RAG v4 generation completed: elapsed_ms=%s", self._elapsed_ms(generation_started_at))
        return content.strip()

    def _refusal_response(self, started_at: float) -> RagQueryResponse:
        """构建固定拒答响应，返回空引用并记录真实耗时。"""
        return RagQueryResponse(
            answer=RAG_REFUSAL_ANSWER,
            sources=[],
            hit_count=0,
            latency_ms=self._elapsed_ms(started_at),
        )

    def _elapsed_ms(self, started_at: float) -> int:
        """根据起始时间计算非负毫秒耗时。"""
        return max(0, int((time.perf_counter() - started_at) * 1000))
