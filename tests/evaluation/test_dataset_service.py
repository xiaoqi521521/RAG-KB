from __future__ import annotations

from datetime import datetime

import pytest
from fastapi import HTTPException

from app.core.context import CurrentUser
from app.evaluation.dataset_service import EvaluationDatasetService
from app.models import EvalDataset, EvalDatasetStatus
from app.schemas.evaluation import EvalDatasetWriteRequest


def _user() -> CurrentUser:
    return CurrentUser(user_id=7, department_id="engineering", role="USER")


class FakeEvaluationRepository:
    def __init__(
        self,
        *,
        datasets: list[EvalDataset] | None = None,
        valid_chunk_ids: list[int] | None = None,
        evaluated_ids: set[int] | None = None,
    ) -> None:
        self.datasets = {(item.kb_id, item.id): item for item in datasets or []}
        self.valid_chunk_ids = valid_chunk_ids or []
        self.evaluated_ids = evaluated_ids or set()
        self.created: list[dict[str, object]] = []
        self.chunk_validation_calls: list[tuple[int, list[int]]] = []

    async def get_dataset(self, *, kb_id: int, dataset_id: int) -> EvalDataset | None:
        return self.datasets.get((kb_id, dataset_id))

    async def create_dataset(self, **kwargs: object) -> EvalDataset:
        self.created.append(kwargs)
        return EvalDataset(id=20, created_at=datetime(2026, 7, 15), **kwargs)

    async def save_dataset(self, dataset: EvalDataset) -> EvalDataset:
        return dataset

    async def has_results(self, dataset_id: int) -> bool:
        return dataset_id in self.evaluated_ids

    async def list_valid_chunk_ids(self, *, kb_id: int, chunk_ids: list[int]) -> list[int]:
        self.chunk_validation_calls.append((kb_id, chunk_ids))
        return self.valid_chunk_ids


@pytest.mark.asyncio
async def test_create_manual_dataset_defaults_active_and_validates_all_chunks() -> None:
    repository = FakeEvaluationRepository(valid_chunk_ids=[10, 11])
    service = EvaluationDatasetService(repository)  # type: ignore[arg-type]

    dataset = await service.create_dataset(
        kb_id=3,
        request=EvalDatasetWriteRequest(
            question="  报销时限？ ",
            expected_answer=None,
            expected_chunk_ids=[10, 11, 10],
        ),
        user=_user(),
    )

    assert dataset.status == EvalDatasetStatus.ACTIVE.value
    assert dataset.question == "报销时限？"
    assert dataset.expected_chunk_ids == [10, 11]
    assert dataset.created_by == 7
    assert repository.chunk_validation_calls == [(3, [10, 11])]


@pytest.mark.asyncio
async def test_create_rejects_entire_write_when_any_expected_chunk_is_invalid() -> None:
    repository = FakeEvaluationRepository(valid_chunk_ids=[10])
    service = EvaluationDatasetService(repository)  # type: ignore[arg-type]

    with pytest.raises(HTTPException) as exc_info:
        await service.create_dataset(
            kb_id=3,
            request=EvalDatasetWriteRequest(
                question="报销时限？",
                expected_chunk_ids=[10, 99],
            ),
            user=_user(),
        )

    assert exc_info.value.status_code == 422
    assert repository.created == []


@pytest.mark.asyncio
async def test_update_rejects_dataset_that_already_has_evaluation_results() -> None:
    dataset = EvalDataset(
        id=9,
        kb_id=3,
        question="旧问题",
        expected_answer="旧答案",
        status=EvalDatasetStatus.ACTIVE.value,
        created_by=1,
    )
    repository = FakeEvaluationRepository(datasets=[dataset], evaluated_ids={9})
    service = EvaluationDatasetService(repository)  # type: ignore[arg-type]

    with pytest.raises(HTTPException) as exc_info:
        await service.update_dataset(
            kb_id=3,
            dataset_id=9,
            request=EvalDatasetWriteRequest(question="新问题", expected_answer="新答案"),
        )

    assert exc_info.value.status_code == 409
    assert dataset.question == "旧问题"


