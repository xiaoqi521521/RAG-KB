from __future__ import annotations

import logging
import time
from typing import Any

from fastapi import HTTPException, status
from langchain_core.messages import HumanMessage, SystemMessage
from sqlalchemy.exc import SQLAlchemyError

from app.core.config import Settings
from app.core.context import CurrentUser
from app.repositories.chunks import ChunkRepository, ChunkSearchHit
from app.schemas.rag import RagQueryResponse
from app.services.embedding import EmbeddingError, EmbeddingService
from app.services.source_builder import SourceBuilder
from app.services.token_metrics import TokenMetrics, record_generation_usage

logger = logging.getLogger(__name__)

RAG_REFUSAL_MARKER = "在知识库中未找到相关内容"
RAG_REFUSAL_ANSWER = "在知识库中未找到相关内容。请确认问题是否与所选知识库相关，或尝试换一种问法。"

SYSTEM_PROMPT_TEMPLATE = """你是企业内部知识库的智能助手。你的任务是只根据【参考内容】回答员工问题。

重要规则：
1. 只能根据【参考内容】回答，不要使用通用知识、经验或猜测补充公司制度。
2. 如果参考内容不足以回答，必须明确回答“在知识库中未找到相关内容”。
3. 回答使用中文，尽量准确、简洁。
4. 如果答案综合了多个参考片段，需要在关键句后标注参考编号，例如 [参考1]。
5. 禁止编造参考内容中不存在的流程、数字、政策、负责人或时间。

【参考内容】
---
{context}
---"""


class RagQueryService:
    """基础 RAG 查询服务，编排向量检索、上下文构建和模型生成。"""

    def __init__(
        self,
        *,
        embedding_service: EmbeddingService,
        chunk_repository: ChunkRepository,
        source_builder: SourceBuilder,
        chat_model: Any,
        token_metrics: TokenMetrics,
        settings: Settings,
    ) -> None:
        """初始化查询服务依赖。

        Args:
            embedding_service: 查询问题向量化服务。
            chunk_repository: chunk 向量检索仓储。
            source_builder: 上下文和引用来源构建器。
            chat_model: LangChain ChatOpenAI 兼容聊天模型。
            settings: 应用配置，提供 TopK 和上下文数量限制。
        """
        self.embedding_service = embedding_service
        self.chunk_repository = chunk_repository
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
        """执行基础 RAG 查询并返回回答、引用和耗时统计。

        Args:
            question: 已通过请求校验的用户问题。
            kb_ids: 已通过读权限校验的知识库 ID 列表。
            user: 当前认证用户上下文，用于日志审计。

        Returns:
            基础 RAG 查询响应；无召回时返回固定拒答。
        """
        started_at = time.perf_counter()
        normalized_question = question.strip()
        logger.info("RAG query started: kb_count=%s", len(kb_ids))

        query_vector = await self._embed_question(normalized_question, started_at)
        hits = await self._retrieve_hits(query_vector, kb_ids)

        if not hits:
            logger.info("RAG query refused: reason=no_hits kb_count=%s", len(kb_ids))
            return self._refusal_response(started_at)

        context, sources = self.source_builder.build(
            hits,
            return_top_n=self.settings.rag_return_top_n,
        )
        answer = await self._generate_answer(normalized_question, context)
        latency_ms = self._elapsed_ms(started_at)
        logger.info(
            "RAG query completed: kb_count=%s hit_count=%s latency_ms=%s",
            len(kb_ids),
            len(sources),
            latency_ms,
        )
        return RagQueryResponse(
            answer=answer,
            sources=sources,
            hit_count=len(sources),
            latency_ms=latency_ms,
        )

    async def _embed_question(
        self,
        question: str,
        started_at: float,
    ) -> list[float]:
        """向量化用户问题，返回可用于 PGVector 检索的向量。"""
        embedding_started_at = time.perf_counter()
        try:
            query_vector = await self.embedding_service.embed_query(question)
        except EmbeddingError as exc:
            logger.warning(
                "RAG query embedding failed: elapsed_ms=%s error_type=%s",
                self._elapsed_ms(started_at),
                type(exc).__name__,
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="向量化服务暂时不可用"
            ) from exc
        except RuntimeError as exc:
            logger.warning(
                "RAG query embedding dependency failed: elapsed_ms=%s error_type=%s",
                self._elapsed_ms(started_at),
                type(exc).__name__,
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="向量化服务暂时不可用"
            ) from exc

        logger.debug(
            "RAG embedding completed: elapsed_ms=%s", self._elapsed_ms(embedding_started_at)
        )
        return query_vector

    async def _retrieve_hits(
        self, query_vector: list[float], kb_ids: list[int]
    ) -> list[ChunkSearchHit]:
        """执行向量检索，返回已按分数排序的 chunk 命中列表。"""
        retrieval_started_at = time.perf_counter()
        try:
            hits = await self.chunk_repository.search_by_vector(
                query_vector=query_vector,
                kb_ids=kb_ids,
                top_k=self.settings.rag_vector_top_k,
            )
        except SQLAlchemyError as exc:
            logger.exception("RAG retrieval failed")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="检索服务暂时不可用"
            ) from exc

        logger.debug(
            "RAG retrieval completed: hit_count=%s elapsed_ms=%s",
            len(hits),
            self._elapsed_ms(retrieval_started_at),
        )
        return hits

    async def _generate_answer(
        self,
        question: str,
        context: str,
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
                "RAG generation failed: elapsed_ms=%s error_type=%s",
                self._elapsed_ms(generation_started_at),
                type(exc).__name__,
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="生成服务暂时不可用"
            ) from exc

        await record_generation_usage(
            recorder=self.token_metrics,
            response=response,
            pipeline="v1",
        )

        content = getattr(response, "content", None)
        if not isinstance(content, str) or not content.strip():
            logger.warning("RAG generation returned empty content")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="生成服务暂时不可用"
            )

        logger.debug(
            "RAG generation completed: elapsed_ms=%s", self._elapsed_ms(generation_started_at)
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
