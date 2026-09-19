import asyncio
import json

import pytest
import opencontext.server.routes.settings as settings_routes

from opencontext.server.routes.settings import (
    ModelSettingsVO,
    _effective_embedding_config,
    _models_with_image_input,
    _subscription_models,
    _subscription_status_payload,
    _validate_subscription_model,
)
from opencontext.llm.global_vlm_client import _messages_contain_image


def _subscription_settings(**overrides):
    values = {
        "modelPlatform": "chatgpt_subscription",
        "modelId": "vision-default",
        "embeddingModelId": "existing-embedding",
    }
    values.update(overrides)
    return ModelSettingsVO(**values)


def test_subscription_preserves_complete_embedding_configuration():
    current = {
        "provider": "custom-embedding",
        "model": "existing-embedding",
        "base_url": "https://embedding.example/v1",
        "api_key": "embedding-secret",
        "output_dim": 1536,
    }

    result = _effective_embedding_config(_subscription_settings(), current)

    assert result == current


def test_subscription_never_borrows_vlm_credentials_for_embeddings():
    settings = _subscription_settings(
        baseUrl="https://should-not-be-used.example/v1",
        apiKey="should-not-be-used",
    )

    with pytest.raises(ValueError, match="Embedding configuration"):
        _effective_embedding_config(settings, {})


def test_account_status_does_not_expose_tokens():
    status = _subscription_status_payload(
        {
            "account": {
                "type": "chatgpt",
                "email": "person@example.com",
                "planType": "pro",
                "accessToken": "must-not-leak",
            },
            "refreshToken": "must-not-leak",
        }
    )

    assert status == {
        "authenticated": True,
        "email": "person@example.com",
        "planType": "pro",
        "type": "chatgpt",
    }


def test_subscription_status_refresh_is_explicit(monkeypatch):
    seen = {}

    async def fake_runtime_call(runtime, names, **kwargs):
        seen.update(kwargs)
        return {"account": {"type": "chatgpt", "planType": "pro"}}

    monkeypatch.setattr(settings_routes, "_get_subscription_runtime", lambda: object())
    monkeypatch.setattr(settings_routes, "_runtime_call_async", fake_runtime_call)

    response = asyncio.run(settings_routes.get_subscription_status(refresh=True, _auth=""))
    payload = json.loads(response.body)

    assert seen["refresh_token"] is True
    assert payload["data"]["authenticated"] is True


def test_model_filter_and_save_validation_require_image_input():
    class Runtime:
        def list_models(self):
            return {
                "data": [
                    {"id": "text-only", "inputModalities": ["text"]},
                    {
                        "id": "vision-default",
                        "inputModalities": ["text", "image"],
                        "supportedReasoningEfforts": [
                            {"reasoningEffort": "low"},
                            {"reasoningEffort": "medium"},
                        ],
                        "isDefault": True,
                    },
                ]
            }

    models = _models_with_image_input(Runtime().list_models())
    assert [model["id"] for model in models] == ["vision-default"]
    assert asyncio.run(_validate_subscription_model(Runtime(), "vision-default")) == (
        True,
        "ok",
    )
    valid, message = asyncio.run(_validate_subscription_model(Runtime(), "text-only"))
    assert valid is False
    assert "image input" in message
    assert asyncio.run(
        _validate_subscription_model(
            Runtime(),
            "vision-default",
            reasoning_effort="medium",
            require_image=False,
        )
    ) == (True, "ok")
    valid, message = asyncio.run(
        _validate_subscription_model(
            Runtime(),
            "vision-default",
            reasoning_effort="max",
            require_image=False,
        )
    )
    assert valid is False
    assert "reasoning effort" in message


def test_model_catalog_keeps_text_models_and_supports_legacy_modalities():
    models = _subscription_models(
        {
            "data": [
                {"id": "text-only", "inputModalities": ["text"]},
                {"id": "legacy"},
                {"id": "hidden", "hidden": True},
            ]
        }
    )

    assert [model["id"] for model in models] == ["text-only", "legacy"]
    assert models[1]["inputModalities"] == ["text", "image"]


def test_image_requests_are_routed_without_changing_text_requests():
    assert _messages_contain_image([{"role": "user", "content": "hello"}]) is False
    assert _messages_contain_image(
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "describe this"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}},
                ],
            }
        ]
    ) is True
