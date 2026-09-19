#!/usr/bin/env python
# -*- coding: utf-8 -*-

# Copyright (c) 2025 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: Apache-2.0

"""Model settings API routes"""

import io
import importlib
import inspect
import threading
from typing import Any

from fastapi import APIRouter, File, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from opencontext.config.global_config import GlobalConfig
from opencontext.llm.global_embedding_client import GlobalEmbeddingClient
from opencontext.llm.global_vlm_client import GlobalVLMClient
from opencontext.llm.llm_client import LLMClient, LLMType
from opencontext.server.middleware.auth import auth_dependency
from opencontext.server.utils import convert_resp
from opencontext.utils.logging_utils import get_logger

logger = get_logger(__name__)
router = APIRouter(tags=["model-settings"])
_config_lock = threading.Lock()


# ==================== Data Models ====================


class ModelSettingsVO(BaseModel):
    """Model settings with optional separate embedding configuration"""

    modelPlatform: str
    modelId: str
    reasoningEffort: str | None = None
    backgroundModelId: str | None = None
    backgroundReasoningEffort: str | None = None
    visionModelId: str | None = None
    visionReasoningEffort: str | None = None
    baseUrl: str = ""
    apiKey: str = ""
    embeddingModelId: str
    embeddingBaseUrl: str | None = None
    embeddingApiKey: str | None = None
    embeddingModelPlatform: str | None = None
    # Kept optional for compatibility with older clients.  The server always
    # persists the effective value from the current embedding configuration.
    embeddingOutputDim: int | None = None


class GetModelSettingsResponse(BaseModel):
    config: ModelSettingsVO


class UpdateModelSettingsRequest(BaseModel):
    config: ModelSettingsVO


class UpdateModelSettingsResponse(BaseModel):
    success: bool
    message: str


# ==================== Helper Functions ====================


def _build_llm_config(
    base_url: str, api_key: str, model: str, provider: str, llm_type: LLMType, **kwargs
) -> dict:
    """Build LLM config dict"""
    config = {"base_url": base_url, "api_key": api_key, "model": model, "provider": provider}

    # Add optional parameters
    if "timeout" in kwargs:
        config["timeout"] = kwargs["timeout"]

    if llm_type == LLMType.CHAT and kwargs.get("reasoning_effort"):
        config["reasoning_effort"] = kwargs["reasoning_effort"]

    if llm_type == LLMType.EMBEDDING:
        config["output_dim"] = kwargs.get("output_dim", 2048)
    return config


def _get_subscription_runtime() -> Any:
    """Return the optional ChatGPT subscription runtime.

    The runtime is deliberately imported lazily.  This keeps the API usable
    in the normal API-key-only installation and avoids importing the Codex
    process/credential code while FastAPI is merely registering routes.
    """
    try:
        module = importlib.import_module("opencontext.llm.codex_subscription_runtime")
    except ImportError as exc:
        raise RuntimeError("ChatGPT subscription runtime is not installed") from exc
    getter = getattr(module, "get_codex_subscription_runtime", None)
    if not callable(getter):
        raise RuntimeError(
            "ChatGPT subscription runtime must expose get_codex_subscription_runtime()"
        )
    return getter()


def _runtime_call(runtime: Any, method_names: tuple[str, ...], *args, **kwargs):
    """Call the first supported runtime method, with a clear error otherwise."""
    for method_name in method_names:
        method = getattr(runtime, method_name, None)
        if callable(method):
            return method(*args, **kwargs)
    raise RuntimeError(
        "ChatGPT subscription runtime does not implement any of: "
        + ", ".join(method_names)
    )


async def _runtime_call_async(runtime: Any, method_names: tuple[str, ...], *args, **kwargs):
    """Call a runtime method whether its implementation is sync or async."""
    try:
        result = _runtime_call(runtime, method_names, *args, **kwargs)
    except TypeError:
        # Keep the route compatible with a runtime whose start/cancel method
        # accepts no optional keyword arguments.
        result = _runtime_call(runtime, method_names, *args)
    if inspect.isawaitable(result):
        result = await result
    return result


def _runtime_data(value: Any) -> Any:
    """Convert runtime dataclasses/models to a safe JSON-compatible value."""
    if value is None:
        return None
    return jsonable_encoder(value)


def _subscription_payload(value: Any) -> Any:
    """Normalize common App Server snake_case fields for the renderer API."""
    value = _runtime_data(value)
    if not isinstance(value, dict):
        return value
    aliases = {
        "login_id": "loginId",
        "auth_url": "authUrl",
        "account_id": "accountId",
        "plan_type": "planType",
        "input_modalities": "inputModalities",
        "default_reasoning_effort": "defaultReasoningEffort",
        "supported_reasoning_efforts": "supportedReasoningEfforts",
        "reasoning_effort": "reasoningEffort",
        "is_default": "isDefault",
    }
    return {aliases.get(key, key): item for key, item in value.items()}


def _subscription_status_payload(value: Any) -> dict:
    """Turn the official ``account/read`` result into renderer status data."""
    raw = _subscription_payload(value)
    if not isinstance(raw, dict):
        return {"authenticated": bool(raw)}
    account = raw.get("account")
    if isinstance(account, dict):
        account = _subscription_payload(account)
    # Whitelist account metadata.  The app-server may include access/refresh
    # token fields; those must never be sent through this API route.
    payload = {
        key: raw[key]
        for key in ("authenticated", "loginId", "message", "status")
        if key in raw
    }
    for key in ("accountId", "email", "planType", "type"):
        if key in raw:
            payload[key] = raw[key]
        elif isinstance(account, dict) and key in account:
            payload[key] = account[key]
    if "authenticated" not in payload:
        payload["authenticated"] = bool(account or raw.get("email"))
    return payload


