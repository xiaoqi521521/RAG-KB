from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from fastapi import HTTPException, status

from app.core.config import Settings
from app.core.context import get_current_user_from_context
from app.repositories.chunks import ChunkRepository, ChunkSearchHit
from app.services.embedding import EmbeddingService
from app.services.permissions import PermissionService
from app.services.rrf import rrf_fuse
from app.services.ts_query_builder import TsQueryBuilder
from app.services.token_metrics import knowledge_base_scope

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class HybridRetrieveResult:
    """混合检索结果、授权范围和内部召回统计。"""

    hits: list[ChunkSearchHit]
    vector_count: int
    fulltext_count: int
    allowed_kb_ids: list[int]


class HybridRetriever:
    """混合检索服务，融合向量检索和 PostgreSQL 全文检索结果。"""

    def __init__(
        self,
        *,
        embedding_service: EmbeddingService,
        chunk_repository: ChunkRepository,
        ts_query_builder: TsQueryBuilder,
        settings: Settings,
        permission_service: PermissionService,
    ) -> None:
        """初始化混合检索依赖。

        Args:
            embedding_service: 查询问题向量化服务。
            chunk_repository: chunk 检索仓储。
            ts_query_builder: 全文检索查询文本构建器。
            settings: RAG 检索参数配置。
            permission_service: 知识库读权限服务。
        """
        if settings.rag_rrf_k <= 0:
            raise ValueError("rag_rrf_k must be positive")
        self.embedding_service = embedding_service
        self.chunk_repository = chunk_repository
        self.ts_query_builder = ts_query_builder
        self.settings = settings
        self.permission_service = permission_service

    async def retrieve(self, *, question: str, kb_ids: list[int]) -> HybridRetrieveResult:
        """读取当前用户并在授权范围内执行混合检索。

        Args:
            question: 用户问题，调用方应已做基础空白清理。
            kb_ids: 请求方指定的知识库 ID 列表。

        Returns:
            RRF 融合后的候选命中、实际授权范围，以及两路原始召回数量。
        """
        requested_kb_ids = list(dict.fromkeys(kb_ids))
        permission_started_at = time.perf_counter()
        try:
            user = get_current_user_from_context()
        except RuntimeError as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="当前请求未完成身份认证",
            ) from exc

        try:
            allowed_kb_ids = await self.permission_service.filter_readable_kb_ids(
                requested_kb_ids,
                user,
            )
        except HTTPException as exc:
            if exc.status_code == status.HTTP_503_SERVICE_UNAVAILABLE:
                logger.warning(
                    (
                        "retrieval_permission_filter operation=hybrid_retrieval "
                        "result=unavailable permission_source=permission_service user_id=%s "
                        "denied_kb_ids=[] requested_count=%s allowed_count=0 denied_count=0 "
                        "elapsed_ms=%s error_type=%s"
                    ),
                    user.user_id,
                    len(requested_kb_ids),
                    _elapsed_ms(permission_started_at),
                    type(exc).__name__,
                )
            raise
        allowed_kb_id_set = set(allowed_kb_ids)
        allowed_kb_ids = [kb_id for kb_id in requested_kb_ids if kb_id in allowed_kb_id_set]
        denied_kb_ids = [kb_id for kb_id in requested_kb_ids if kb_id not in allowed_kb_id_set]
        if denied_kb_ids:
            result = "denied" if not allowed_kb_ids else "filtered"
            logger.warning(
                (
                    "retrieval_permission_filter operation=hybrid_retrieval "
                    "result=%s permission_source=permission_service user_id=%s "
                    "denied_kb_ids=%s requested_count=%s allowed_count=%s "
                    "denied_count=%s elapsed_ms=%s error_type=none"
                ),
                result,
                user.user_id,
                denied_kb_ids,
                len(requested_kb_ids),
                len(allowed_kb_ids),
                len(denied_kb_ids),
                _elapsed_ms(permission_started_at),
            )

        if not allowed_kb_ids:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="您对所请求的知识库没有访问权限",
            )

        return await self._retrieve(question=question, allowed_kb_ids=allowed_kb_ids)

    async def _retrieve(
        self,
        *,
        question: str,
        allowed_kb_ids: list[int],
    ) -> HybridRetrieveResult:
        """仅在已过滤范围内执行向量召回、全文召回和 RRF 融合。"""
        normalized_question = question.strip()

        query_vector = await self.embedding_service.embed_query(
            normalized_question,
            kb_id=knowledge_base_scope(allowed_kb_ids),
        )
        vector_hits = await self.chunk_repository.search_by_vector(
            query_vector=query_vector,
            kb_ids=allowed_kb_ids,
            top_k=self.settings.rag_vector_top_k,
        )

        query_text = self.ts_query_builder.build(normalized_question)
        fulltext_hits: list[ChunkSearchHit] = []
        if query_text:
            fulltext_hits = await self.chunk_repository.search_by_fulltext(
                query_text=query_text,
                kb_ids=allowed_kb_ids,
                top_k=self.settings.rag_fulltext_top_k,
            )

        fused_hits = rrf_fuse(
            {
                "vector": vector_hits,
                "fulltext": fulltext_hits,
            },
            rrf_k=self.settings.rag_rrf_k,
        )
        hits = [self._with_rrf_score(item.hit, item.score) for item in fused_hits]

        logger.debug(
            "Hybrid retrieval completed: vector_count=%s fulltext_count=%s fused_count=%s",
            len(vector_hits),
            len(fulltext_hits),
            len(hits),
        )
        return HybridRetrieveResult(
            hits=hits,
            vector_count=len(vector_hits),
            fulltext_count=len(fulltext_hits),
            allowed_kb_ids=allowed_kb_ids,
        )

    def _with_rrf_score(self, hit: ChunkSearchHit, score: float) -> ChunkSearchHit:
        """复制命中元数据并将对外 score 替换为 RRF 分数。"""
        return ChunkSearchHit(
            chunk_id=hit.chunk_id,
            doc_id=hit.doc_id,
            document_name=hit.document_name,
            kb_id=hit.kb_id,
            chunk_index=hit.chunk_index,
            content=hit.content,
            page_num=hit.page_num,
            section_title=hit.section_title,
            score=score,
        )


def _elapsed_ms(started_at: float) -> int:
    """计算权限过滤阶段的耗时。"""
    return max(0, int((time.perf_counter() - started_at) * 1000))
