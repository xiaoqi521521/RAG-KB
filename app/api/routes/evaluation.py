from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_current_user
from app.api.dependencies import get_app_token_metrics
from app.api.routes.knowledge_bases import get_permission_service
from app.api.routes.rag import (
    build_rag_query_service,
    get_token_budget_gate,
)
from app.core.clients import get_chat_model, get_embeddings
from app.core.config import Settings, get_settings
from app.core.context import CurrentUser, current_user_var
from app.core.database import AsyncSessionLocal, get_db
from app.evaluation.dataset_service import EvaluationDatasetService
from app.evaluation.ragas_evaluator import RagasEvaluator
from app.evaluation.service import (
    EvaluationRagExecutor,
    EvaluationRunService,
    EvaluationUsageCollector,
    GenerationEvaluator,
)
from app.models import EvalDatasetStatus
from app.repositories.evaluations import EvaluationRepository
from app.schemas.common import ApiResponse
from app.schemas.evaluation import (
    CurrentChunkSummaryItem,
    EvalDatasetItem,
    EvalDatasetWriteRequest,
    EvaluationHistoryPage,
    EvaluationReportItem,
    normalize_eval_version,
)
from app.services.permissions import PermissionService
from app.services.rag_query_v4 import RagQueryServiceV4
from app.services.token_metrics import (
    TokenUsageRecorder,
    evaluation_usage_capture_var,
    suppress_user_usage_var,
)
from app.services.token_budget import GlobalTokenBudgetGate

router = APIRouter()


def get_evaluation_dataset_service(
    session: AsyncSession = Depends(get_db),
) -> EvaluationDatasetService:
    """构建标准问题集服务及其数据库仓储。"""
    return EvaluationDatasetService(EvaluationRepository(session))


def get_evaluation_run_service(
    session: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
    token_metrics: TokenUsageRecorder = Depends(get_app_token_metrics),
    budget_gate: GlobalTokenBudgetGate = Depends(get_token_budget_gate),
) -> EvaluationRunService:
    """构建评估结果读写和运行编排服务。"""
    return EvaluationRunService(
        repository=EvaluationRepository(session),
        rag_concurrency=settings.evaluation_rag_concurrency,
        token_metrics=token_metrics,
        budget_gate=budget_gate,
        token_chat_model_name=settings.chat_model,
        token_embedding_model_name=settings.embedding_model,
    )


class SessionScopedEvaluationRagExecutor:
    """为每次评估题创建独立数据库会话，支持有界并行执行。"""

    def __init__(
        self,
        *,
        settings: Settings,
        token_metrics: TokenUsageRecorder,
    ) -> None:
        self.settings = settings
        self.token_metrics = token_metrics

    async def execute(
        self,
        *,
        question: str,
        kb_ids: list[int],
        user: CurrentUser,
        usage_collector: EvaluationUsageCollector,
    ):
        """在独立会话中执行单题 V4 管道，避免共享 AsyncSession 并发冲突。"""
        current_user_token = current_user_var.set(user)
        # 评估消耗不归属任何个人：抑制个人 Redis 累计，监控与预算照常。
        suppress_token = suppress_user_usage_var.set(True)
        capture_token = evaluation_usage_capture_var.set(usage_collector)
        try:
            async with AsyncSessionLocal() as session:
                rag_service = build_rag_query_service(
                    session=session,
                    settings=self.settings,
                    token_metrics=self.token_metrics,
                    permission_service=get_permission_service(session),
                )
                if not isinstance(rag_service, RagQueryServiceV4):
                    raise HTTPException(
                        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                        detail="正式评估要求启用 V4 RAG 管道",
                    )
                return await rag_service.execute(
                    question=question,
                    kb_ids=kb_ids,
                    user=user,
                )
        finally:
            current_user_var.reset(current_user_token)
            suppress_user_usage_var.reset(suppress_token)
            evaluation_usage_capture_var.reset(capture_token)