@pytest.mark.asyncio
async def test_update_allows_evaluated_dataset_to_be_archived_without_content_change() -> None:
    dataset = EvalDataset(
        id=9,
        kb_id=3,
        question="旧问题",
        expected_answer="旧答案",
        expected_chunk_ids=[10],
        status=EvalDatasetStatus.ACTIVE.value,
        created_by=1,
    )
    repository = FakeEvaluationRepository(datasets=[dataset], evaluated_ids={9})
    service = EvaluationDatasetService(repository)  # type: ignore[arg-type]

    archived = await service.update_dataset(
        kb_id=3,
        dataset_id=9,
        request=EvalDatasetWriteRequest(
            question="旧问题",
            expected_answer="旧答案",
            expected_chunk_ids=[10],
            status=EvalDatasetStatus.ARCHIVED.value,
        ),
    )

    assert archived is dataset
    assert archived.status == EvalDatasetStatus.ARCHIVED.value
    assert archived.question == "旧问题"
    assert archived.expected_chunk_ids == [10]


@pytest.mark.asyncio
async def test_update_rejects_content_change_while_archiving_evaluated_dataset() -> None:
    dataset = EvalDataset(
        id=9,
        kb_id=3,
        question="旧问题",
        expected_answer="旧答案",
        expected_chunk_ids=[10],
        status=EvalDatasetStatus.ACTIVE.value,
        created_by=1,
    )
    repository = FakeEvaluationRepository(datasets=[dataset], evaluated_ids={9})
    service = EvaluationDatasetService(repository)  # type: ignore[arg-type]

    with pytest.raises(HTTPException) as exc_info:
        await service.update_dataset(
            kb_id=3,
            dataset_id=9,
            request=EvalDatasetWriteRequest(
                question="新问题",
                expected_answer="旧答案",
                expected_chunk_ids=[10],
                status=EvalDatasetStatus.ARCHIVED.value,
            ),
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == "已参与评估的标准问题仅允许归档，不能同时修改内容"
    assert dataset.status == EvalDatasetStatus.ACTIVE.value
    assert dataset.question == "旧问题"


@pytest.mark.asyncio
async def test_update_allows_archived_dataset_with_historical_results() -> None:
    dataset = EvalDataset(
        id=9,
        kb_id=3,
        question="旧问题",
        expected_answer="旧答案",
        status=EvalDatasetStatus.ARCHIVED.value,
        created_by=1,
    )
    repository = FakeEvaluationRepository(datasets=[dataset], evaluated_ids={9})
    service = EvaluationDatasetService(repository)  # type: ignore[arg-type]

    updated = await service.update_dataset(
        kb_id=3,
        dataset_id=9,
        request=EvalDatasetWriteRequest(
            question="修订后问题",
            expected_answer="修订后答案",
            status=EvalDatasetStatus.ACTIVE.value,
        ),
    )

    assert updated is dataset
    assert updated.question == "修订后问题"
    assert updated.expected_answer == "修订后答案"
    assert updated.status == EvalDatasetStatus.ACTIVE.value


@pytest.mark.asyncio
async def test_update_and_archive_are_saved_through_the_dataset_repository() -> None:
    dataset = EvalDataset(
        id=9,
        kb_id=3,
        question="旧问题",
        expected_answer="旧答案",
        status=EvalDatasetStatus.ACTIVE.value,
        created_by=1,
    )
    repository = FakeEvaluationRepository(datasets=[dataset])
    service = EvaluationDatasetService(repository)  # type: ignore[arg-type]

    updated = await service.update_dataset(
        kb_id=3,
        dataset_id=9,
        request=EvalDatasetWriteRequest(question="新问题", expected_answer="新答案"),
    )
    archived = await service.archive_dataset(kb_id=3, dataset_id=9)

    assert updated.question == "新问题"
    assert archived.status == EvalDatasetStatus.ARCHIVED.value


@pytest.mark.asyncio
async def test_archive_is_scoped_and_idempotent_without_physical_delete() -> None:
    dataset = EvalDataset(
        id=9,
        kb_id=3,
        question="问题",
        expected_answer="答案",
        status=EvalDatasetStatus.ACTIVE.value,
        created_by=1,
    )
    repository = FakeEvaluationRepository(datasets=[dataset])
    service = EvaluationDatasetService(repository)  # type: ignore[arg-type]

    first = await service.archive_dataset(kb_id=3, dataset_id=9)
    second = await service.archive_dataset(kb_id=3, dataset_id=9)

    assert first is dataset
    assert second is dataset
    assert dataset.status == EvalDatasetStatus.ARCHIVED.value
    with pytest.raises(HTTPException) as exc_info:
        await service.archive_dataset(kb_id=4, dataset_id=9)
    assert exc_info.value.status_code == 404