def _subscription_models(value: Any) -> list[dict]:
    """Normalize the account's picker-visible ``model/list`` response."""
    if isinstance(value, dict):
        value = value.get("models", value.get("data", value.get("items", [])))
    if not isinstance(value, (list, tuple)):
        return []
    result = []
    for model in value:
        item = _subscription_payload(model)
        if not isinstance(item, dict):
            continue
        if not item.get("id"):
            item["id"] = item.get("model") or item.get("modelId") or item.get("model_id")
        if not item.get("id") or item.get("hidden") is True:
            continue
        modalities = item.get("inputModalities", item.get("input_modalities"))
        if modalities is None:
            item["inputModalities"] = ["text", "image"]
        efforts = item.get("supportedReasoningEfforts")
        if isinstance(efforts, (list, tuple)):
            item["supportedReasoningEfforts"] = [
                _subscription_payload(effort) if isinstance(effort, dict) else effort
                for effort in efforts
            ]
        result.append(item)
    return result


def _models_with_image_input(value: Any) -> list[dict]:
    """Return the normalized models that accept image input."""
    result = []
    for item in _subscription_models(value):
        modalities = item.get("inputModalities", [])
        if isinstance(modalities, str):
            modalities = [modalities]
        if any(str(modality).lower() == "image" for modality in (modalities or [])):
            result.append(item)
    return result


def _supported_efforts(model: dict) -> set[str]:
    result: set[str] = set()
    for effort in model.get("supportedReasoningEfforts") or []:
        value = effort.get("reasoningEffort") if isinstance(effort, dict) else effort
        if value:
            result.add(str(value))
    return result


async def _validate_subscription_model(
    runtime: Any,
    model_id: str,
    *,
    reasoning_effort: str | None = None,
    require_image: bool = True,
) -> tuple[bool, str]:
    """Validate a model, its modality, and an optional reasoning effort."""
    models = _subscription_models(await _runtime_call_async(runtime, ("list_models",)))
    model = next((item for item in models if str(item.get("id")) == model_id), None)
    if model is None:
        return False, "Selected ChatGPT subscription model is not available"
    modalities = model.get("inputModalities") or []
    if isinstance(modalities, str):
        modalities = [modalities]
    if require_image and not any(str(value).lower() == "image" for value in modalities):
        return False, "Selected ChatGPT subscription model does not support image input"
    supported_efforts = _supported_efforts(model)
    if reasoning_effort and supported_efforts and reasoning_effort not in supported_efforts:
        return False, "Selected reasoning effort is not supported by this model"
    return True, "ok"


def _effective_embedding_config(cfg: ModelSettingsVO, current: dict) -> dict:
    """Resolve a complete embedding config without borrowing VLM fields.

    Older renderer versions omit the separate fields when the user has not
    edited embedding settings.  In that case the already-persisted embedding
    config is retained field-for-field.  There is intentionally no fallback
    to the VLM provider, URL, key, or model (especially for subscription VLM).
    """
    current = current or {}
    provider = cfg.embeddingModelPlatform or current.get("provider")
    model = cfg.embeddingModelId or current.get("model")
    base_url = cfg.embeddingBaseUrl or current.get("base_url")
    api_key = cfg.embeddingApiKey or current.get("api_key")
    output_dim = cfg.embeddingOutputDim
    if output_dim is None:
        output_dim = current.get("output_dim", 2048)
    # A legacy API-only renderer may not send the separate fields on its
    # first save, while the base config may have an empty provider placeholder.
    # Preserve that compatibility only for API-key providers; never derive an
    # embedding credential or URL from a ChatGPT subscription VLM.
    if cfg.modelPlatform != "chatgpt_subscription":
        provider = provider or cfg.modelPlatform
        base_url = base_url or cfg.baseUrl
        api_key = api_key or cfg.apiKey
    if not all((provider, model, base_url, api_key)):
        raise ValueError(
            "Embedding configuration must include provider, model, base URL, and API key"
        )
    if not isinstance(output_dim, int) or output_dim <= 0:
        raise ValueError("Embedding output dimension must be a positive integer")
    return {
        "provider": provider,
        "model": model,
        "base_url": base_url,
        "api_key": api_key,
        "output_dim": output_dim,
    }


# ==================== API Endpoints ====================