def get_evaluation_rag_executor(
    settings: Settings = Depends(get_settings),
    token_metrics: TokenUsageRecorder = Depends(get_app_token_metrics),
) -> EvaluationRagExecutor:
    """校验当前部署启用 V4 管道，并返回支持并发的评估执行器。"""
    if settings.rag_query_pipeline != "v4":
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="正式评估要求启用 V4 RAG 管道",
        )
    return SessionScopedEvaluationRagExecutor(
        settings=settings,
        token_metrics=token_metrics,
    )


def get_evaluation_ragas_evaluator() -> GenerationEvaluator:
    """复用现有回答与 Embedding 客户端构建正式 RAGAS 适配器。"""
    settings = get_settings()
    return RagasEvaluator.from_clients(
        chat_model=get_chat_model(),
        embeddings=get_embeddings(),
        max_tokens=settings.ragas_max_tokens,
        timeout_seconds=settings.ragas_timeout_seconds,
    )


@router.post("/{kb_id}/run")
async def run_evaluation(
    kb_id: int,
    user: CurrentUser = Depends(get_current_user),
    permission_service: PermissionService = Depends(get_permission_service),
    run_service: EvaluationRunService = Depends(get_evaluation_run_service),
    rag_executor: EvaluationRagExecutor = Depends(get_evaluation_rag_executor),
    ragas_evaluator: GenerationEvaluator = Depends(get_evaluation_ragas_evaluator),
    budget_gate: GlobalTokenBudgetGate = Depends(get_token_budget_gate),
) -> ApiResponse[EvaluationReportItem]:
    """同步运行当前 V4 管道并自动使用下一个评估版本。"""
    import logging

    logger = logging.getLogger(__name__)
    logger.info("Starting evaluation run: kb_id=%s user_id=%s", kb_id, user.user_id)

    await permission_service.require_admin(kb_id, user)
    # 评估是一整批模型调用，纳入同一请求成本观测；超阈值时复用全局高成本告警。
    async with budget_gate.request_scope():
        report = await run_service.run(
            kb_id=kb_id,
            user=user,
            rag_executor=rag_executor,
            ragas_evaluator=ragas_evaluator,
        )
    logger.info("Evaluation run completed: kb_id=%s eval_version=%s", kb_id, report.eval_version)
    return ApiResponse.ok(EvaluationReportItem.model_validate(report))


@router.get("/{kb_id}/history")
async def list_evaluation_history(
    kb_id: int,
    version: str | None = Query(default=None),
    page: int | None = Query(default=None, ge=1),
    page_size: int | None = Query(default=None, ge=1, le=100),
    user: CurrentUser = Depends(get_current_user),
    permission_service: PermissionService = Depends(get_permission_service),
    run_service: EvaluationRunService = Depends(get_evaluation_run_service),
) -> ApiResponse[list[EvaluationReportItem] | EvaluationHistoryPage]:
    """按评估时间倒序返回聚合历史，分页参数存在时附带总数和版本列表。"""
    await permission_service.require_admin(kb_id, user)
    eval_version = None
    if version is not None:
        try:
            eval_version = normalize_eval_version(version)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="评估版本格式无效",
            ) from exc
    if page is None and page_size is None:
        reports = await run_service.list_history(kb_id=kb_id, eval_version=eval_version)
        return ApiResponse.ok([EvaluationReportItem.model_validate(item) for item in reports])
    effective_page = page or 1
    effective_page_size = page_size or 10
    offset = (effective_page - 1) * effective_page_size
    reports = await run_service.list_history(
        kb_id=kb_id,
        eval_version=eval_version,
        limit=effective_page_size,
        offset=offset,
    )
    total = await run_service.count_history(kb_id=kb_id, eval_version=eval_version)
    versions = await run_service.list_history_versions(kb_id=kb_id)
    data = EvaluationHistoryPage(
        items=[EvaluationReportItem.model_validate(item) for item in reports],
        total=total,
        page=effective_page,
        page_size=effective_page_size,
        versions=versions,
    )
    return ApiResponse.ok(data)


