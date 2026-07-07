from __future__ import annotations

import re


STOP_WORDS = {
    "的",
    "了",
    "是",
    "在",
    "有",
    "和",
    "与",
    "及",
    "或",
    "这",
    "那",
    "什么",
    "怎么",
    "如何",
    "为什么",
    "哪些",
    "怎样",
    "请问",
    "是什么",
    "a",
    "an",
    "the",
    "is",
    "are",
    "what",
    "how",
}

STOP_PHRASES = {
    "是什么",
    "为什么",
    "有哪些",
    "怎么",
    "如何",
    "怎样",
    "请问",
}

TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_.-]+|[\u4e00-\u9fff]+")


class TsQueryBuilder:
    """构建 PostgreSQL 全文检索查询文本。"""

    def build(self, question: str) -> str | None:
        """将用户问题转换为可传给 to_tsquery 的查询文本。

        Args:
            question: 用户原始问题。

        Returns:
            清洗后的全文检索文本；无有效关键词时返回 None。
        """
        tokens = TOKEN_PATTERN.findall(question.strip())
        keywords = []
        for token in tokens:
            keyword = self._clean_token(token)
            if keyword:
                keywords.append(keyword)
        if not keywords:
            return None
        return " & ".join(keywords)

    def _clean_token(self, token: str) -> str | None:
        """清理单个 token，剥离中文疑问短语并过滤停用词。"""
        keyword = token.strip()
        for phrase in STOP_PHRASES:
            keyword = keyword.replace(phrase, "")
        if not keyword or keyword.lower() in STOP_WORDS:
            return None
        return keyword
