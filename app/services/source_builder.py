from __future__ import annotations

from app.repositories.chunks import ChunkSearchHit
from app.schemas.rag import SourceCitation


class SourceBuilder:
    """构建 RAG 生成阶段使用的参考上下文和引用来源。"""

    def __init__(self, *, max_context_chars: int) -> None:
        """初始化引用构建器。

        Args:
            max_context_chars: 允许进入 Prompt 的最大近似字符数。
        """
        self.max_context_chars = max(0, max_context_chars)

    def build(
        self,
        hits: list[ChunkSearchHit],
        *,
        return_top_n: int,
    ) -> tuple[str, list[SourceCitation]]:
        """将检索命中转换为上下文文本和引用来源。

        Args:
            hits: 按相似度排序后的 chunk 命中结果。
            return_top_n: 最多允许进入 Prompt 的 chunk 数量。

        Returns:
            二元组，第一个元素为 Prompt 上下文文本，第二个元素为与参考编号一致的来源列表。
        """
        if return_top_n <= 0 or self.max_context_chars <= 0:
            return "", []

        context_parts: list[str] = []
        sources: list[SourceCitation] = []
        used_chars = 0

        for reference_index, hit in enumerate(hits[:return_top_n], start=1):
            prefix = self._format_prefix(reference_index, hit)
            suffix = "\n"
            remaining = self.max_context_chars - used_chars
            if remaining <= 0:
                break

            block = f"{prefix}{hit.content}{suffix}"
            if len(block) > remaining:
                content_budget = remaining - len(prefix) - len(suffix)
                if content_budget <= 0:
                    break
                # 基础阶段只做近似字符预算；精确 token 裁剪留给后续上下文裁剪阶段。
                block = f"{prefix}{hit.content[:content_budget]}{suffix}"

            context_parts.append(block)
            sources.append(self._to_source(hit))
            used_chars += len(block)

            if used_chars >= self.max_context_chars:
                break

        return "\n".join(context_parts).strip(), sources

    def _format_prefix(self, reference_index: int, hit: ChunkSearchHit) -> str:
        """格式化单个参考片段的元数据前缀，返回可拼接 chunk 内容的文本。"""
        page_number = hit.page_num if hit.page_num is not None else "不适用"
        section_title = hit.section_title or "不适用"
        return (
            f"[参考{reference_index}]\n"
            f"文档：{hit.document_name}\n"
            f"知识库ID：{hit.kb_id}\n"
            f"ChunkID：{hit.chunk_id}\n"
            f"页码：{page_number}\n"
            f"章节：{section_title}\n"
            "内容：\n"
        )

    def _to_source(self, hit: ChunkSearchHit) -> SourceCitation:
        """将 chunk 命中转换为 API 响应中的引用来源 DTO。"""
        return SourceCitation(
            document_id=hit.doc_id,
            document_name=hit.document_name,
            kb_id=hit.kb_id,
            chunk_id=hit.chunk_id,
            chunk_index=hit.chunk_index,
            page_number=hit.page_num,
            section_title=hit.section_title,
            score=hit.score,
        )
