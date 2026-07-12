from __future__ import annotations

import logging
import time
from typing import Any

from fastapi import HTTPException, status
from langchain_core.messages import HumanMessage, SystemMessage
from sqlalchemy.exc import SQLAlchemyError

from app.core.config import Settings
from app.core.context import CurrentUser
from app.schemas.rag import RagQueryResponse
from app.services.embedding import EmbeddingError
from app.services.hybrid_retriever import HybridRetrieveResult, HybridRetriever
from app.services.rag_query import RAG_REFUSAL_ANSWER, SYSTEM_PROMPT_TEMPLATE
from app.services.source_builder import SourceBuilder
from app.services.token_metrics import TokenMetrics, record_generation_usage

logger = logging.getLogger(__name__)


class RagQueryServiceV2:
    """混合检索 RAG 查询服务，编排 HybridRetriever、上下文构建和模型生成。"""

    def __init__(
        self,
        *,
        retriever: HybridRetriever,
        source_builder: SourceBuilder,
        chat_model: Any,
        token_metrics: TokenMetrics,
        settings: Settings,
    ) -> None:
        """初始化查询服务依赖。

        Args:
            retriever: 混合检索服务，负责向量、全文和 RRF 融合。
            source_builder: 上下文和引用来源构建器。
            chat_model: LangChain ChatOpenAI 兼容聊天模型。
            settings: 应用配置，提供上下文数量限制等参数。
        """
        self.retriever = retriever
        self.source_builder = source_builder
        self.chat_model = chat_model
        self.token_metrics = token_metrics
        self.settings = settings

    async def query(
        self,
        *,
        question: str,
        kb_ids: list[int],
        user: CurrentUser,
    ) -> RagQueryResponse:
        """执行混合检索 RAG 查询并返回回答、引用和耗时统计。

        Args:
            question: 已通过请求校验的用户问题。
            kb_ids: 已通过读权限校验的知识库 ID 列表。
            user: 当前认证用户上下文，用于日志审计。

        Returns:
            RAG 查询响应；无可用候选时返回固定拒答。
        """
        started_at = time.perf_counter()
        normalized_question = question.strip()
        logger.info("RAG v2 query started: user_id=%s kb_ids=%s", user.user_id, kb_ids)

        retrieve_result = await self._retrieve_hits(normalized_question, kb_ids, user, started_at)
        hits = retrieve_result.hits
        if not hits:
            logger.info(
                "RAG v2 query refused: reason=no_hits user_id=%s kb_ids=%s", user.user_id, kb_ids
            )
            return self._refusal_response(started_at)

        context, sources = self.source_builder.build(
            hits,
            return_top_n=self.settings.rag_return_top_n,
        )
        answer = await self._generate_answer(normalized_question, context, user, kb_ids)
        latency_ms = self._elapsed_ms(started_at)
        logger.info(
            "RAG v2 query completed: user_id=%s kb_ids=%s hit_count=%s latency_ms=%s",
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

    async def _retrieve_hits(
        self,
        question: str,
        kb_ids: list[int],
        user: CurrentUser,
        started_at: float,
    ) -> HybridRetrieveResult:
        """执行混合检索，统一处理向量和数据库检索异常。"""
        retrieval_started_at = time.perf_counter()
        try:
            result = await self.retriever.retrieve(question=question, kb_ids=kb_ids)
        except EmbeddingError as exc:
            logger.warning(
                "RAG v2 query embedding failed: user_id=%s kb_ids=%s elapsed_ms=%s error=%s",
                user.user_id,
                kb_ids,
                self._elapsed_ms(started_at),
                exc,
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="向量化服务暂时不可用"
            ) from exc
        except RuntimeError as exc:
            logger.warning(
                "RAG v2 query embedding dependency failed: user_id=%s kb_ids=%s elapsed_ms=%s error=%s",
                user.user_id,
                kb_ids,
                self._elapsed_ms(started_at),
                exc,
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="向量化服务暂时不可用"
            ) from exc
        except SQLAlchemyError as exc:
            logger.exception("RAG v2 retrieval failed: kb_ids=%s", kb_ids)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="检索服务暂时不可用"
            ) from exc

        logger.debug(
            "RAG v2 retrieval completed: hit_count=%s vector_count=%s fulltext_count=%s elapsed_ms=%s",
            len(result.hits),
            result.vector_count,
            result.fulltext_count,
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
                "RAG v2 generation failed: user_id=%s kb_ids=%s elapsed_ms=%s error=%s",
                user.user_id,
                kb_ids,
                self._elapsed_ms(generation_started_at),
                exc,
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="生成服务暂时不可用"
            ) from exc

        await record_generation_usage(
            recorder=self.token_metrics,
            response=response,
            pipeline="v2",
        )

        content = getattr(response, "content", None)
        if not isinstance(content, str) or not content.strip():
            logger.warning(
                "RAG v2 generation returned empty content: user_id=%s kb_ids=%s",
                user.user_id,
                kb_ids,
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="生成服务暂时不可用"
            )

        logger.debug(
            "RAG v2 generation completed: elapsed_ms=%s", self._elapsed_ms(generation_started_at)
        )
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
