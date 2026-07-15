from app.evaluation.metrics import calculate_retrieval_metrics


def test_retrieval_metrics_hit_at_rank_one_and_five() -> None:
    first = calculate_retrieval_metrics([10, 11, 12, 13, 14], [10])
    fifth = calculate_retrieval_metrics([10, 11, 12, 13, 14], [14])

    assert (first.hit, first.rank, first.reciprocal_rank) == (True, 1, 1.0)
    assert (fifth.hit, fifth.rank, fifth.reciprocal_rank) == (True, 5, 0.2)


def test_retrieval_metrics_ignore_rank_six_and_return_zero_for_miss() -> None:
    result = calculate_retrieval_metrics([10, 11, 12, 13, 14, 15], [15])

    assert result.hit is False
    assert result.rank is None
    assert result.reciprocal_rank == 0.0


def test_retrieval_metrics_use_earliest_expected_chunk() -> None:
    result = calculate_retrieval_metrics([10, 11, 12, 13, 14], [14, 11])

    assert result.hit is True
    assert result.rank == 2
    assert result.reciprocal_rank == 0.5


def test_retrieval_metrics_treat_normal_empty_candidates_as_miss() -> None:
    result = calculate_retrieval_metrics([], [10])

    assert result.hit is False
    assert result.rank is None
    assert result.reciprocal_rank == 0.0
