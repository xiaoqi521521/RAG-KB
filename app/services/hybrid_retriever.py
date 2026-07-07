from __future__ import annotations

import logging
from dataclasses import dataclass

from app.core.config import Settings
from app.repositories.chunks import ChunkRepository, ChunkSearchHit
from app.services.embedding import EmbeddingService
from app.services.ts_query_builder import TsQueryBuilder

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class HybridRetrieveResult:
    """混合检索结果和内部召回统计。"""

    hits: list[ChunkSearchHit]
    vector_count: int
    fulltext_count: int


@dataclass(frozen=True)
class _RrfSearchHit:
    """RRF 融合后的内部命中结果。"""

    hit: ChunkSearchHit
    score: float
    retrieval_sources: tuple[str, ...]


def _rrf_fuse(
    ranked_results: dict[str, list[ChunkSearchHit]],
    *,
    rrf_k: int,
) -> list[_RrfSearchHit]:
    """按 RRF 分数融合多路已排序检索结果。

    Args:
        ranked_results: key 为检索通道名，value 为该通道内已排序的命中列表。
        rrf_k: RRF 平滑参数，通常为 60。

    Returns:
        按 RRF 分数降序排列、按 chunk_id 去重后的命中列表。
    """
    if rrf_k <= 0:
        raise ValueError("rrf_k must be positive")

    hits_by_id: dict[int, ChunkSearchHit] = {}
    scores_by_id: dict[int, float] = {}
    sources_by_id: dict[int, list[str]] = {}
    first_seen_order: dict[int, int] = {}

    order = 0
    for source_name, hits in ranked_results.items():
        for rank, hit in enumerate(hits, start=1):
            chunk_id = hit.chunk_id
            if chunk_id not in first_seen_order:
                first_seen_order[chunk_id] = order
                order += 1
                sources_by_id[chunk_id] = []

            # 保留首次出现的 chunk 元数据，避免不同通道的原始分数互相污染。
            hits_by_id.setdefault(chunk_id, hit)
            scores_by_id[chunk_id] = scores_by_id.get(chunk_id, 0.0) + (1.0 / (rrf_k + rank))
            if source_name not in sources_by_id[chunk_id]:
                sources_by_id[chunk_id].append(source_name)

    return [
        _RrfSearchHit(
            hit=hits_by_id[chunk_id],
            score=scores_by_id[chunk_id],
            retrieval_sources=tuple(sources_by_id[chunk_id]),
        )
        for chunk_id in sorted(
            scores_by_id,
            key=lambda item: (-scores_by_id[item], first_seen_order[item]),
        )
    ]


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

        query_vector = await self.embedding_service.embed_query(normalized_question)
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

        fused_hits = _rrf_fuse(
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