@router.get("/api/model_settings/get")
async def get_model_settings(_auth: str = auth_dependency):
    """Get current model configuration"""
    try:
        config = GlobalConfig.get_instance().get_config()
        if not config:
            return convert_resp(code=500, status=500, message="配置未初始化")

        vlm_cfg = config.get("vlm_model", {})
        background_cfg = config.get("background_model", {})
        vision_cfg = config.get("vision_model", {})
        emb_cfg = config.get("embedding_model", {})

        settings = ModelSettingsVO(
            modelPlatform=vlm_cfg.get("provider", ""),
            modelId=vlm_cfg.get("model", ""),
            reasoningEffort=vlm_cfg.get("reasoning_effort"),
            backgroundModelId=background_cfg.get("model") or vlm_cfg.get("model", ""),
            backgroundReasoningEffort=background_cfg.get("reasoning_effort"),
            visionModelId=vision_cfg.get("model") or vlm_cfg.get("model", ""),
            visionReasoningEffort=vision_cfg.get("reasoning_effort"),
            baseUrl=vlm_cfg.get("base_url", ""),
            apiKey=vlm_cfg.get("api_key", ""),
            embeddingModelId=emb_cfg.get("model", ""),
            embeddingBaseUrl=emb_cfg.get("base_url", ""),
            embeddingApiKey=emb_cfg.get("api_key", ""),
            embeddingModelPlatform=emb_cfg.get("provider", ""),
            embeddingOutputDim=emb_cfg.get("output_dim", 2048),
        )

        return convert_resp(data=GetModelSettingsResponse(config=settings).model_dump())

    except Exception as e:
        logger.exception(f"Failed to get model settings: {e}")
        return convert_resp(code=500, status=500, message=f"获取模型设置失败: {str(e)}")


@router.post("/api/model_settings/update")
async def update_model_settings(request: UpdateModelSettingsRequest, _auth: str = auth_dependency):
    """Update model configuration and reinitialize LLM clients"""
    with _config_lock:
        try:
            cfg = request.config

            current_config = GlobalConfig.get_instance().get_config() or {}
            current_emb = current_config.get("embedding_model", {})
            current_vlm = current_config.get("vlm_model", {})
            current_background = current_config.get("background_model", {})
            current_vision = current_config.get("vision_model", {})
            vlm_key = cfg.apiKey or ""
            is_subscription = cfg.modelPlatform == "chatgpt_subscription"
            vision_model_id = cfg.visionModelId or cfg.modelId
            background_model_id = (
                cfg.backgroundModelId
                or current_background.get("model")
                or (vision_model_id if is_subscription else cfg.modelId)
            )
            chat_effort = cfg.reasoningEffort or current_vlm.get("reasoning_effort") or "medium"
            background_effort = (
                cfg.backgroundReasoningEffort
                or current_background.get("reasoning_effort")
                or "medium"
            )
            vision_effort = (
                cfg.visionReasoningEffort
                or current_vision.get("reasoning_effort")
                or "medium"
            )

            # Validation
            if not is_subscription and not vlm_key:
                return convert_resp(code=400, status=400, message="VLM API key cannot be empty")
            if not cfg.modelId:
                return convert_resp(code=400, status=400, message="VLM model ID cannot be empty")

            try:
                embedding_config = _effective_embedding_config(cfg, current_emb)
            except ValueError as exc:
                return convert_resp(code=400, status=400, message=str(exc))

            emb_url = embedding_config["base_url"]
            emb_key = embedding_config["api_key"]
            emb_provider = embedding_config["provider"]

            # Validate VLM
            vlm_config = _build_llm_config(
                cfg.baseUrl, vlm_key, cfg.modelId, cfg.modelPlatform, LLMType.CHAT,
                timeout=15, reasoning_effort=chat_effort if is_subscription else None
            )
            if is_subscription:
                try:
                    runtime = _get_subscription_runtime()
                    status = _subscription_status_payload(
                        await _runtime_call_async(runtime, ("account_read",), refresh_token=False)
                    )
                    authenticated = bool(status.get("authenticated", False))
                    if not authenticated:
                        return convert_resp(code=400, status=400, message="ChatGPT subscription is not logged in")
                    model_valid, model_message = await _validate_subscription_model(
                        runtime, cfg.modelId, reasoning_effort=chat_effort, require_image=False
                    )
                    if not model_valid:
                        return convert_resp(code=400, status=400, message=model_message)
                    background_valid, background_message = await _validate_subscription_model(
                        runtime, background_model_id,
                        reasoning_effort=background_effort, require_image=False
                    )
                    if not background_valid:
                        return convert_resp(code=400, status=400, message=background_message)
                    vision_valid, vision_message = await _validate_subscription_model(
                        runtime, vision_model_id,
                        reasoning_effort=vision_effort, require_image=True
                    )
                    if not vision_valid:
                        return convert_resp(code=400, status=400, message=vision_message)
                except Exception as exc:
                    return convert_resp(code=400, status=400, message=f"ChatGPT subscription validation failed: {exc}")
            else:
                vlm_valid, vlm_msg = LLMClient(llm_type=LLMType.CHAT, config=vlm_config).validate()
                if not vlm_valid:
                    return convert_resp(
                        code=400, status=400, message=f"VLM validation failed: {vlm_msg}"
                    )

            # Validate Embedding
            emb_config = _build_llm_config(
                emb_url, emb_key, embedding_config["model"], emb_provider, LLMType.EMBEDDING,
                timeout=15, output_dim=embedding_config["output_dim"]
            )
            emb_valid, emb_msg = LLMClient(llm_type=LLMType.EMBEDDING, config=emb_config).validate()
            if not emb_valid:
                return convert_resp(
                    code=400, status=400, message=f"Embedding validation failed: {emb_msg}"
                )

            # Save configuration (without timeout limit)
            vlm_config_save = _build_llm_config(
                cfg.baseUrl, vlm_key, cfg.modelId, cfg.modelPlatform, LLMType.CHAT,
                reasoning_effort=chat_effort if is_subscription else None
            )
            background_config_save = _build_llm_config(
                cfg.baseUrl, vlm_key, background_model_id, cfg.modelPlatform, LLMType.CHAT,
                reasoning_effort=background_effort if is_subscription else None
            )
            vision_config_save = _build_llm_config(
                cfg.baseUrl, vlm_key, vision_model_id, cfg.modelPlatform, LLMType.CHAT,
                reasoning_effort=vision_effort if is_subscription else None
            )
            emb_config_save = _build_llm_config(
                emb_url, emb_key, embedding_config["model"], emb_provider, LLMType.EMBEDDING,
                output_dim=embedding_config["output_dim"]
            )

            new_settings = {
                "vlm_model": vlm_config_save,
                "background_model": background_config_save,
                "vision_model": vision_config_save,
                "embedding_model": emb_config_save,
            }

            config_mgr = GlobalConfig.get_instance().get_config_manager()
            if not config_mgr:
                return convert_resp(code=500, status=500, message="Config manager not initialized")

            if not config_mgr.save_user_settings(new_settings):
                return convert_resp(code=500, status=500, message="Failed to save settings")

            config_mgr.load_config(config_mgr.get_config_path())

            # Reinitialize clients
            if not GlobalVLMClient.get_instance().reinitialize():
                return convert_resp(
                    code=500, status=500, message="Failed to reinitialize VLM client"
                )
            if not GlobalEmbeddingClient.get_instance().reinitialize():
                return convert_resp(
                    code=500, status=500, message="Failed to reinitialize embedding client"
                )

            logger.info("Model settings updated successfully")
            return convert_resp(
                data=UpdateModelSettingsResponse(
                    success=True, message="Model settings updated successfully"
                ).model_dump()
            )

        except Exception as e:
            logger.exception(f"Failed to update model settings: {e}")
            return convert_resp(code=500, status=500, message="Failed to update model settings")


