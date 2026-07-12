from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
async def test_openai_compatible_embeddings_returns_vectors_and_provider_usage() -> None:
    from app.integrations.openai_embeddings import OpenAICompatibleEmbeddings

    response = SimpleNamespace(
        data=[
            SimpleNamespace(index=1, embedding=[0.2, 0.3]),
            SimpleNamespace(index=0, embedding=[0.1, 0.4]),
        ],
        usage=SimpleNamespace(total_tokens=12),
    )

    class FakeEmbeddingsApi:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        async def create(self, **kwargs: object) -> object:
            self.calls.append(kwargs)
            return response

    api = FakeEmbeddingsApi()
    client = OpenAICompatibleEmbeddings(
        embeddings_api=api,
        model="text-embedding-v3",
    )

    vectors, total_tokens = await client.aembed_documents_with_usage(["a", "b"])

    assert vectors == [[0.1, 0.4], [0.2, 0.3]]
    assert total_tokens == 12
    assert api.calls == [{"input": ["a", "b"], "model": "text-embedding-v3"}]
