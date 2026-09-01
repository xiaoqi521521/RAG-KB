from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from typing import Any

import redis.asyncio as redis
from langchain_core.messages import HumanMessage, SystemMessage

from app.services.token_metrics import TokenUsageRecorder, record_chat_usage

logger = logging.getLogger(__name__)

HYDE_MAX_CHARS = 800
MULTI_QUERY_COUNT = 3
MULTI_QUERY_MAX_CHARS = 200
REWRITE_CACHE_VERSION = "v1"
HYDE_CACHE_PREFIX = "rag:query:user-question-hyde:"

HYDE_PROMPT_TEMPLATE = """请根据以下问题生成一个简洁的假设性回答，用于企业知识库检索。
要求：
1. 只写 2-4 句中文。
2. 保持原始问题意图，不扩展到无关主题。
3. 不要写“根据资料”“可能”“以下是”等前缀。
4. 这不是最终答案，只需要覆盖可能出现在文档中的表达。

问题：{question}"""

MULTI_QUERY_PROMPT_TEMPLATE = """请将以下问题改写成 3 个不同表达方式的企业知识库查询。
要求：
1. 保持原始意图不变。
2. 每个查询从不同角度表达，例如流程、条件、入口、限制或正式术语。
3. 每行一个查询，不要编号，不要解释。
4. 不要扩展到无关制度或更大主题。

原始问题：{question}"""


@dataclass(frozen=True)
class HydeRewriteResult:
    """HyDE 假设性回答生成结果。"""

    original_question: str
    hyde_answer: str | None
    used_cache: bool
    degraded_reasons: tuple[str, ...]
    response: Any | None = None


@dataclass(frozen=True)
class MultiQueryRewriteResult:
    """多路查询扩展结果；本阶段只生成，不接入检索链路。"""

    original_question: str
    expanded_queries: list[str]
    used_cache: bool
    degraded_reasons: tuple[str, ...]


