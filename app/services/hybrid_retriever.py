from __future__ import annotations

import logging
from dataclasses import dataclass

from app.core.config import Settings
from app.repositories.chunks import ChunkRepository, ChunkSearchHit
from app.services.embedding import EmbeddingService
from app.services.rrf import rrf_fuse
from app.services.ts_query_builder import TsQueryBuilder
from app.services.token_metrics import knowledge_base_scope

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class HybridRetrieveResult:
    """混合检索结果和内部召回统计。"""

    hits: list[ChunkSearchHit]
    vector_count: int
    fulltext_count: int


class HybridRetriever:
    """混合检索服务，融合向量检索和 PostgreSQL 全文检索结果。"""

    def __init__(
        self,
        *,
        embedding_service: EmbeddingService,
        chunk_repository: ChunkRepository,
        ts_query_builder: TsQueryBuilder,
        settings: Settings,
    ) -> None:
        """初始化混合检索依赖。

        Args:
            embedding_service: 查询问题向量化服务。
            chunk_repository: chunk 检索仓储。
            ts_query_builder: 全文检索查询文本构建器。
            settings: RAG 检索参数配置。
        """
        if settings.rag_rrf_k <= 0:
            raise ValueError("rag_rrf_k must be positive")
        self.embedding_service = embedding_service
        self.chunk_repository = chunk_repository
        self.ts_query_builder = ts_query_builder
        self.settings = settings

    async def retrieve(self, *, question: str, kb_ids: list[int]) -> HybridRetrieveResult:
        """执行向量召回、全文召回和 RRF 融合。

        Args:
            question: 用户问题，调用方应已做基础空白清理。
            kb_ids: 已通过读权限校验的知识库 ID 列表。

        Returns:
            RRF 融合后的候选命中，以及两路原始召回数量。
        """
        normalized_question = question.strip()

        query_vector = await self.embedding_service.embed_query(
            normalized_question,
            kb_id=knowledge_base_scope(kb_ids),
        )
        vector_hits = await self.chunk_repository.search_by_vector(
            query_vector=query_vector,
            kb_ids=kb_ids,
            top_k=self.settings.rag_vector_top_k,
        )

        query_text = self.ts_query_builder.build(normalized_question)
        fulltext_hits: list[ChunkSearchHit] = []
        if query_text:
            fulltext_hits = await self.chunk_repository.search_by_fulltext(
                query_text=query_text,
                kb_ids=kb_ids,
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