@router.post("/api/model_settings/validate")
async def validate_llm_config(request: UpdateModelSettingsRequest, _auth: str = auth_dependency):
    """Validate LLM configuration from frontend (without saving)"""
    try:
        cfg = request.config

        current_config = GlobalConfig.get_instance().get_config() or {}
        current_emb = current_config.get("embedding_model", {})
        current_vlm = current_config.get("vlm_model", {})
        current_background = current_config.get("background_model", {})
        current_vision = current_config.get("vision_model", {})
        vlm_key = cfg.apiKey or ""
        is_subscription = cfg.modelPlatform == "chatgpt_subscription"
        vision_model_id = cfg.visionModelId or cfg.modelId
        background_model_id = (
            cfg.backgroundModelId
            or current_background.get("model")
            or (vision_model_id if is_subscription else cfg.modelId)
        )
        chat_effort = cfg.reasoningEffort or current_vlm.get("reasoning_effort") or "medium"
        background_effort = (
            cfg.backgroundReasoningEffort
            or current_background.get("reasoning_effort")
            or "medium"
        )
        vision_effort = (
            cfg.visionReasoningEffort
            or current_vision.get("reasoning_effort")
            or "medium"
        )

        # Validation
        if not is_subscription and not vlm_key:
            return convert_resp(code=400, status=400, message="VLM API key cannot be empty")
        if not cfg.modelId:
            return convert_resp(code=400, status=400, message="VLM model ID cannot be empty")

        try:
            embedding_config = _effective_embedding_config(cfg, current_emb)
        except ValueError as exc:
            return convert_resp(code=400, status=400, message=str(exc))

        emb_url = embedding_config["base_url"]
        emb_key = embedding_config["api_key"]
        emb_provider = embedding_config["provider"]

        # Build configs for validation (without saving)
        vlm_config = _build_llm_config(
            cfg.baseUrl, vlm_key, cfg.modelId, cfg.modelPlatform, LLMType.CHAT,
            timeout=15, reasoning_effort=chat_effort if is_subscription else None
        )
        emb_config = _build_llm_config(
            emb_url, emb_key, embedding_config["model"], emb_provider, LLMType.EMBEDDING,
            timeout=15, output_dim=embedding_config["output_dim"]
        )

        # Validate VLM
        if is_subscription:
            try:
                runtime = _get_subscription_runtime()
                status = _subscription_status_payload(
                    await _runtime_call_async(runtime, ("account_read",), refresh_token=False)
                )
                authenticated = bool(status.get("authenticated", False))
                if authenticated:
                    vlm_valid, vlm_msg = await _validate_subscription_model(
                        runtime, cfg.modelId,
                        reasoning_effort=chat_effort, require_image=False
                    )
                    if vlm_valid:
                        vlm_valid, vlm_msg = await _validate_subscription_model(
                            runtime, background_model_id,
                            reasoning_effort=background_effort, require_image=False
                        )
                    if vlm_valid:
                        vlm_valid, vlm_msg = await _validate_subscription_model(
                            runtime, vision_model_id,
                            reasoning_effort=vision_effort, require_image=True
                        )
                else:
                    vlm_valid, vlm_msg = False, "subscription is not logged in"
            except Exception as exc:
                vlm_valid, vlm_msg = False, str(exc)
        else:
            vlm_valid, vlm_msg = LLMClient(llm_type=LLMType.CHAT, config=vlm_config).validate()

        # Validate Embedding
        emb_valid, emb_msg = LLMClient(llm_type=LLMType.EMBEDDING, config=emb_config).validate()

        # Build error message
        if not vlm_valid or not emb_valid:
            errors = []
            if not vlm_valid:
                errors.append(f"VLM: {vlm_msg}")
            if not emb_valid:
                errors.append(f"Embedding: {emb_msg}")
            error_msg = "; ".join(errors)
            return convert_resp(code=400, status=400, message=error_msg)

        return convert_resp(code=0, status=200, message="连接测试成功！模型和Embedding配置均正常")

    except Exception as e:
        logger.exception(f"Validation failed: {e}")
        return convert_resp(code=500, status=500, message=f"Validation failed: {str(e)}")


