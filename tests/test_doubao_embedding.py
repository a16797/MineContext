import asyncio
from types import SimpleNamespace

import pytest

from opencontext.llm.llm_client import LLMClient, LLMProvider, LLMType


class RecordingCreate:
    def __init__(self, response):
        self.response = response
        self.kwargs = None

    def __call__(self, **kwargs):
        self.kwargs = kwargs
        return self.response


def _client(create, *, output_dim=None):
    client = object.__new__(LLMClient)
    client.llm_type = LLMType.EMBEDDING
    client.provider = LLMProvider.DOUBAO.value
    client.model = "doubao-embedding-vision-251215"
    client.config = {} if output_dim is None else {"output_dim": output_dim}
    client.client = SimpleNamespace(multimodal_embeddings=SimpleNamespace(create=create))
    return client


def _response(values):
    return SimpleNamespace(data=SimpleNamespace(embedding=values))


def test_doubao_sync_embedding_uses_single_dense_vector_request():
    create = RecordingCreate(_response([0.1, 0.2, 0.3]))
    client = _client(create, output_dim=3)

    result = client.generate_embedding("hello")

    assert result == [0.1, 0.2, 0.3]
    assert create.kwargs == {
        "model": "doubao-embedding-vision-251215",
        "input": [{"type": "text", "text": "hello"}],
        "dimensions": 3,
        "encoding_format": "float",
    }


def test_doubao_defaults_to_2048_dimensions():
    create = RecordingCreate(_response([0.0] * 2048))
    client = _client(create)

    result = client.generate_embedding("hello")

    assert len(result) == 2048
    assert create.kwargs["dimensions"] == 2048
    assert create.kwargs["encoding_format"] == "float"


def test_doubao_async_embedding_runs_sync_sdk_in_worker_thread():
    create = RecordingCreate(_response([0.1, 0.2]))
    client = _client(create, output_dim=2)

    result = asyncio.run(client.generate_embedding_async("hello"))

    assert result == [0.1, 0.2]
    assert create.kwargs["input"] == [{"type": "text", "text": "hello"}]
    assert create.kwargs["dimensions"] == 2
    assert create.kwargs["encoding_format"] == "float"


def test_doubao_rejects_wrong_dimension_instead_of_truncating():
    create = RecordingCreate(_response([0.1, 0.2, 0.3]))
    client = _client(create, output_dim=2)

    with pytest.raises(ValueError, match="dimension mismatch"):
        client.generate_embedding("hello")


def test_doubao_validate_uses_same_dimension_and_response_checks():
    create = RecordingCreate(_response([0.1, 0.2]))
    client = _client(create, output_dim=2)

    valid, message = client.validate()

    assert valid is True
    assert message == "Embedding model validation successful"
    assert create.kwargs["input"] == [{"type": "text", "text": "test"}]
    assert create.kwargs["dimensions"] == 2
    assert create.kwargs["encoding_format"] == "float"