@router.get("/{kb_id}/dataset")
async def list_evaluation_datasets(
    kb_id: int,
    status_filter: EvalDatasetStatus | None = Query(default=None, alias="status"),
    user: CurrentUser = Depends(get_current_user),
    permission_service: PermissionService = Depends(get_permission_service),
    dataset_service: EvaluationDatasetService = Depends(get_evaluation_dataset_service),
) -> ApiResponse[list[EvalDatasetItem]]:
    """列出目标知识库的全部或指定状态标准问题。"""
    await permission_service.require_admin(kb_id, user)
    datasets = await dataset_service.list_datasets(
        kb_id=kb_id,
        status_filter=status_filter,
    )
    return ApiResponse.ok([EvalDatasetItem.model_validate(item) for item in datasets])


@router.post("/{kb_id}/dataset", status_code=status.HTTP_201_CREATED)
async def create_evaluation_dataset(
    kb_id: int,
    request: EvalDatasetWriteRequest,
    user: CurrentUser = Depends(get_current_user),
    permission_service: PermissionService = Depends(get_permission_service),
    dataset_service: EvaluationDatasetService = Depends(get_evaluation_dataset_service),
) -> ApiResponse[EvalDatasetItem]:
    """创建默认处于 ACTIVE 的人工标准问题。"""
    await permission_service.require_admin(kb_id, user)
    dataset = await dataset_service.create_dataset(kb_id=kb_id, request=request, user=user)
    return ApiResponse.ok(EvalDatasetItem.model_validate(dataset))


@router.put("/{kb_id}/dataset/{dataset_id}")
async def update_evaluation_dataset(
    kb_id: int,
    dataset_id: int,
    request: EvalDatasetWriteRequest,
    user: CurrentUser = Depends(get_current_user),
    permission_service: PermissionService = Depends(get_permission_service),
    dataset_service: EvaluationDatasetService = Depends(get_evaluation_dataset_service),
) -> ApiResponse[EvalDatasetItem]:
    """编辑尚未参与评估的标准问题及其标注。"""
    await permission_service.require_admin(kb_id, user)
    dataset = await dataset_service.update_dataset(
        kb_id=kb_id,
        dataset_id=dataset_id,
        request=request,
    )
    return ApiResponse.ok(EvalDatasetItem.model_validate(dataset))


@router.delete("/{kb_id}/dataset/{dataset_id}")
async def archive_evaluation_dataset(
    kb_id: int,
    dataset_id: int,
    user: CurrentUser = Depends(get_current_user),
    permission_service: PermissionService = Depends(get_permission_service),
    dataset_service: EvaluationDatasetService = Depends(get_evaluation_dataset_service),
) -> ApiResponse[EvalDatasetItem]:
    """幂等归档标准问题，不物理删除历史记录。"""
    await permission_service.require_admin(kb_id, user)
    dataset = await dataset_service.archive_dataset(kb_id=kb_id, dataset_id=dataset_id)
    return ApiResponse.ok(EvalDatasetItem.model_validate(dataset))


@router.get("/{kb_id}/chunks")
async def list_current_evaluation_chunks(
    kb_id: int,
    user: CurrentUser = Depends(get_current_user),
    permission_service: PermissionService = Depends(get_permission_service),
    dataset_service: EvaluationDatasetService = Depends(get_evaluation_dataset_service),
) -> ApiResponse[list[CurrentChunkSummaryItem]]:
    """不分页列出当前已发布 chunk 的安全标注摘要。"""
    await permission_service.require_admin(kb_id, user)
    chunks = await dataset_service.list_current_chunks(kb_id=kb_id)
    return ApiResponse.ok([CurrentChunkSummaryItem.model_validate(item) for item in chunks])