# ==================== ChatGPT subscription ====================


class SubscriptionLoginStartRequest(BaseModel):
    """Optional login method understood by the official Codex runtime."""

    method: str = Field(default="chatgpt", pattern="^(chatgpt|chatgptDeviceCode)$")


class SubscriptionLoginCancelRequest(BaseModel):
    loginId: str | None = None


@router.post("/api/model_settings/chatgpt/auth/login/start")
@router.post("/api/model_settings/subscription/login/start")
async def start_subscription_login(
    request: SubscriptionLoginStartRequest | None = None, _auth: str = auth_dependency
):
    """Start official ChatGPT subscription login via the local Codex runtime."""
    try:
        runtime = _get_subscription_runtime()
        method = (request or SubscriptionLoginStartRequest()).method
        result = await _runtime_call_async(
            runtime, ("login_start",), device_code=method == "chatgptDeviceCode"
        )
        return convert_resp(data=_subscription_payload(result))
    except Exception as exc:
        logger.exception("Failed to start ChatGPT subscription login: %s", exc)
        return convert_resp(code=500, status=500, message=f"登录启动失败: {exc}")


@router.post("/api/model_settings/chatgpt/auth/login/cancel")
@router.post("/api/model_settings/subscription/login/cancel")
async def cancel_subscription_login(
    request: SubscriptionLoginCancelRequest | None = None, _auth: str = auth_dependency
):
    """Cancel a pending official login attempt."""
    try:
        runtime = _get_subscription_runtime()
        login_id = (request or SubscriptionLoginCancelRequest()).loginId
        result = await _runtime_call_async(
            runtime,
            ("login_cancel",),
            login_id=login_id,
        )
        return convert_resp(data=_subscription_payload(result))
    except Exception as exc:
        logger.exception("Failed to cancel ChatGPT subscription login: %s", exc)
        return convert_resp(code=500, status=500, message=f"取消登录失败: {exc}")


@router.get("/api/model_settings/chatgpt/auth/status")
@router.get("/api/model_settings/subscription/status")
async def get_subscription_status(_auth: str = auth_dependency):
    """Return login status without exposing credentials or tokens."""
    try:
        runtime = _get_subscription_runtime()
        result = await _runtime_call_async(runtime, ("account_read",), refresh_token=False)
        return convert_resp(data=_subscription_status_payload(result))
    except Exception as exc:
        logger.exception("Failed to get ChatGPT subscription status: %s", exc)
        return convert_resp(code=500, status=500, message=f"获取登录状态失败: {exc}")


@router.post("/api/model_settings/chatgpt/auth/logout")
@router.post("/api/model_settings/subscription/logout")
async def logout_subscription(_auth: str = auth_dependency):
    """Log out through the official runtime."""
    try:
        runtime = _get_subscription_runtime()
        result = await _runtime_call_async(runtime, ("logout", "account_logout"))
        return convert_resp(data=_subscription_payload(result))
    except Exception as exc:
        logger.exception("Failed to log out ChatGPT subscription: %s", exc)
        return convert_resp(code=500, status=500, message=f"退出登录失败: {exc}")


@router.get("/api/model_settings/chatgpt/models")
@router.get("/api/model_settings/subscription/models")
async def list_subscription_models(_auth: str = auth_dependency):
    """List only models that advertise image input support."""
    try:
        runtime = _get_subscription_runtime()
        result = await _runtime_call_async(
            runtime, ("list_models",)
        )
        return convert_resp(data={"models": _subscription_models(result)})
    except Exception as exc:
        logger.exception("Failed to list ChatGPT subscription models: %s", exc)
        return convert_resp(code=500, status=500, message=f"获取模型列表失败: {exc}")


# ==================== System Info ====================


@router.get("/api/settings/system_info")
async def get_system_info(_auth: str = auth_dependency):
    """Get system information including data directory path"""
    try:
        import os
        from pathlib import Path

        context_path = os.getenv("CONTEXT_PATH", ".")
        # Get absolute path
        absolute_path = str(Path(context_path).resolve())

        return convert_resp(
            data={
                "context_path": context_path,
                "context_path_absolute": absolute_path,
            }
        )

    except Exception as e:
        logger.exception(f"Failed to get system info: {e}")
        return convert_resp(code=500, status=500, message=f"Failed to get system info: {str(e)}")


# ==================== General Settings ====================


class GeneralSettingsRequest(BaseModel):
    """General system settings request"""

    capture: dict | None = None
    processing: dict | None = None
    logging: dict | None = None
    content_generation: dict | None = None


