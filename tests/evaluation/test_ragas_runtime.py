def test_locked_ragas_runtime_imports_production_types() -> None:
    """锁定依赖必须能真实加载生产适配器使用的 RAGAS 类型。"""
    from ragas.embeddings.base import BaseRagasEmbedding
    from ragas.llms.base import InstructorBaseRagasLLM
    from ragas.metrics.collections import (
        AnswerRelevancy,
        ContextPrecisionWithReference,
        ContextRecall,
        Faithfulness,
    )

    assert all(
        value is not None
        for value in (
            BaseRagasEmbedding,
            InstructorBaseRagasLLM,
            Faithfulness,
            AnswerRelevancy,
            ContextRecall,
            ContextPrecisionWithReference,
        )
    )
