from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from io import BytesIO
from typing import Protocol

from langchain_core.documents import Document

from app.integrations.minio import MinioStorageService
from app.models import DocChunk, DocumentStatus, IndexTaskType, KbDocument
from app.repositories.chunks import ChunkRepository
from app.repositories.documents import DocumentRepository
from app.repositories.evaluations import EvaluationRepository
from app.repositories.index_tasks import IndexTaskRepository
from app.services.chunking import ChunkService
from app.services.document_loader.service import DocumentLoaderService
from app.services.embedding import EmbeddingService

logger = logging.getLogger(__name__)


class IndexTaskRunner(Protocol):
    """后台索引执行器协议，用于隔离请求会话和后台会话。"""

    async def run_task(self, task_id: int, doc_id: int) -> None:
        """执行指定索引任务。

        Args:
            task_id: 索引任务 ID。
            doc_id: 任务关联的文档 ID。

        Returns:
            无返回值，执行结果通过任务和文档状态落库。
        """


BackgroundServiceFactory = Callable[[], AbstractAsyncContextManager[IndexTaskRunner]]
CommitBeforeLaunch = Callable[[], Awaitable[None]]
CommitAfterStatusChange = Callable[[], Awaitable[None]]
RollbackBeforeFailureStatus = Callable[[], Awaitable[None]]