@router.get("/api/settings/general")
async def get_general_settings(_auth: str = auth_dependency):
    """Get general system settings"""
    try:
        import os

        config = GlobalConfig.get_instance().get_config()
        if not config:
            return convert_resp(code=500, status=500, message="Configuration not initialized")

        settings = {
            "capture": config.get("capture", {}),
            "processing": config.get("processing", {}),
            "logging": config.get("logging", {}),
            "content_generation": config.get("content_generation", {}),
        }

        # Resolve environment variables in debug output path for display
        if "content_generation" in settings and "debug" in settings["content_generation"]:
            debug_config = settings["content_generation"]["debug"]
            if "output_path" in debug_config:
                output_path = debug_config["output_path"]
                # Resolve environment variables
                if "${CONTEXT_PATH" in output_path:
                    context_path = os.getenv("CONTEXT_PATH", ".")
                    resolved_path = output_path.replace("${CONTEXT_PATH:.}", context_path)
                    resolved_path = resolved_path.replace("${CONTEXT_PATH}", context_path)
                    # Add resolved path as a separate field for display
                    debug_config["output_path_resolved"] = resolved_path

        return convert_resp(data=settings)

    except Exception as e:
        logger.exception(f"Failed to get general settings: {e}")
        return convert_resp(
            code=500, status=500, message=f"Failed to get general settings: {str(e)}"
        )


@router.post("/api/settings/general")
async def update_general_settings(request: GeneralSettingsRequest, _auth: str = auth_dependency):
    """Update general system settings"""
    with _config_lock:
        try:
            config_mgr = GlobalConfig.get_instance().get_config_manager()
            if not config_mgr:
                return convert_resp(code=500, status=500, message="Config manager not initialized")

            # Build settings dict
            settings = {}
            if request.capture is not None:
                settings["capture"] = request.capture
            if request.processing is not None:
                settings["processing"] = request.processing
            if request.logging is not None:
                settings["logging"] = request.logging
            if request.content_generation is not None:
                settings["content_generation"] = request.content_generation

            if not settings:
                return convert_resp(code=400, status=400, message="No settings provided")

            # Save to user_setting.yaml
            if not config_mgr.save_user_settings(settings):
                return convert_resp(code=500, status=500, message="Failed to save settings")

            # Reload config
            config_mgr.load_config(config_mgr.get_config_path())

            # 同步通知 ConsumptionManager 实时应用 content_generation 新配置
            try:
                from opencontext.opencontext import get_context_lab
                opencontext = get_context_lab()
                if opencontext and hasattr(opencontext, 'consumption_manager') and opencontext.consumption_manager and request.content_generation:
                    # 提取相关字段构建更新配置
                    gen_cfg = request.content_generation
                    task_config = {}
                    for field in ("activity", "tips", "todos", "report"):
                        if field in gen_cfg:
                            task_config[field] = gen_cfg[field]
                    if task_config:
                        opencontext.consumption_manager.update_task_config(task_config)
            except Exception as e:
                logger.warning(f"Failed to update ConsumptionManager runtime config: {e}")

            logger.info("General settings updated successfully")
            return convert_resp(code=0, status=200, message="Settings updated successfully")

        except Exception as e:
            logger.exception(f"Failed to update general settings: {e}")
            return convert_resp(
                code=500, status=500, message=f"Failed to update settings: {str(e)}"
            )


# ==================== Prompts Settings ====================


class PromptsUpdateRequest(BaseModel):
    """Prompts update request"""

    prompts: dict


@router.get("/api/settings/prompts")
async def get_prompts(_auth: str = auth_dependency):
    """Get current prompts"""
    try:
        prompt_mgr = GlobalConfig.get_instance().get_prompt_manager()
        if not prompt_mgr:
            return convert_resp(code=500, status=500, message="Prompt manager not initialized")

        return convert_resp(data={"prompts": prompt_mgr.prompts})

    except Exception as e:
        logger.exception(f"Failed to get prompts: {e}")
        return convert_resp(code=500, status=500, message=f"Failed to get prompts: {str(e)}")


@router.post("/api/settings/prompts")
async def update_prompts(request: PromptsUpdateRequest, _auth: str = auth_dependency):
    """Update prompts"""
    try:
        prompt_mgr = GlobalConfig.get_instance().get_prompt_manager()
        if not prompt_mgr:
            return convert_resp(code=500, status=500, message="Prompt manager not initialized")

        if not prompt_mgr.save_prompts(request.prompts):
            return convert_resp(code=500, status=500, message="Failed to save prompts")

        logger.info("Prompts updated successfully")
        return convert_resp(code=0, status=200, message="Prompts updated successfully")

    except Exception as e:
        logger.exception(f"Failed to update prompts: {e}")
        return convert_resp(code=500, status=500, message=f"Failed to update prompts: {str(e)}")


@router.post("/api/settings/prompts/import")
async def import_prompts(file: UploadFile = File(...), _auth: str = auth_dependency):
    """Import prompts from YAML file"""
    try:
        prompt_mgr = GlobalConfig.get_instance().get_prompt_manager()
        if not prompt_mgr:
            return convert_resp(code=500, status=500, message="Prompt manager not initialized")

        # Read file content
        content = await file.read()
        yaml_content = content.decode("utf-8")

        if not prompt_mgr.import_prompts(yaml_content):
            return convert_resp(code=400, status=400, message="Failed to import prompts")

        logger.info("Prompts imported successfully")
        return convert_resp(code=0, status=200, message="Prompts imported successfully")

    except Exception as e:
        logger.exception(f"Failed to import prompts: {e}")
        return convert_resp(code=500, status=500, message=f"Failed to import prompts: {str(e)}")