class QueryRewriter:
    """查询改写服务，封装 HyDE 与多路扩展生成、缓存和降级。"""

    def __init__(
        self,
        *,
        chat_model: Any,
        redis_client: redis.Redis,
        chat_model_name: str,
        cache_ttl_seconds: int,
        token_metrics: TokenUsageRecorder | None = None,
    ) -> None:
        """初始化查询改写服务依赖。

        Args:
            chat_model: LangChain ChatOpenAI 兼容聊天模型。
            redis_client: Redis 客户端，作为改写结果加速缓存。
            chat_model_name: 当前聊天模型名，用于隔离缓存。
            cache_ttl_seconds: 改写结果缓存过期时间。
        """
        self.chat_model = chat_model
        self.redis = redis_client
        self.chat_model_name = chat_model_name
        self.cache_ttl_seconds = cache_ttl_seconds
        self.token_metrics = token_metrics

    async def generate_hyde_answer(
        self,
        question: str,
        *,
        kb_id: str | int = "unknown",
    ) -> HydeRewriteResult:
        """生成 HyDE 假设性回答，失败时降级为空结果。

        Args:
            question: 用户原始问题。

        Returns:
            HyDE 改写结果；`hyde_answer=None` 表示该路不可用。
        """
        normalized_question = question.strip()
        degraded_reasons: list[str] = []
        cache_key = self._cache_key("hyde", normalized_question)

        cached = await self._read_cache(cache_key, "hyde", degraded_reasons)
        if cached:
            return HydeRewriteResult(
                original_question=normalized_question,
                hyde_answer=self._clean_text(cached, max_chars=HYDE_MAX_CHARS),
                used_cache=True,
                degraded_reasons=tuple(degraded_reasons),
            )

        try:
            response = await self._invoke_chat(
                HYDE_PROMPT_TEMPLATE.format(question=normalized_question)
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("HyDE generation failed: error=%s", exc)
            degraded_reasons.append("hyde_generation_failed")
            return HydeRewriteResult(normalized_question, None, False, tuple(degraded_reasons))

        await self._record_hyde_usage(response=response, kb_id=kb_id)
        content = getattr(response, "content", None)
        hyde_answer = self._clean_text(content if isinstance(content, str) else "", max_chars=HYDE_MAX_CHARS)
        if not hyde_answer:
            degraded_reasons.append("hyde_empty")
            return HydeRewriteResult(normalized_question, None, False, tuple(degraded_reasons), response)

        await self._write_cache(cache_key, hyde_answer, "hyde", degraded_reasons)
        return HydeRewriteResult(normalized_question, hyde_answer, False, tuple(degraded_reasons), response)

    async def expand_queries(self, question: str) -> MultiQueryRewriteResult:
        """生成多路扩展问题；本阶段暂不接入实际检索链路。

        Args:
            question: 用户原始问题。

        Returns:
            多路扩展结果；失败时 `expanded_queries=[]`。
        """
        normalized_question = question.strip()
        degraded_reasons: list[str] = []
        cache_key = self._cache_key("multi", normalized_question)

        cached = await self._read_cache(cache_key, "multi", degraded_reasons)
        if cached:
            return MultiQueryRewriteResult(
                original_question=normalized_question,
                expanded_queries=self._parse_expanded_queries(cached, normalized_question),
                used_cache=True,
                degraded_reasons=tuple(degraded_reasons),
            )

        try:
            response = await self._invoke_chat(
                MULTI_QUERY_PROMPT_TEMPLATE.format(question=normalized_question)
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Multi-query generation failed: error=%s", exc)
            degraded_reasons.append("multi_generation_failed")
            return MultiQueryRewriteResult(normalized_question, [], False, tuple(degraded_reasons))

        content = getattr(response, "content", None)
        expanded_queries = self._parse_expanded_queries(
            content if isinstance(content, str) else "", normalized_question
        )
        if not expanded_queries:
            degraded_reasons.append("multi_empty")
            return MultiQueryRewriteResult(normalized_question, [], False, tuple(degraded_reasons))

        await self._write_cache(cache_key, json.dumps(expanded_queries, ensure_ascii=False), "multi", degraded_reasons)
        return MultiQueryRewriteResult(normalized_question, expanded_queries, False, tuple(degraded_reasons))

    def _cache_key(self, kind: str, question: str) -> str:
        """构造改写缓存 key，按模型和策略版本隔离。"""
        digest = hashlib.md5(question.strip().encode("utf-8")).hexdigest()
        if kind == "hyde":
            return f"{HYDE_CACHE_PREFIX}{digest}"
        if kind == "multi":
            return f"rag:rewrite:{REWRITE_CACHE_VERSION}:multi:{MULTI_QUERY_COUNT}:{self.chat_model_name}:{digest}"
        return f"rag:rewrite:{REWRITE_CACHE_VERSION}:{kind}:{self.chat_model_name}:{digest}"

    async def _invoke_chat(self, prompt: str) -> Any:
        """调用聊天模型并保留原始响应，供 usage 记录和文本清洗分别使用。"""
        messages = [
            SystemMessage(content="你是企业知识库查询改写助手，只输出改写结果。"),
            HumanMessage(content=prompt),
        ]
        return await self.chat_model.ainvoke(messages)

    async def _record_hyde_usage(self, *, response: Any, kb_id: str | int) -> None:
        """记录 HyDE 聊天调用的完整输入和独立输出。"""
        if self.token_metrics is None:
            return
        await record_chat_usage(
            recorder=self.token_metrics,
            response=response,
            model=self.chat_model_name,
            output_type="hyde",
            kb_id=kb_id,
        )

    async def _read_cache(self, key: str, kind: str, degraded_reasons: list[str]) -> str | None:
        """读取缓存；缓存是加速层，失败只记录降级原因。"""
        try:
            value = await self.redis.get(key)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Rewrite cache read failed: kind=%s error=%s", kind, exc)
            degraded_reasons.append(f"{kind}_cache_read_failed")
            return None

        if isinstance(value, bytes):
            return value.decode("utf-8")
        return value if isinstance(value, str) else None

    async def _write_cache(self, key: str, value: str, kind: str, degraded_reasons: list[str]) -> None:
        """写入缓存；失败不影响已生成的改写结果。"""
        try:
            await self.redis.setex(key, self.cache_ttl_seconds, value)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Rewrite cache write failed: kind=%s error=%s", kind, exc)
            degraded_reasons.append(f"{kind}_cache_write_failed")

    def _parse_expanded_queries(self, content: str, original_question: str) -> list[str]:
        """清洗多路扩展输出，去掉编号、空行和原始问题重复项。"""
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            lines = content.splitlines()
        else:
            lines = parsed if isinstance(parsed, list) else content.splitlines()

        queries: list[str] = []
        seen = {original_question.strip()}
        for line in lines:
            if not isinstance(line, str):
                continue
            query = self._clean_query_line(line)
            if not query or query in seen:
                continue
            seen.add(query)
            queries.append(query[:MULTI_QUERY_MAX_CHARS])
            if len(queries) >= MULTI_QUERY_COUNT:
                break
        return queries

    def _clean_query_line(self, line: str) -> str:
        """清理模型常见编号前缀，保留真正的查询文本。"""
        cleaned = line.strip()
        cleaned = re.sub(r"^\s*[-*]\s*", "", cleaned)
        cleaned = re.sub(r"^\s*\d+[\.、\)]\s*", "", cleaned)
        return cleaned.strip()

    def _clean_text(self, content: str, *, max_chars: int) -> str:
        """清理单段文本，避免空白和超长输出进入检索。"""
        cleaned = content.strip()
        return cleaned[:max_chars]
