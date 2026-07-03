from __future__ import annotations

import asyncio
from io import BytesIO

from langchain_core.documents import Document

from app.integrations.minio import MinioStorageService
from app.models import DocChunk, IndexTaskType, KbDocument
from app.repositories.chunks import ChunkRepository
from app.repositories.documents import DocumentRepository
from app.repositories.index_tasks import IndexTaskRepository
from app.services.chunking import ChunkService
from app.services.document_loader.service import DocumentLoaderService
from app.services.embedding import EmbeddingService


class IndexPipelineError(Exception):
    """索引管道基础异常，供上层按是否可重试做状态流转。"""


class RetryableIndexError(IndexPipelineError):
    """临时失败，适合落库后按任务级调度重试。"""


class NonRetryableIndexError(IndexPipelineError):
    """不可恢复失败，保持 FAILED 等待人工处理或后续手动重试。"""


class IndexService:
    """服务层索引管道，串联下载、解析、分块、向量化和 chunk 入库。

    本类只负责编排离线索引流程，不包含 API 路由、完整 MinIO 实现或 Embedding
    细节实现；这些能力由外部服务注入，便于后续分阶段替换。
    """

    def __init__(
        self,
        *,
        document_repository: DocumentRepository,
        task_repository: IndexTaskRepository,
        chunk_repository: ChunkRepository,
        storage_service: MinioStorageService,
        loader_service: DocumentLoaderService,
        chunk_service: ChunkService,
        embedding_service: EmbeddingService,
    ) -> None:
        """注入索引管道依赖，保持服务层只负责编排流程。

        Args:
            document_repository: 文档状态与版本信息仓储。
            task_repository: 索引任务状态仓储。
            chunk_repository: 文档 chunk 与向量写入仓储。
            storage_service: 原始文件下载服务，本阶段只依赖 download 签名。
            loader_service: 文档解析服务。
            chunk_service: 文档分块服务。
            embedding_service: 批量向量化服务。
        """
        self.document_repository = document_repository
        self.task_repository = task_repository
        self.chunk_repository = chunk_repository
        self.storage_service = storage_service
        self.loader_service = loader_service
        self.chunk_service = chunk_service
        self.embedding_service = embedding_service
        self._background_tasks: set[asyncio.Task[None]] = set()

    async def submit_index_task(self, doc_id: int) -> int:
        """提交首次索引任务并后台执行。

        Args:
            doc_id: 待索引文档 ID。

        Returns:
            新创建的索引任务 ID。
        """
        document = await self.document_repository.get(doc_id)
        if document is None:
            # 文档不存在属于确定性数据问题，重试不会让记录重新出现。
            raise NonRetryableIndexError(f"document not found: {doc_id}")

        task = await self.task_repository.create(doc_id, task_type=IndexTaskType.INDEX)
        self._launch_task(task.id, doc_id)
        return task.id

    async def reindex_document(self, doc_id: int) -> int:
        """提交重建索引任务并后台执行。

        Args:
            doc_id: 待重建索引的文档 ID。

        Returns:
            新创建的重建索引任务 ID。
        """
        document = await self.document_repository.get(doc_id)
        if document is None:
            # 重建索引依赖已有文档元数据；记录缺失时直接终止。
            raise NonRetryableIndexError(f"document not found: {doc_id}")

        task = await self.task_repository.create(doc_id, task_type=IndexTaskType.REINDEX)
        self._launch_task(task.id, doc_id)
        return task.id

    def _launch_task(self, task_id: int, doc_id: int) -> None:
        """创建后台任务并维护任务引用，避免任务被提前回收。

        Args:
            task_id: 已落库的索引任务 ID。
            doc_id: 任务关联的文档 ID。
        """
        task = asyncio.create_task(self.run_task(task_id, doc_id))
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def run_task(self, task_id: int, doc_id: int) -> None:
        """执行索引任务并根据异常类型更新失败或重试状态。

        Args:
            task_id: 索引任务 ID。
            doc_id: 任务关联的文档 ID。
        """
        try:
            await self._run_task_once(task_id, doc_id)
        except RetryableIndexError as exc:
            # 可重试异常先落失败原因，再把任务改回 PENDING 并调度下一次执行。
            await self._mark_failed(task_id, doc_id, str(exc))
            await self._retry_if_possible(task_id, doc_id, str(exc))
        except NonRetryableIndexError as exc:
            # 不可重试异常只标记失败，避免无意义地反复占用索引执行资源。
            await self._mark_failed(task_id, doc_id, str(exc))
        except Exception as exc:  # noqa: BLE001
            # 未预期异常按临时故障处理，保留自动重试机会，最终仍受 max_retries 约束。
            await self._mark_failed(task_id, doc_id, str(exc))
            await self._retry_if_possible(task_id, doc_id, str(exc))

    async def _run_task_once(self, task_id: int, doc_id: int) -> None:
        """执行一次完整索引链路，不在本方法内处理重试。

        Args:
            task_id: 索引任务 ID。
            doc_id: 任务关联的文档 ID。
        """
        # 第一步：校验任务和文档元数据。两者缺失都是确定性失败，不进入自动重试。
        task = await self.task_repository.get(task_id)
        if task is None:
            raise NonRetryableIndexError(f"index task not found: {task_id}")

        document = await self.document_repository.get(doc_id)
        if document is None:
            raise NonRetryableIndexError(f"document not found: {doc_id}")

        await self.task_repository.mark_running(task_id)
        await self.document_repository.mark_processing(doc_id)

        # 第二步：下载持久化源文件。MinIO/网络/IO 异常通常是瞬时问题，允许任务级重试。
        try:
            raw_file = await self.storage_service.download(document.minio_path)
        except IndexPipelineError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise RetryableIndexError(f"storage download failed: {exc}") from exc

        # 第三步：解析并分块。空分块可能来自解析抖动或文件内容异常，交给重试上限兜底。
        parsed_docs = self.loader_service.load(BytesIO(raw_file), document.file_name)
        chunks = self.chunk_service.split_documents(parsed_docs)
        if not chunks:
            raise RetryableIndexError("no valid chunks generated from document")

        # 第四步：批量向量化。Embedding 服务异常和数量不一致都会破坏 chunk/vector 对齐。
        texts = [chunk.page_content for chunk in chunks]
        try:
            vectors = await self.embedding_service.embed_documents(texts)
        except IndexPipelineError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise RetryableIndexError(f"embedding failed: {exc}") from exc

        if len(vectors) != len(chunks):
            raise RetryableIndexError(
                f"embedding result count mismatch: expected {len(chunks)}, got {len(vectors)}"
            )

        # 第五步：构建新版本 chunk。先写新版本、再删旧版本，避免失败时查询结果被清空。
        new_version = document.version + 1
        doc_chunks = self._build_doc_chunks(document, chunks, vectors, new_version)
        token_count = sum(chunk.token_count for chunk in doc_chunks)

        await self.chunk_repository.insert_many(doc_chunks)
        await self.document_repository.mark_done(
            doc_id,
            chunk_count=len(doc_chunks),
            token_count=token_count,
            version=new_version,
        )
        # 新版本已完整写入后再清理旧版本；若清理失败，旧数据残留也不会影响最新版本查询。
        await self.chunk_repository.delete_older_versions(doc_id, new_version)
        await self.task_repository.mark_done(task_id)

    def _build_doc_chunks(
        self,
        document: KbDocument,
        chunks: list[Document],
        vectors: list[list[float]],
        doc_version: int,
    ) -> list[DocChunk]:
        """组装可入库的 chunk 记录，保留来源元数据和版本号。

        Args:
            document: 当前索引文档记录。
            chunks: 分块服务产出的 LangChain Document 列表。
            vectors: 与 chunks 顺序一一对应的向量列表。
            doc_version: 本次索引写入的新版本号。

        Returns:
            可批量写入数据库的 DocChunk 列表。
        """
        doc_chunks: list[DocChunk] = []
        for index, (chunk, vector) in enumerate(zip(chunks, vectors, strict=True)):
            metadata = chunk.metadata
            page_num = metadata.get("page_num")
            estimated_tokens = metadata.get("estimated_tokens")
            section_title = metadata.get("section_title")

            # metadata 来自不同 loader，类型可能是 str/int/None；入库前统一收敛类型。
            doc_chunks.append(
                DocChunk(
                    doc_id=document.id,
                    kb_id=document.kb_id,
                    chunk_index=index,
                    content=chunk.page_content,
                    embedding=vector,
                    page_num=int(page_num) if page_num is not None else None,
                    section_title=str(section_title) if section_title is not None else None,
                    token_count=int(estimated_tokens) if estimated_tokens is not None else 0,
                    doc_version=doc_version,
                )
            )
        return doc_chunks

    async def _mark_failed(self, task_id: int, doc_id: int, error_msg: str) -> None:
        """同步标记任务失败，并在文档存在时标记文档失败。

        Args:
            task_id: 需要标记失败的任务 ID。
            doc_id: 任务关联的文档 ID。
            error_msg: 失败原因，写入任务和文档状态便于排查。
        """
        await self.task_repository.mark_failed(task_id, error_msg)
        if await self.document_repository.get(doc_id) is not None:
            await self.document_repository.mark_failed(doc_id, error_msg)

    async def _retry_if_possible(self, task_id: int, doc_id: int, error_msg: str) -> None:
        """在未超过最大重试次数时记录重试并调度下一次执行。

        Args:
            task_id: 待重试任务 ID。
            doc_id: 任务关联的文档 ID。
            error_msg: 本次失败原因，保留到任务记录中。
        """
        task = await self.task_repository.get(task_id)
        if task is None or not task.can_retry():
            # can_retry 统一封装 max_retries 判断，超过上限后保持 FAILED 等人工介入。
            return

        retry_count = task.retry_count + 1
        await self.task_repository.mark_retry_pending(
            task_id,
            retry_count=retry_count,
            error_msg=error_msg,
        )
        self._schedule_retry(task_id, doc_id, retry_count)

    def _schedule_retry(self, task_id: int, doc_id: int, retry_count: int) -> None:
        """按指数退避调度下一次重试。

        Args:
            task_id: 待重投递任务 ID。
            doc_id: 任务关联的文档 ID。
            retry_count: 本次已累计重试次数，用于计算 1s、2s、4s... 延迟。
        """
        delay_seconds = 2 ** (retry_count - 1)
        # 用独立后台协程等待，不在当前索引执行协程里 sleep，避免阻塞本次任务收尾。
        task = asyncio.create_task(self._delayed_retry(task_id, doc_id, delay_seconds))
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _delayed_retry(self, task_id: int, doc_id: int, delay_seconds: int) -> None:
        """等待指定时间后重新启动索引任务。

        Args:
            task_id: 待重投递任务 ID。
            doc_id: 任务关联的文档 ID。
            delay_seconds: 指数退避计算出的等待秒数。
        """
        await asyncio.sleep(delay_seconds)
        self._launch_task(task_id, doc_id)
