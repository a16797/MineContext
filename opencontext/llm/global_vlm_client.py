#!/usr/bin/env python
# -*- coding: utf-8 -*-

# Copyright (c) 2025 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Global LLM manager singleton wrapper
Provides global access to LLMManager instances
"""

import asyncio
import concurrent.futures
import inspect
import json
import threading
from typing import Any, Dict, Optional

from opencontext.config.global_config import get_config
from opencontext.llm.llm_client import LLMClient, LLMType
from opencontext.utils.json_parser import parse_json_from_response
from opencontext.utils.logging_utils import get_logger

logger = get_logger(__name__)

CHATGPT_SUBSCRIPTION_PROVIDER = "chatgpt_subscription"


def create_vlm_client(config: Dict[str, Any]):
    """Create the configured chat client without affecting the embedding client."""
    if config.get("provider") == CHATGPT_SUBSCRIPTION_PROVIDER:
        from opencontext.llm.codex_subscription_client import CodexSubscriptionClient

        return CodexSubscriptionClient(config=config)
    return LLMClient(llm_type=LLMType.CHAT, config=config)


def _messages_contain_image(messages: list) -> bool:
    """Return whether an OpenAI-style message list contains image input."""
    for message in messages:
        content = message.get("content") if isinstance(message, dict) else getattr(message, "content", None)
        if not isinstance(content, list):
            continue
        for part in content:
            if isinstance(part, dict) and part.get("type") in {"image", "input_image", "image_url"}:
                return True
    return False


class GlobalVLMClient:
    """
    Global LLM manager (singleton pattern)
    """

    _instance = None
    _lock = threading.Lock()
    _initialized = False

    def __new__(cls):
        """Ensure singleton pattern"""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        """Initialize VLM client"""
        if not self._initialized:
            with self._lock:
                if not self._initialized:
                    # Backwards-compatible alias: _vlm_client points to the
                    # background client after purpose routing is initialized.
                    self._vlm_client: Optional[Any] = None
                    self._chat_client: Optional[Any] = None
                    self._background_client: Optional[Any] = None
                    self._vision_client: Optional[Any] = None
                    self._auto_initialized = False
                    GlobalVLMClient._initialized = True

    @classmethod
    def get_instance(cls) -> "GlobalVLMClient":
        """
        Get global LLM manager instance
        """
        instance = cls()
        if not instance._auto_initialized and instance._vlm_client is None:
            instance._auto_initialize()
        return instance

    @classmethod
    def reset(cls):
        """Reset singleton instance (mainly for testing)"""
        with cls._lock:
            instance = cls._instance
            cls._instance = None
            cls._initialized = False
            if instance is not None:
                cls._close_clients(
                    getattr(instance, "_vlm_client", None),
                    getattr(instance, "_chat_client", None),
                    getattr(instance, "_background_client", None),
                    getattr(instance, "_vision_client", None),
                )

    def _auto_initialize(self):
        """Auto-initialize VLM client"""
        if self._auto_initialized:
            return
        from opencontext.tools.tools_executor import ToolsExecutor

        self._tools_executor = ToolsExecutor()
        try:
            chat_config = get_config("chat_model") or get_config("vlm_model")
            background_config = get_config("background_model") or get_config("vlm_model")
            if not chat_config and not background_config:
                logger.warning("No VLM config found in chat_model/background_model/vlm_model")
                self._auto_initialized = True
                return

            chat_config = chat_config or background_config
            background_config = background_config or chat_config
            vision_config = get_config("vision_model") or background_config
            self._chat_client, self._background_client, self._vision_client = self._create_clients(
                chat_config, background_config, vision_config
            )
            self._vlm_client = self._background_client
            logger.info("GlobalVLMClient auto-initialized successfully")
            self._auto_initialized = True
        except Exception as e:
            logger.error(f"GlobalVLMClient auto-initialization failed: {e}")
            self._auto_initialized = True

    def is_initialized(self) -> bool:
        return self._vlm_client is not None

    def reinitialize(self):
        """
        Thread-safe reinitialization of VLM client
        """
        with self._lock:
            try:
                chat_config = get_config("chat_model") or get_config("vlm_model")
                background_config = get_config("background_model") or get_config("vlm_model")
                if not chat_config and not background_config:
                    logger.error("No VLM config found during reinitialize")
                    raise ValueError("No vlm config found")
                chat_config = chat_config or background_config
                background_config = background_config or chat_config
                vision_config = get_config("vision_model") or background_config
                new_chat_client, new_background_client, new_vision_client = self._create_clients(
                    chat_config, background_config, vision_config
                )
                old_client = self._vlm_client
                old_chat_client = self._chat_client
                old_background_client = self._background_client
                old_vision_client = self._vision_client
                self._chat_client = new_chat_client
                self._background_client = new_background_client
                self._vlm_client = new_background_client
                self._vision_client = new_vision_client
                self._close_clients(old_client, old_chat_client, old_background_client, old_vision_client)
                logger.info("GlobalVLMClient reinitialized successfully")

            except Exception as e:
                logger.error(f"Failed to reinitialize VLM client: {e}")
                return False
            return True

    @staticmethod
    def _create_clients(
        chat_config: Dict[str, Any],
        background_config: Dict[str, Any],
        vision_config: Dict[str, Any],
    ) -> tuple[Any, Any, Any]:
        """Create purpose clients atomically and reuse equal configurations."""
        created: list[Any] = []
        try:
            chat_client = create_vlm_client(chat_config)
            created.append(chat_client)
            background_client = (
                chat_client
                if background_config == chat_config
                else create_vlm_client(background_config)
            )
            if background_client is not chat_client:
                created.append(background_client)
            if vision_config == background_config:
                vision_client = background_client
            elif vision_config == chat_config:
                vision_client = chat_client
            else:
                vision_client = create_vlm_client(vision_config)
                created.append(vision_client)
            return chat_client, background_client, vision_client
        except Exception:
            GlobalVLMClient._close_clients(*created)
            raise

    @staticmethod
    def _close_client(client: Optional[Any]):
        if client is None:
            return
        close_methods = []
        close = getattr(client, "close", None)
        if callable(close):
            close_methods.append(close)
        else:
            # The regular API client predates the shared lifecycle hook.
            sync_client = getattr(client, "client", None)
            async_client = getattr(client, "async_client", None)
            for nested_client in (sync_client, async_client):
                nested_close = getattr(nested_client, "close", None)
                if callable(nested_close):
                    close_methods.append(nested_close)
        if not close_methods:
            return
        for close_method in close_methods:
            try:
                result = close_method()
                if inspect.isawaitable(result):
                    try:
                        loop = asyncio.get_running_loop()
                    except RuntimeError:
                        asyncio.run(result)
                    else:
                        loop.create_task(result)
            except Exception as exc:
                logger.warning(f"Failed to close previous VLM client: {exc}")

    @classmethod
    def _close_clients(cls, *clients: Optional[Any]):
        seen = set()
        for client in clients:
            if client is None or id(client) in seen:
                continue
            seen.add(id(client))
            cls._close_client(client)

    def _client_for_messages(self, messages: list, purpose: Optional[str] = None):
        if purpose == "chat":
            return self._chat_client or self._vlm_client
        if purpose == "background":
            return self._background_client or self._vlm_client
        if purpose == "vision":
            return self._vision_client or self._background_client or self._vlm_client
        if _messages_contain_image(messages):
            return self._vision_client or self._vlm_client
        return self._background_client or self._vlm_client

    def close(self):
        """Release the current VLM client and its optional local runtime."""
        with self._lock:
            client = self._vlm_client
            chat_client = self._chat_client
            background_client = self._background_client
            vision_client = self._vision_client
            self._vlm_client = None
            self._chat_client = None
            self._background_client = None
            self._vision_client = None
            self._auto_initialized = False
        self._close_clients(client, chat_client, background_client, vision_client)

    def generate_with_messages(
        self, messages: list, enable_executor: bool = True, max_calls: int = 5, **kwargs
    ):
        purpose = kwargs.pop("purpose", None)
        client = self._client_for_messages(messages, purpose)
        response = client.generate_with_messages(messages, **kwargs)
        call_count = 0
        while enable_executor:
            call_count += 1
            if call_count > max_calls:
                logger.warning(
                    f"Reached maximum tool call limit ({max_calls}), stopping further calls"
                )
                messages.append(
                    {
                        "role": "system",
                        "content": f"System notice: Maximum tool call limit ({max_calls}) reached. Cannot execute more tool calls. Please answer the user's question directly without attempting more tool calls.",
                    }
                )
                response = client.generate_with_messages(messages, **kwargs)
                break
            message = response.choices[0].message
            if not message.tool_calls:
                break
            messages.append(message)
            tool_calls = message.tool_calls
            tool_call_info = []
            for tc in tool_calls:
                function_name = tc.function.name
                function_args = parse_json_from_response(tc.function.arguments)
                tool_call_info.append((tc.id, function_name, function_args))
            results = []
            with concurrent.futures.ThreadPoolExecutor() as executor:
                future_to_tool = {
                    executor.submit(self._tools_executor.run, function_name, function_args): (
                        tool_id,
                        function_name,
                    )
                    for tool_id, function_name, function_args in tool_call_info
                }
                for future in concurrent.futures.as_completed(future_to_tool):
                    tool_id, function_name = future_to_tool[future]
                    try:
                        content = future.result()
                        # logger.info(f"Tool call {function_name} successful, result: {content}")
                        results.append((tool_id, function_name, content))
                    except Exception as e:
                        # logger.exception(f"Tool call {function_name} failed: {e}")
                        results.append((tool_id, function_name, "failed"))
            # logger.info(f"Tool call results: {results}")
            for tool_id, function_name, content in results:
                messages.append(
                    {
                        "role": "tool",
                        "name": function_name,
                        "content": json.dumps(content),
                        "tool_call_id": tool_id,
                    }
                )
            response = client.generate_with_messages(messages, **kwargs)

        message = response.choices[0].message
        return message.content

    async def generate_with_messages_async(
        self, messages: list, enable_executor: bool = True, max_calls: int = 5, **kwargs
    ):
        purpose = kwargs.pop("purpose", None)
        client = self._client_for_messages(messages, purpose)
        response = await client.generate_with_messages_async(messages, **kwargs)
        call_count = 0
        while enable_executor:
            call_count += 1
            if call_count > max_calls:
                # logger.warning(f"Reached maximum tool call limit ({max_calls}), stopping further calls")
                messages.append(
                    {
                        "role": "system",
                        "content": f"System notice: Maximum tool call limit ({max_calls}) reached. Cannot execute more tool calls. Please answer the user's question directly without attempting more tool calls.",
                    }
                )
                response = await client.generate_with_messages_async(messages, **kwargs)
                break
            message = response.choices[0].message
            if not message.tool_calls:
                break
            messages.append(message)
            tool_calls = message.tool_calls

            # Collect all async tasks for tool calls
            tasks = []
            tool_call_info = []
            for tc in tool_calls:
                function_name = tc.function.name
                function_args = parse_json_from_response(tc.function.arguments)
                if function_args is not None:
                    tasks.append(self._tools_executor.run_async(function_name, function_args))
                    tool_call_info.append((tc.id, function_name))
                else:
                    logger.error(
                        f"Failed to parse arguments for {function_name}: {tc.function.arguments}"
                    )

            # Execute all tool calls in parallel
            results = await asyncio.gather(*tasks)

            # Collect results and add to message list
            for result, (tool_id, function_name) in zip(results, tool_call_info):
                # logger.info(f"Tool call {function_name} successful, result: {result}")
                messages.append(
                    {
                        "role": "tool",
                        "name": function_name,
                        "content": json.dumps(result),
                        "tool_call_id": tool_id,
                    }
                )

            # Call LLM again
            response = await client.generate_with_messages_async(messages, **kwargs)

        message = response.choices[0].message
        return message.content

    async def generate_for_agent_async(self, messages: list, tools: list = None, **kwargs):
        """
        Agent-specific generation method that returns raw response without auto-executing tool calls

        Args:
            messages: Message list
            tools: Available tool definitions
            **kwargs: Other parameters

        Returns:
            Raw LLM response object, including possible tool_calls
        """
        purpose = kwargs.pop("purpose", None)
        client = self._client_for_messages(messages, purpose)
        response = await client.generate_with_messages_async(
            messages, tools=tools, **kwargs
        )
        return response

    async def generate_stream_for_agent(self, messages: list, tools: list = None, **kwargs):
        """
        Agent-specific streaming generation method
        """
        purpose = kwargs.pop("purpose", None)
        client = self._client_for_messages(messages, purpose)
        async for chunk in client._openai_chat_completion_stream_async(
            messages, tools=tools, **kwargs
        ):
            yield chunk

    async def execute_tool_async(self, tool_call):
        """
        Execute a single tool call independently

        Args:
            tool_call: OpenAI format tool call object

        Returns:
            Tool execution result
        """
        function_name = tool_call.function.name
        function_args = parse_json_from_response(tool_call.function.arguments)

        if function_args is None:
            logger.error(
                f"Failed to parse arguments for {function_name}: {tool_call.function.arguments}"
            )
            return {"error": f"Failed to parse arguments for {function_name}"}

        try:
            result = await self._tools_executor.run_async(function_name, function_args)
            logger.info(f"Tool {function_name} executed successfully")
            return result
        except Exception as e:
            logger.exception(f"Tool {function_name} execution failed: {e}")
            return {"error": str(e)}


def is_initialized() -> bool:
    return GlobalVLMClient.get_instance()._auto_initialized


def generate_with_messages(
    messages: list, enable_executor: bool = True, max_calls: int = 5, **kwargs
):
    return GlobalVLMClient.get_instance().generate_with_messages(
        messages, enable_executor, max_calls, **kwargs
    )


async def generate_with_messages_async(
    messages: list, enable_executor: bool = True, max_calls: int = 5, **kwargs
):
    return await GlobalVLMClient.get_instance().generate_with_messages_async(
        messages, enable_executor, max_calls, **kwargs
    )


async def generate_for_agent_async(messages: list, tools: list = None, **kwargs):
    return await GlobalVLMClient.get_instance().generate_for_agent_async(messages, tools, **kwargs)


async def generate_stream_for_agent(messages: list, tools: list = None, **kwargs):
    async for chunk in GlobalVLMClient.get_instance().generate_stream_for_agent(
        messages, tools, **kwargs
    ):
        yield chunk
