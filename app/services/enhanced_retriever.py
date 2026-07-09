from __future__ import annotations

import logging
from dataclasses import dataclass

from app.repositories.chunks import ChunkRepository, ChunkSearchHit
from app.services.embedding import EmbeddingService
from app.services.hybrid_retriever import HybridRetriever
from app.services.query_rewriter import QueryRewriter
from app.services.rrf import rrf_fuse

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EnhancedRetrieveResult:
    """查询改写增强检索结果和内部召回统计。"""

    hits: list[ChunkSearchHit]
    original_count: int
    hyde_count: int
    merged_count: int
    degraded_reasons: tuple[str, ...]


class EnhancedRetriever:
    """参考文献式增强检索：原始问题混合检索 + HyDE 向量检索后二次 RRF。"""

    def __init__(
        self,
        *,
        query_rewriter: QueryRewriter,
        hybrid_retriever: HybridRetriever,
        embedding_service: EmbeddingService,
        chunk_repository: ChunkRepository,
        rrf_k: int,
        hyde_vector_top_k: int,
    ) -> None:
        """初始化增强检索依赖。"""
        if rrf_k <= 0:
            raise ValueError("rrf_k must be positive")
        self.query_rewriter = query_rewriter
        self.hybrid_retriever = hybrid_retriever
        self.embedding_service = embedding_service
        self.chunk_repository = chunk_repository
        self.rrf_k = rrf_k
        self.hyde_vector_top_k = hyde_vector_top_k

    async def retrieve(self, *, question: str, kb_ids: list[int]) -> EnhancedRetrieveResult:
        """执行原始问题混合检索、HyDE 向量检索和二次 RRF。

        Args:
            question: 用户原始问题，调用方应已通过基础校验。
            kb_ids: 已通过读权限校验的知识库 ID 列表。

        Returns:
            二次 RRF 融合后的候选命中和召回统计。
        """
        normalized_question = question.strip()

        # 第一步：原始问题仍走第 13 章混合检索，内部已完成向量 + 全文 RRF。
        original_result = await self.hybrid_retriever.retrieve(question=normalized_question, kb_ids=kb_ids)

        # 第二步：HyDE 只做向量检索；失败时不能影响原始混合检索结果。
        hyde_hits: list[ChunkSearchHit] = []
        degraded_reasons = list(await self._retrieve_hyde_hits(normalized_question, kb_ids, hyde_hits))

        fused_hits = rrf_fuse(
            {
                "original_hybrid": original_result.hits,
                "hyde_vector": hyde_hits,
            },
            rrf_k=self.rrf_k,
        )
        hits = [self._with_rrf_score(item.hit, item.score) for item in fused_hits]

        logger.debug(
            "Enhanced retrieval completed: original_count=%s hyde_count=%s merged_count=%s",
            len(original_result.hits),
            len(hyde_hits),
            len(hits),
        )
        return EnhancedRetrieveResult(
            hits=hits,
            original_count=len(original_result.hits),
            hyde_count=len(hyde_hits),
            merged_count=len(hits),
            degraded_reasons=tuple(degraded_reasons),
        )

    async def _retrieve_hyde_hits(
        self,
        question: str,
        kb_ids: list[int],
        hyde_hits: list[ChunkSearchHit],
    ) -> tuple[str, ...]:
        """生成 HyDE 并执行向量检索；异常统一降级为无 HyDE 结果。"""
        rewrite_result = await self.query_rewriter.generate_hyde_answer(question)
        degraded_reasons = list(rewrite_result.degraded_reasons)
        if not rewrite_result.hyde_answer:
            return tuple(degraded_reasons)

        try:
            hyde_vector = await self.embedding_service.embed_query(
                rewrite_result.hyde_answer,
                namespace="hyde",
                cache_enabled=False,
            )
            hyde_hits.extend(
                await self.chunk_repository.search_by_vector(
                    query_vector=hyde_vector,
                    kb_ids=kb_ids,
                    top_k=self.hyde_vector_top_k,
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("HyDE retrieval failed, fallback to original hybrid results: error=%s", exc)
            degraded_reasons.append("hyde_retrieval_failed")
        return tuple(degraded_reasons)

    def _with_rrf_score(self, hit: ChunkSearchHit, score: float) -> ChunkSearchHit:
        """复制命中元数据并将对外 score 替换为第二阶段 RRF 分数。"""
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