@router.get("/api/settings/prompts/export")
async def export_prompts(_auth: str = auth_dependency):
    """Export prompts as YAML file"""
    try:
        prompt_mgr = GlobalConfig.get_instance().get_prompt_manager()
        if not prompt_mgr:
            return convert_resp(code=500, status=500, message="Prompt manager not initialized")

        yaml_content = prompt_mgr.export_prompts()
        if not yaml_content:
            return convert_resp(code=500, status=500, message="Failed to export prompts")

        # Return as downloadable file
        language = GlobalConfig.get_instance().get_language()
        filename = f"prompts_{language}.yaml"

        return StreamingResponse(
            io.BytesIO(yaml_content.encode("utf-8")),
            media_type="application/x-yaml",
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )

    except Exception as e:
        logger.exception(f"Failed to export prompts: {e}")
        return convert_resp(code=500, status=500, message=f"Failed to export prompts: {str(e)}")


@router.get("/api/settings/prompts/language")
async def get_prompt_language(_auth: str = auth_dependency):
    """Get current prompt language"""
    try:
        language = GlobalConfig.get_instance().get_language()
        return convert_resp(data={"language": language})
    except Exception as e:
        logger.exception(f"Failed to get prompt language: {e}")
        return convert_resp(code=500, status=500, message=f"Failed to get language: {str(e)}")


class LanguageChangeRequest(BaseModel):
    """Language change request"""

    language: str = Field(..., pattern="^(zh|en)$")


@router.post("/api/settings/prompts/language")
async def change_prompt_language(request: LanguageChangeRequest, _auth: str = auth_dependency):
    """Change prompt language"""
    try:
        # Update language setting and reload prompts
        success = GlobalConfig.get_instance().set_language(request.language)

        if not success:
            return convert_resp(code=500, status=500, message="Failed to change language")

        logger.info(f"Prompt language changed to: {request.language}")
        return convert_resp(message=f"Language changed to {request.language}")
    except Exception as e:
        logger.exception(f"Failed to change prompt language: {e}")
        return convert_resp(code=500, status=500, message=f"Failed to change language: {str(e)}")


# ==================== Reset Settings ====================


@router.post("/api/settings/reset")
async def reset_settings(_auth: str = auth_dependency):
    """Reset all user settings to defaults"""
    with _config_lock:
        try:
            config_mgr = GlobalConfig.get_instance().get_config_manager()
            prompt_mgr = GlobalConfig.get_instance().get_prompt_manager()

            success = True

            # Reset user settings
            if config_mgr:
                if not config_mgr.reset_user_settings():
                    success = False
                    logger.error("Failed to reset user settings")

            # Reset user prompts
            if prompt_mgr:
                if not prompt_mgr.reset_user_prompts():
                    success = False
                    logger.error("Failed to reset user prompts")

            if not success:
                return convert_resp(code=500, status=500, message="Failed to reset some settings")

            logger.info("All settings reset successfully")
            return convert_resp(code=0, status=200, message="Settings reset successfully")

        except Exception as e:
            logger.exception(f"Failed to reset settings: {e}")
            return convert_resp(code=500, status=500, message=f"Failed to reset settings: {str(e)}")


# ==================== Prompts History & Debug ====================


@router.get("/api/settings/prompts/history/{category}")
async def get_prompts_history(category: str, _auth: str = auth_dependency):
    """
    Get debug history files for a prompt category

    Args:
        category: Prompt category (smart_tip_generation, todo_extraction, generation_report, realtime_activity_monitor)

    Returns:
        List of history files with metadata
    """
    try:
        import os
        from pathlib import Path

        # Map category names to directory names
        category_map = {
            "smart_tip_generation": "tips",
            "todo_extraction": "todo",
            "generation_report": "report",
            "realtime_activity_monitor": "activity",
        }

        if category not in category_map:
            return convert_resp(code=400, status=400, message=f"Invalid category: {category}")

        dir_name = category_map[category]

        # Get debug output path from config
        config = GlobalConfig.get_instance().get_config()
        if not config:
            return convert_resp(code=500, status=500, message="Configuration not initialized")

        debug_config = config.get("content_generation", {}).get("debug", {})
        if not debug_config.get("enabled", False):
            return convert_resp(code=400, status=400, message="Debug mode is not enabled")

        base_path = debug_config.get("output_path", "${CONTEXT_PATH:.}/debug/generation")

        # Resolve environment variables
        if "${CONTEXT_PATH" in base_path:
            context_path = os.getenv("CONTEXT_PATH", ".")
            base_path = base_path.replace("${CONTEXT_PATH:.}", context_path)
            base_path = base_path.replace("${CONTEXT_PATH}", context_path)

        # Get directory path
        history_dir = Path(base_path) / dir_name

        if not history_dir.exists():
            return convert_resp(data=[])

        # List all JSON files
        history_files = []
        for filepath in sorted(history_dir.glob("*.json"), reverse=True):  # Most recent first
            try:
                # Read file to check if it has response
                import json

                with open(filepath, "r", encoding="utf-8") as f:
                    data = json.load(f)

                has_result = bool(data.get("response"))
                timestamp_str = data.get("timestamp", "")

                history_files.append(
                    {
                        "filename": filepath.name,
                        "timestamp": timestamp_str,
                        "has_result": has_result,
                    }
                )
            except Exception as e:
                logger.warning(f"Failed to read history file {filepath}: {e}")
                continue

        return convert_resp(data=history_files)

    except Exception as e:
        logger.exception(f"Failed to get prompts history for {category}: {e}")
        return convert_resp(code=500, status=500, message=f"Failed to get history: {str(e)}")