@dataclass(frozen=True)
class IndexSource:
    """本次索引任务实际读取和发布的原文件信息。"""

    file_name: str
    file_type: str
    file_size: int
    minio_path: str
    old_minio_path: str | None = None

    @property
    def is_replacement(self) -> bool:
        """是否为文档替换产生的新原文件。"""
        return self.old_minio_path is not None


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
        evaluation_repository: EvaluationRepository,
        storage_service: MinioStorageService,
        loader_service: DocumentLoaderService,
        chunk_service: ChunkService,
        embedding_service: EmbeddingService,
        background_service_factory: BackgroundServiceFactory | None = None,
        commit_before_launch: CommitBeforeLaunch | None = None,
        commit_after_status_change: CommitAfterStatusChange | None = None,
        rollback_before_failure_status: RollbackBeforeFailureStatus | None = None,
    ) -> None:
        """注入索引管道依赖，保持服务层只负责编排流程。

        Args:
            document_repository: 文档状态与版本信息仓储。
            task_repository: 索引任务状态仓储。
            chunk_repository: 文档 chunk 与向量写入仓储。
            evaluation_repository: 标准问题标注状态仓储。
            storage_service: 原始文件下载服务，本阶段只依赖 download 签名。
            loader_service: 文档解析服务。
            chunk_service: 文档分块服务。
            embedding_service: 批量向量化服务。
            background_service_factory: 后台任务使用的独立服务工厂，避免复用请求级数据库会话。
            commit_before_launch: 后台任务启动前的提交钩子，确保新任务对独立会话可见。
            commit_after_status_change: 进入执行态后的提交钩子，确保外部轮询可见处理中状态。
            rollback_before_failure_status: 失败状态落库前回滚当前发布事务的钩子。
        """
        self.document_repository = document_repository
        self.task_repository = task_repository
        self.chunk_repository = chunk_repository
        self.evaluation_repository = evaluation_repository
        self.storage_service = storage_service
        self.loader_service = loader_service
        self.chunk_service = chunk_service
        self.embedding_service = embedding_service
        self.background_service_factory = background_service_factory
        self.commit_before_launch = commit_before_launch
        self.commit_after_status_change = commit_after_status_change
        self.rollback_before_failure_status = rollback_before_failure_status
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
        await self._commit_before_launch()
        self._launch_task(task.id, doc_id)
        return task.id

    async def reindex_document(
        self,
        doc_id: int,
        *,
        payload: dict[str, object] | None = None,
    ) -> int:
        """提交重建索引任务并后台执行。

        Args:
            doc_id: 待重建索引的文档 ID。
            payload: 重建任务私有载荷。文档替换时用于暂存新文件元数据，
                避免任务完成前污染当前可查询版本。

        Returns:
            新创建的重建索引任务 ID。
        """
        document = await self.document_repository.get(doc_id)
        if document is None:
            # 重建索引依赖已有文档元数据；记录缺失时直接终止。
            raise NonRetryableIndexError(f"document not found: {doc_id}")

        task = await self.task_repository.create(
            doc_id,
            task_type=IndexTaskType.REINDEX,
            payload=payload,
        )
        await self._commit_before_launch()
        self._launch_task(task.id, doc_id)
        return task.id

    async def _commit_before_launch(self) -> None:
        """在后台任务启动前提交当前事务。

        请求级服务创建任务后需要先提交事务，否则独立后台会话可能读不到任务记录；
        单元测试或同步调用场景不传钩子时保持原有事务边界。
        """
        if self.commit_before_launch is not None:
            await self.commit_before_launch()

    async def _commit_after_status_change(self) -> None:
        """提交任务开始状态，确保长耗时索引阶段对外可观测。

        请求级同步调用场景不传钩子时仍保持原有事务边界；后台独立会话会在
        `RUNNING/PROCESSING` 写入后立即提交，避免数据库里长时间停留在 PENDING。
        """
        if self.commit_after_status_change is not None:
            await self.commit_after_status_change()

    def _launch_task(self, task_id: int, doc_id: int) -> None:
        """创建后台任务并维护任务引用，避免任务被提前回收。

        Args:
            task_id: 已落库的索引任务 ID。
            doc_id: 任务关联的文档 ID。
        """
        task = asyncio.create_task(self._run_launched_task(task_id, doc_id))
        self._background_tasks.add(task)
        task.add_done_callback(self._handle_background_task_done)

    def _handle_background_task_done(self, task: asyncio.Task[None]) -> None:
        """回收后台任务引用并记录未处理异常。

        Args:
            task: 已结束的 asyncio 任务。

        Returns:
            无返回值。
        """
        self._background_tasks.discard(task)
        try:
            task.result()
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("索引后台任务执行异常")

    async def _run_launched_task(self, task_id: int, doc_id: int) -> None:
        """使用独立后台服务执行索引任务。

        Args:
            task_id: 索引任务 ID。
            doc_id: 任务关联的文档 ID。

        Returns:
            无返回值。
        """
        if self.background_service_factory is None:
            await self.run_task(task_id, doc_id)
            return

        # 后台任务不能复用请求级 AsyncSession；每次执行都创建独立服务和事务。
        async with self.background_service_factory() as service:
            await service.run_task(task_id, doc_id)

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
        index_source = self._resolve_index_source(document, task.payload)

        await self.task_repository.mark_running(task_id)
        if not self._should_keep_document_published(task.task_type, document):
            await self.document_repository.mark_processing(doc_id)
        await self._commit_after_status_change()

        # 第二步：下载持久化源文件。MinIO/网络/IO 异常通常是瞬时问题，允许任务级重试。
        try:
            raw_file = await self.storage_service.download(index_source.minio_path)
        except IndexPipelineError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise RetryableIndexError(f"storage download failed: {exc}") from exc

        # 第三步：解析并分块。空分块可能来自解析抖动或文件内容异常，交给重试上限兜底。
        logger.info("文档加载阶段开始了...")
        parsed_docs = await asyncio.to_thread(
            self.loader_service.load,
            BytesIO(raw_file),
            index_source.file_name,
        )
        logger.info("文档加载阶段结束了...")
        logger.info("文档分块阶段开始了...")
        chunks = await asyncio.to_thread(self.chunk_service.split_documents, parsed_docs)
        logger.info("文档分块阶段结束了...")
        if not chunks:
            raise RetryableIndexError("no valid chunks generated from document")

        # 第四步：批量向量化。Embedding 服务异常和数量不一致都会破坏 chunk/vector 对齐。
        texts = [chunk.page_content for chunk in chunks]
        try:
            logger.info("Embedding阶段开始了...")
            vectors = await self.embedding_service.embed_documents(texts)
            logger.info("Embedding阶段结束了...")
        except IndexPipelineError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise RetryableIndexError(f"embedding failed: {exc}") from exc

        if len(vectors) != len(chunks):
            raise RetryableIndexError(
                f"embedding result count mismatch: expected {len(chunks)}, got {len(vectors)}"
            )

        # 第五步：按任务类型确定 chunk 版本。首次索引用初始版本，重建索引才递增版本。
        new_version = self._resolve_doc_version(task.task_type, document.version)
        doc_chunks = self._build_doc_chunks(document, chunks, vectors, new_version)
        token_count = sum(chunk.token_count for chunk in doc_chunks)

        await self.chunk_repository.insert_many(doc_chunks)
        logger.info(
            "chunk 入库完成：doc_id=%s chunk_count=%s doc_version=%s token_count=%s",
            doc_id,
            len(doc_chunks),
            new_version,
            token_count,
        )
        old_chunk_ids: list[int] = []
        if task.task_type == IndexTaskType.REINDEX.value:
            old_chunk_ids = await self.chunk_repository.list_older_version_ids(
                doc_id,
                new_version,
            )
        publish_source = index_source if index_source.is_replacement else None
        await self.document_repository.mark_done(
            doc_id,
            chunk_count=len(doc_chunks),
            token_count=token_count,
            version=new_version,
            file_name=publish_source.file_name if publish_source is not None else None,
            file_type=publish_source.file_type if publish_source is not None else None,
            file_size=publish_source.file_size if publish_source is not None else None,
            minio_path=publish_source.minio_path if publish_source is not None else None,
        )
        if old_chunk_ids:
            await self.evaluation_repository.invalidate_reindexed_chunk_labels(
                kb_id=document.kb_id,
                old_chunk_ids=old_chunk_ids,
            )
        # 新版本已完整写入后再清理旧版本；若清理失败，旧数据残留也不会影响最新版本查询。
        await self.chunk_repository.delete_older_versions(doc_id, new_version)
        if index_source.old_minio_path is not None:
            await self.storage_service.delete(index_source.old_minio_path)
        logger.info(
            "旧版本 chunk 清理完成：doc_id=%s current_version=%s",
            doc_id,
            new_version,
        )
        await self.task_repository.mark_done(task_id)
        logger.info(
            "索引任务 DONE：task_id=%s doc_id=%s doc_version=%s",
            task_id,
            doc_id,
            new_version,
        )

    def _resolve_doc_version(self, task_type: str, current_version: int) -> int:
        """根据索引任务类型计算本次写入的文档版本号。

        Args:
            task_type: 索引任务类型，来自 `kb_index_task.task_type`。
            current_version: 文档当前版本号。

        Returns:
            本次 chunk 写入和文档完成状态应使用的版本号。
        """
        if task_type == IndexTaskType.INDEX.value:
            return current_version
        if task_type == IndexTaskType.REINDEX.value:
            return current_version + 1
        raise NonRetryableIndexError(f"unknown index task type: {task_type}")

    def _should_keep_document_published(self, task_type: str, document: KbDocument) -> bool:
        """判断执行态是否应保持当前发布文档状态不变。

        已完成文档的 REINDEX 只是构建下一版本，任务状态应写入 `kb_index_task`，
        不能把主文档改成 PROCESSING，否则查询层的 DONE 过滤会屏蔽旧版本 chunk。
        """
        return task_type == IndexTaskType.REINDEX.value and document.status == DocumentStatus.DONE.value

    def _resolve_index_source(
        self,
        document: KbDocument,
        payload: dict[str, object] | None,
    ) -> IndexSource:
        """解析本次索引实际使用的原文件信息。

        普通索引和强制重建使用当前文档元数据；文档替换任务从 payload 中读取
        新文件元数据，并等索引成功后再发布到 `kb_document`。
        """
        source_payload = payload.get("source") if isinstance(payload, dict) else None
        if source_payload is None:
            return IndexSource(
                file_name=document.file_name,
                file_type=document.file_type,
                file_size=document.file_size,
                minio_path=document.minio_path,
            )
        if not isinstance(source_payload, dict):
            raise NonRetryableIndexError("invalid index task payload: source must be object")

        try:
            file_name = str(source_payload["file_name"])
            file_type = str(source_payload["file_type"])
            file_size = int(source_payload["file_size"])
            minio_path = str(source_payload["minio_path"])
        except (KeyError, TypeError, ValueError) as exc:
            raise NonRetryableIndexError("invalid index task payload: missing source metadata") from exc

        old_minio_path = payload.get("old_minio_path") if isinstance(payload, dict) else None
        return IndexSource(
            file_name=file_name,
            file_type=file_type,
            file_size=file_size,
            minio_path=minio_path,
            old_minio_path=str(old_minio_path) if old_minio_path is not None else None,
        )

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
        # 发布事务必须先整体回滚，再用新事务记录失败状态，避免版本与标注部分生效。
        if self.rollback_before_failure_status is not None:
            await self.rollback_before_failure_status()
        await self.task_repository.mark_failed(task_id, error_msg)
        task = await self.task_repository.get(task_id)
        document = await self.document_repository.get(doc_id)
        if document is None:
            return
        if task is not None and self._should_keep_document_published(task.task_type, document):
            return
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
        task.add_done_callback(self._handle_background_task_done)

    async def _delayed_retry(self, task_id: int, doc_id: int, delay_seconds: int) -> None:
        """等待指定时间后重新启动索引任务。

        Args:
            task_id: 待重投递任务 ID。
            doc_id: 任务关联的文档 ID。
            delay_seconds: 指数退避计算出的等待秒数。
        """
        await asyncio.sleep(delay_seconds)
        self._launch_task(task_id, doc_id)
