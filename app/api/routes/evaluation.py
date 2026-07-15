from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_current_user
from app.api.routes.knowledge_bases import get_permission_service
from app.api.routes.rag import RagQueryPipeline, get_rag_query_service
from app.core.clients import get_chat_model, get_embeddings
from app.core.context import CurrentUser
from app.core.database import get_db
from app.evaluation.dataset_service import EvaluationDatasetService
from app.evaluation.ragas_evaluator import RagasEvaluator
from app.evaluation.service import (
    EvaluationRagExecutor,
    EvaluationRunService,
    GenerationEvaluator,
)
from app.models import EvalDatasetStatus
from app.repositories.evaluations import EvaluationRepository
from app.schemas.common import ApiResponse
from app.schemas.evaluation import (
    CurrentChunkSummaryItem,
    EvalDatasetItem,
    EvalDatasetWriteRequest,
    EvaluationReportItem,
    normalize_eval_version,
)
from app.services.permissions import PermissionService
from app.services.rag_query_v4 import RagQueryServiceV4

router = APIRouter()


def get_evaluation_dataset_service(
    session: AsyncSession = Depends(get_db),
) -> EvaluationDatasetService:
    """构建标准问题集服务及其数据库仓储。"""
    return EvaluationDatasetService(EvaluationRepository(session))


def get_evaluation_run_service(
    session: AsyncSession = Depends(get_db),
) -> EvaluationRunService:
    """构建评估结果读写和运行编排服务。"""
    return EvaluationRunService(repository=EvaluationRepository(session))


def get_evaluation_rag_executor(
    rag_service: RagQueryPipeline = Depends(get_rag_query_service),
) -> EvaluationRagExecutor:
    """校验正式运行复用的是当前部署 V4 共享执行接口。"""
    if not isinstance(rag_service, RagQueryServiceV4):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="正式评估要求启用 V4 RAG 管道",
        )
    return rag_service


def get_evaluation_ragas_evaluator() -> GenerationEvaluator:
    """复用现有回答与 Embedding 客户端构建正式 RAGAS 适配器。"""
    return RagasEvaluator.from_clients(
        chat_model=get_chat_model(),
        embeddings=get_embeddings(),
    )


@router.post("/{kb_id}/run")
async def run_evaluation(
    kb_id: int,
    version: str = Query(...),
    user: CurrentUser = Depends(get_current_user),
    permission_service: PermissionService = Depends(get_permission_service),
    run_service: EvaluationRunService = Depends(get_evaluation_run_service),
    rag_executor: EvaluationRagExecutor = Depends(get_evaluation_rag_executor),
    ragas_evaluator: GenerationEvaluator = Depends(get_evaluation_ragas_evaluator),
) -> ApiResponse[EvaluationReportItem]:
    """同步运行当前 V4 管道并返回本次检索聚合报告。"""
    await permission_service.require_admin(kb_id, user)
    try:
        eval_version = normalize_eval_version(version)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="评估版本格式无效",
        ) from exc
    report = await run_service.run(
        kb_id=kb_id,
        eval_version=eval_version,
        user=user,
        rag_executor=rag_executor,
        ragas_evaluator=ragas_evaluator,
    )
    return ApiResponse.ok(EvaluationReportItem.model_validate(report))


@router.get("/{kb_id}/history")
async def list_evaluation_history(
    kb_id: int,
    user: CurrentUser = Depends(get_current_user),
    permission_service: PermissionService = Depends(get_permission_service),
    run_service: EvaluationRunService = Depends(get_evaluation_run_service),
) -> ApiResponse[list[EvaluationReportItem]]:
    """按评估时间倒序返回聚合历史，不暴露逐题结果。"""
    await permission_service.require_admin(kb_id, user)
    reports = await run_service.list_history(kb_id=kb_id)
    return ApiResponse.ok([EvaluationReportItem.model_validate(item) for item in reports])


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