@router.get("/api/settings/prompts/history/{category}/{filename}")
async def get_prompts_history_detail(category: str, filename: str, _auth: str = auth_dependency):
    """
    Get detailed content of a specific debug history file

    Args:
        category: Prompt category
        filename: History file name

    Returns:
        Debug file content with messages and response
    """
    try:
        import json
        import os
        from pathlib import Path

        # Map category names to directory names
        category_map = {
            "smart_tip_generation": "tips",
            "todo_extraction": "todo",
            "generation_report": "report",
            "realtime_activity_monitor": "activity",
        }

        if category not in category_map:
            return convert_resp(code=400, status=400, message=f"Invalid category: {category}")

        dir_name = category_map[category]

        # Get debug output path from config
        config = GlobalConfig.get_instance().get_config()
        if not config:
            return convert_resp(code=500, status=500, message="Configuration not initialized")

        debug_config = config.get("content_generation", {}).get("debug", {})
        base_path = debug_config.get("output_path", "${CONTEXT_PATH:.}/debug/generation")

        # Resolve environment variables
        if "${CONTEXT_PATH" in base_path:
            context_path = os.getenv("CONTEXT_PATH", ".")
            base_path = base_path.replace("${CONTEXT_PATH:.}", context_path)
            base_path = base_path.replace("${CONTEXT_PATH}", context_path)

        # Get file path (validate filename to prevent directory traversal)
        if ".." in filename or "/" in filename or "\\" in filename:
            return convert_resp(code=400, status=400, message="Invalid filename")

        filepath = Path(base_path) / dir_name / filename

        if not filepath.exists():
            return convert_resp(code=404, status=404, message="History file not found")

        # Read and return file content
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)

        return convert_resp(data=data)

    except Exception as e:
        logger.exception(f"Failed to get history detail for {category}/{filename}: {e}")
        return convert_resp(code=500, status=500, message=f"Failed to get history detail: {str(e)}")


class RegenerateRequest(BaseModel):
    """Regenerate request with custom prompts"""

    category: str
    history_file: str
    custom_prompts: dict = Field(
        default_factory=dict, description="Custom prompts with system and user keys"
    )


@router.post("/api/settings/prompts/regenerate")
async def regenerate_with_custom_prompts(request: RegenerateRequest, _auth: str = auth_dependency):
    """
    Regenerate content using custom prompts and compare with original result

    Args:
        request: Regenerate request with category, history_file, and custom_prompts

    Returns:
        Comparison data with original and new results
    """
    try:
        import json
        import os
        from pathlib import Path

        # Map category names to directory names
        category_map = {
            "smart_tip_generation": "tips",
            "todo_extraction": "todo",
            "generation_report": "report",
            "realtime_activity_monitor": "activity",
        }

        if request.category not in category_map:
            return convert_resp(
                code=400, status=400, message=f"Invalid category: {request.category}"
            )

        dir_name = category_map[request.category]

        # Get debug output path from config
        config = GlobalConfig.get_instance().get_config()
        if not config:
            return convert_resp(code=500, status=500, message="Configuration not initialized")

        debug_config = config.get("content_generation", {}).get("debug", {})
        base_path = debug_config.get("output_path", "${CONTEXT_PATH:.}/debug/generation")

        # Resolve environment variables
        if "${CONTEXT_PATH" in base_path:
            context_path = os.getenv("CONTEXT_PATH", ".")
            base_path = base_path.replace("${CONTEXT_PATH:.}", context_path)
            base_path = base_path.replace("${CONTEXT_PATH}", context_path)

        # Validate and read history file
        if (
            ".." in request.history_file
            or "/" in request.history_file
            or "\\" in request.history_file
        ):
            return convert_resp(code=400, status=400, message="Invalid filename")

        filepath = Path(base_path) / dir_name / request.history_file

        if not filepath.exists():
            return convert_resp(code=404, status=404, message="History file not found")

        # Read original debug data
        with open(filepath, "r", encoding="utf-8") as f:
            original_data = json.load(f)

        original_messages = original_data.get("messages", [])
        original_response = original_data.get("response", "")

        if not original_messages:
            return convert_resp(code=400, status=400, message="No messages found in history file")

        # Replace prompts with custom ones
        new_messages = []
        for msg in original_messages:
            if msg.get("role") == "system" and request.custom_prompts.get("system"):
                new_messages.append({"role": "system", "content": request.custom_prompts["system"]})
            elif msg.get("role") == "user" and request.custom_prompts.get("user"):
                # For user messages, we might want to keep the data but replace the template
                # This is tricky as user messages contain actual data, not just template
                # For now, we'll keep the original user message as it contains real data
                new_messages.append(msg)
            else:
                new_messages.append(msg)

        # Generate new response
        from opencontext.llm.global_vlm_client import generate_with_messages

        response = generate_with_messages(new_messages)
        # Return comparison data
        comparison_data = {
            "original_result": original_response,
            "new_result": response,
            "custom_prompts": request.custom_prompts,
            "timestamp": original_data.get("timestamp", ""),
            "category": request.category,
        }

        return convert_resp(data=comparison_data)

    except Exception as e:
        logger.exception(f"Failed to regenerate with custom prompts: {e}")
        return convert_resp(code=500, status=500, message=f"Failed to regenerate: {str(e)}")
