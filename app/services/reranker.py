from __future__ import annotations

import logging
import time
import asyncio
from dataclasses import dataclass

import httpx

from app.integrations.dashscope import DashScopeRerankerClient, RerankApiResponse
from app.repositories.chunks import ChunkSearchHit

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RerankResult:
    """Reranker 服务层输出，包含候选、降级状态和统计信息。"""

    hits: list[ChunkSearchHit]
    degraded: bool
    degraded_reason: str | None
    input_count: int
    output_count: int
    elapsed_ms: int
    total_tokens: int | None


class RerankerService:
    """Reranker 编排服务，负责精排映射和失败降级。"""

    def __init__(
        self,
        *,
        client: DashScopeRerankerClient,
        top_n: int,
        max_retries: int = 0,
    ) -> None:
        """初始化精排服务依赖。"""
        if top_n <= 0:
            raise ValueError("top_n must be positive")
        if isinstance(max_retries, bool) or not 0 <= max_retries <= 2:
            raise ValueError("max_retries must be between 0 and 2")
        self.client = client
        self.top_n = top_n
        self.max_retries = max_retries

    async def rerank(
        self,
        *,
        question: str,
        candidates: list[ChunkSearchHit],
    ) -> RerankResult:
        """对候选 chunk 精排；失败时回退到输入候选的前 top_n 条。"""
        started_at = time.perf_counter()
        if not candidates:
            return self._result(
                hits=[],
                degraded=False,
                degraded_reason=None,
                input_count=0,
                started_at=started_at,
                total_tokens=None,
            )

        effective_top_n = min(self.top_n, len(candidates))
        if len(candidates) <= effective_top_n:
            logger.info("[Reranker] 候选数不超过 topN，跳过精排：候选=%s，topN=%s", len(candidates), self.top_n)
            return self._result(
                hits=candidates[:effective_top_n],
                degraded=False,
                degraded_reason="skipped_not_enough_candidates",
                input_count=len(candidates),
                started_at=started_at,
                total_tokens=None,
            )

        try:
            response = await self._invoke_with_retry(
                query=question.strip(),
                documents=[hit.content for hit in candidates],
                top_n=effective_top_n,
            )
            hits = self._map_results(response.results, candidates)
            if not hits:
                logger.warning("[Reranker] 精排失败或超时，降级使用 RRF 分数：%s", "reranker returned empty results")
                return self._degrade(candidates, started_at, "reranker_empty_result")
            logger.info("[Reranker] 精排完成：候选=%s，返回=%s", len(candidates), len(hits))
            return self._result(
                hits=hits,
                degraded=False,
                degraded_reason=None,
                input_count=len(candidates),
                started_at=started_at,
                total_tokens=response.total_tokens,
            )
        except httpx.TimeoutException as exc:
            logger.warning("[Reranker] 精排失败或超时，降级使用 RRF 分数：%s", exc)
            return self._degrade(candidates, started_at, "reranker_timeout")
        except httpx.HTTPStatusError as exc:
            logger.warning("[Reranker] 精排失败或超时，降级使用 RRF 分数：%s", exc)
            return self._degrade(candidates, started_at, "reranker_http_error")
        except _InvalidRerankIndex as exc:
            logger.warning("[Reranker] 精排失败或超时，降级使用 RRF 分数：%s", exc)
            return self._degrade(candidates, started_at, "reranker_invalid_index")
        except _InvalidRerankScore as exc:
            logger.warning("[Reranker] 精排失败或超时，降级使用 RRF 分数：%s", exc)
            return self._degrade(candidates, started_at, "reranker_invalid_score")
        except Exception as exc:  # noqa: BLE001
            logger.warning("[Reranker] 精排失败或超时，降级使用 RRF 分数：%s", exc)
            return self._degrade(candidates, started_at, "reranker_unexpected_error")

    def _map_results(
        self,
        results: object,
        candidates: list[ChunkSearchHit],
    ) -> list[ChunkSearchHit]:
        """将 provider index 映射回原始候选，并替换为精排分数。"""
        if not results:
            return []

        mapped: list[ChunkSearchHit] = []
        seen_indexes: set[int] = set()
        for result in results:
            index = getattr(result, "index")
            if index in seen_indexes:
                continue
            if index < 0 or index >= len(candidates):
                raise _InvalidRerankIndex(f"index out of range: {index}")
            seen_indexes.add(index)
            try:
                score = float(getattr(result, "relevance_score"))
            except (TypeError, ValueError) as exc:
                raise _InvalidRerankScore("relevance_score must be numeric") from exc
            original = candidates[index]
            mapped.append(
                ChunkSearchHit(
                    chunk_id=original.chunk_id,
                    doc_id=original.doc_id,
                    document_name=original.document_name,
                    kb_id=original.kb_id,
                    chunk_index=original.chunk_index,
                    content=original.content,
                    page_num=original.page_num,
                    section_title=original.section_title,
                    score=score,
                )
            )
        return sorted(mapped, key=lambda hit: hit.score, reverse=True)

    async def _invoke_with_retry(
        self,
        *,
        query: str,
        documents: list[str],
        top_n: int,
    ) -> RerankApiResponse:
        """对瞬时超时做有限重试；最终失败仍交由调用方降级。"""
        for attempt in range(self.max_retries + 1):
            try:
                return await self.client.rerank(
                    query=query,
                    documents=documents,
                    top_n=top_n,
                )
            except httpx.TimeoutException:
                if attempt >= self.max_retries:
                    raise
                logger.warning(
                    "[Reranker] 精排超时，准备重试：attempt=%s max_retries=%s",
                    attempt + 1,
                    self.max_retries,
                )
                await asyncio.sleep(0.2)

    def _degrade(
        self,
        candidates: list[ChunkSearchHit],
        started_at: float,
        reason: str,
    ) -> RerankResult:
        """构建 RRF 降级结果，保留原始候选顺序和分数。"""
        return self._result(
            hits=candidates[: min(self.top_n, len(candidates))],
            degraded=True,
            degraded_reason=reason,
            input_count=len(candidates),
            started_at=started_at,
            total_tokens=None,
        )

    def _result(
        self,
        *,
        hits: list[ChunkSearchHit],
        degraded: bool,
        degraded_reason: str | None,
        input_count: int,
        started_at: float,
        total_tokens: int | None,
    ) -> RerankResult:
        """统一生成结果并记录耗时。"""
        return RerankResult(
            hits=hits,
            degraded=degraded,
            degraded_reason=degraded_reason,
            input_count=input_count,
            output_count=len(hits),
            elapsed_ms=max(0, int((time.perf_counter() - started_at) * 1000)),
            total_tokens=total_tokens,
        )


class _InvalidRerankIndex(ValueError):
    """provider 返回的 index 无法映射回候选列表。"""


class _InvalidRerankScore(ValueError):
    """provider 返回的分数无法转为 float。"""
