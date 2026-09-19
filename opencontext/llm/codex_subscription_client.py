# -*- coding: utf-8 -*-
"""OpenAI-compatible adapter over the official Codex app-server runtime."""

from __future__ import annotations

import asyncio
import json
import threading
import uuid
from types import SimpleNamespace
from typing import Any, AsyncIterator, Dict, Iterable, List, Mapping, Optional

from .codex_subscription_runtime import (
    CodexSubscriptionError,
    CodexSubscriptionRuntime,
    CodexTurnHandle,
    get_codex_subscription_runtime,
)


def _get(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _json_arguments(arguments: Any) -> str:
    if isinstance(arguments, str):
        parsed = json.loads(arguments)
        if not isinstance(parsed, dict):
            raise ValueError("Tool arguments must be a JSON object")
        return arguments
    if not isinstance(arguments, dict):
        raise ValueError("Tool arguments must be a JSON object")
    return json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))


def _content_parts(content: Any) -> list[Any]:
    if content is None:
        return []
    return content if isinstance(content, list) else [content]


def _text_content(content: Any) -> str:
    chunks: list[str] = []
    for part in _content_parts(content):
        if isinstance(part, str):
            chunks.append(part)
        elif isinstance(part, Mapping):
            if part.get("type") in {"text", "input_text", "output_text"}:
                chunks.append(str(part.get("text", "")))
    return "".join(chunks)


class CodexSubscriptionClient:
    """Drop-in chat client for the existing ``GlobalVLMClient`` call shape.

    The class intentionally implements no embedding methods.  Embeddings stay
    on MineContext's existing API client and are not routed through Codex.
    """

    def __init__(self, config: Mapping[str, Any], runtime: Optional[CodexSubscriptionRuntime] = None):
        self.config = dict(config)
        self.model = self.config.get("model") or self.config.get("model_id")
        if not self.model:
            raise ValueError("ChatGPT subscription model must be selected")
        # App Server calls use ``effort`` on each turn.  Settings persist the
        # project-facing name ``reasoning_effort``; accept the wire name too
        # for compatibility with hand-written configs.  Keep an explicit
        # default so every turn has deterministic model/effort parameters.
        self.reasoning_effort = (
            self.config.get("reasoning_effort")
            or self.config.get("reasoningEffort")
            or self.config.get("effort")
            or "medium"
        )
        self.timeout = float(self.config.get("timeout", 300))
        self.runtime = runtime or get_codex_subscription_runtime(self.config.get("runtime"))
        self._runtime_ref = getattr(self.runtime, "acquire", None)
        if self._runtime_ref:
            self._runtime_ref()
        self._active_lock = threading.Lock()
        self._active: Dict[str, CodexTurnHandle] = {}
        self._cancelled: set[str] = set()
        self._closed = False

    @staticmethod
    def _message_role(message: Any) -> str:
        role = _get(message, "role")
        if role not in {"system", "developer", "user", "assistant", "tool"}:
            raise ValueError(f"Unsupported message role: {role!r}")
        return "system" if role == "developer" else role

    @staticmethod
    def _user_inputs(content: Any) -> list[dict[str, Any]]:
        inputs: list[dict[str, Any]] = []
        for part in _content_parts(content):
            if isinstance(part, str):
                inputs.append({"type": "text", "text": part})
                continue
            if not isinstance(part, Mapping):
                raise ValueError("Message content must be text or a content-part mapping")
            kind = part.get("type")
            if kind in {"text", "input_text", "output_text"}:
                inputs.append({"type": "text", "text": str(part.get("text", ""))})
            elif kind == "image_url":
                image_url = part.get("image_url")
                url = image_url.get("url") if isinstance(image_url, Mapping) else image_url
                if not isinstance(url, str) or not url:
                    raise ValueError("image_url must contain a non-empty data URI")
                inputs.append({"type": "image", "url": url})
            elif kind in {"image", "input_image"}:
                url = part.get("image_url") or part.get("url")
                if not isinstance(url, str) or not url:
                    raise ValueError("image content must contain a URL")
                inputs.append({"type": "image", "url": url})
            else:
                raise ValueError(f"Unsupported content part: {kind!r}")
        return inputs

    @staticmethod
    def _response_item(message: Any) -> list[dict[str, Any]]:
        role = _get(message, "role")
        content = _get(message, "content")
        items: list[dict[str, Any]] = []
        if role in {"user", "assistant"} and content:
            parts: list[dict[str, Any]] = []
            for part in _content_parts(content):
                if isinstance(part, str):
                    parts.append({"type": "input_text" if role == "user" else "output_text", "text": part})
                elif isinstance(part, Mapping) and part.get("type") in {"text", "input_text", "output_text"}:
                    parts.append({"type": "input_text" if role == "user" else "output_text", "text": str(part.get("text", ""))})
                elif isinstance(part, Mapping) and part.get("type") == "image_url":
                    image_url = part.get("image_url")
                    url = image_url.get("url") if isinstance(image_url, Mapping) else image_url
                    if not isinstance(url, str):
                        raise ValueError("Historical image_url is invalid")
                    parts.append({"type": "input_image", "image_url": url})
            if parts:
                items.append({"type": "message", "role": role, "content": parts})
        if role == "assistant":
            for tool_call in _get(message, "tool_calls", []) or []:
                function = _get(tool_call, "function", {})
                call_id = _get(tool_call, "id")
                name = _get(function, "name")
                if not call_id or not name:
                    raise ValueError("Assistant tool calls require id and function name")
                items.append({
                    "type": "function_call",
                    "call_id": str(call_id),
                    "name": str(name),
                    "arguments": _json_arguments(_get(function, "arguments", {})),
                })
        elif role == "tool":
            call_id = _get(message, "tool_call_id") or _get(message, "call_id")
            if not call_id:
                raise ValueError("Tool messages require tool_call_id")
            output = _get(message, "content", "")
            if not isinstance(output, str):
                output = json.dumps(output, ensure_ascii=False)
            items.append({"type": "function_call_output", "call_id": str(call_id), "output": output})
        return items

    def _prepare(self, messages: Iterable[Any]) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
        all_messages = list(messages)
        system_parts: list[str] = []
        non_system: list[Any] = []
        for message in all_messages:
            role = self._message_role(message)
            if role == "system":
                text = _text_content(_get(message, "content"))
                if text:
                    system_parts.append(text)
            else:
                non_system.append(message)
        # Only a final user message becomes turn/start input.  If a caller is
        # continuing after a tool result, the empty input is intentional and
        # lets the model continue from the injected function-call history.
        current: list[dict[str, Any]] = []
        history_messages = non_system
        if non_system and self._message_role(non_system[-1]) == "user":
            current = self._user_inputs(_get(non_system[-1], "content"))
            history_messages = non_system[:-1]
        history: list[dict[str, Any]] = []
        for message in history_messages:
            history.extend(self._response_item(message))
        return "\n\n".join(system_parts), history, current

    def _new_thread(self, developer_instructions: str, tools: Optional[list[Mapping[str, Any]]] = None) -> str:
        params: Dict[str, Any] = {
            "ephemeral": True,
            "model": self.model,
            "approvalPolicy": "never",
            "sandbox": "read-only",
        }
        if tools:
            tool_lines = []
            for tool in tools:
                function = tool.get("function", tool)
                if isinstance(function, Mapping) and function.get("name"):
                    tool_lines.append(json.dumps({
                        "name": function.get("name"),
                        "description": function.get("description", ""),
                        "parameters": function.get("parameters", {}),
                    }, ensure_ascii=False, sort_keys=True))
            if tool_lines:
                developer_instructions = (developer_instructions + "\n\n" if developer_instructions else "") + (
                    "MineContext tools are available only for local retrieval. "
                    "Return a JSON object with content and tool_calls; choose only from these definitions. "
                    "Each tool_calls[].arguments value must be a JSON-encoded object string:\n"
                    + "\n".join(tool_lines)
                )
        if developer_instructions:
            params["developerInstructions"] = developer_instructions
        result = self.runtime.request("thread/start", params, timeout=self.timeout)
        thread = result.get("thread", result) if isinstance(result, Mapping) else {}
        thread_id = thread.get("id") if isinstance(thread, Mapping) else None
        if not thread_id:
            raise CodexSubscriptionError("Codex thread/start returned no thread id")
        return str(thread_id)

    @staticmethod
    def _tool_schema(tools: list[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
        schemas: dict[str, Mapping[str, Any]] = {}
        for tool in tools:
            function = tool.get("function", tool)
            name = function.get("name") if isinstance(function, Mapping) else None
            if name:
                schemas[str(name)] = function
        return schemas

    @classmethod
    def _validate_arguments(cls, function: Mapping[str, Any], args: Mapping[str, Any]) -> None:
        params = function.get("parameters") or {}
        if not isinstance(params, Mapping):
            return
        cls._validate_schema_value(params, args, "arguments")

    @classmethod
    def _validate_schema_value(
        cls, schema: Mapping[str, Any], value: Any, path: str
    ) -> None:
        expected = schema.get("type")
        expected_types = expected if isinstance(expected, list) else [expected]
        type_checks = {
            "string": lambda candidate: isinstance(candidate, str),
            "number": lambda candidate: isinstance(candidate, (int, float))
            and not isinstance(candidate, bool),
            "integer": lambda candidate: isinstance(candidate, int)
            and not isinstance(candidate, bool),
            "boolean": lambda candidate: isinstance(candidate, bool),
            "array": lambda candidate: isinstance(candidate, list),
            "object": lambda candidate: isinstance(candidate, Mapping),
            "null": lambda candidate: candidate is None,
            None: lambda candidate: True,
        }
        if not any(type_checks.get(kind, lambda candidate: True)(value) for kind in expected_types):
            raise ValueError(f"Invalid type for tool {path}")
        if "enum" in schema and value not in schema["enum"]:
            raise ValueError(f"Invalid value for tool {path}")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if "minimum" in schema and value < schema["minimum"]:
                raise ValueError(f"Tool {path} is below the minimum")
            if "maximum" in schema and value > schema["maximum"]:
                raise ValueError(f"Tool {path} is above the maximum")
        if isinstance(value, Mapping):
            properties = schema.get("properties", {}) or {}
            for name in schema.get("required", []) or []:
                if name not in value:
                    raise ValueError(f"Missing required tool {path}.{name}")
            if schema.get("additionalProperties") is False:
                unknown = set(value) - set(properties)
                if unknown:
                    raise ValueError(f"Unknown tool {path}: {sorted(unknown)}")
            for name, child in value.items():
                child_schema = properties.get(name)
                if isinstance(child_schema, Mapping):
                    cls._validate_schema_value(child_schema, child, f"{path}.{name}")
        if isinstance(value, list) and isinstance(schema.get("items"), Mapping):
            for index, child in enumerate(value):
                cls._validate_schema_value(schema["items"], child, f"{path}[{index}]")

    @classmethod
    def _output_schema(cls, tools: list[Mapping[str, Any]]) -> dict[str, Any]:
        tool_names = sorted(cls._tool_schema(tools))
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "content": {"type": ["string", "null"]},
                "tool_calls": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "name": {"type": "string", "enum": tool_names},
                            # A JSON string keeps this schema strict while the
                            # original heterogeneous tool schemas remain
                            # optional and are validated locally after parsing.
                            "arguments": {"type": "string"},
                        },
                        "required": ["name", "arguments"],
                    },
                },
            },
            "required": ["content", "tool_calls"],
        }

    @staticmethod
    def _event_matches(event: Mapping[str, Any], thread_id: str, turn_id: str) -> bool:
        params = event.get("params") or {}
        return params.get("threadId") == thread_id and (not params.get("turnId") or params.get("turnId") == turn_id)

    def _turn_input(self, thread_id: str, history: list[dict[str, Any]], current: list[dict[str, Any]], tools: Optional[list[Mapping[str, Any]]] = None, request_key: Optional[str] = None) -> CodexTurnHandle:
        try:
            if history:
                self.runtime.request(
                    "thread/inject_items",
                    {"threadId": thread_id, "items": history},
                    timeout=self.timeout,
                )
            params: Dict[str, Any] = {
                "threadId": thread_id,
                "input": current,
                "model": self.model,
                "effort": self.reasoning_effort,
            }
            if tools:
                params["outputSchema"] = self._output_schema(tools)
            handle = self.runtime.start_turn(params)
        except Exception:
            unsubscribe = getattr(self.runtime, "unsubscribe_thread", None)
            if unsubscribe:
                unsubscribe(thread_id)
            raise
        with self._active_lock:
            self._active[request_key or uuid.uuid4().hex] = handle
            cancelled = bool(request_key and request_key in self._cancelled)
            if request_key:
                self._cancelled.discard(request_key)
        if cancelled:
            self.runtime.cancel_current_turn(handle)
        return handle

    def _collect_turn(self, handle: CodexTurnHandle) -> str:
        chunks: list[str] = []
        completed_text: Optional[str] = None
        completed = False
        try:
            while True:
                event = handle.next_event(timeout=self.timeout)
                method = event.get("method")
                params = event.get("params") or {}
                if not self._event_matches(event, handle.thread_id, handle.turn_id):
                    continue
                if method == "item/agentMessage/delta":
                    chunks.append(str(params.get("delta", "")))
                elif method == "item/completed":
                    item = params.get("item") or {}
                    if item.get("type") == "agentMessage":
                        completed_text = str(item.get("text", ""))
                elif method == "turn/completed":
                    turn = params.get("turn") or {}
                    status = turn.get("status")
                    completed = True
                    if status != "completed":
                        error = turn.get("error") or {}
                        raise CodexSubscriptionError(error.get("message") or f"Codex turn ended: {status}")
                    return "".join(chunks) if chunks else (completed_text or "")
        finally:
            if not completed:
                self.runtime.cancel_current_turn(handle)
            handle.close()
            self._deactivate(handle)

    def _deactivate(self, handle: CodexTurnHandle) -> None:
        with self._active_lock:
            for key, active in list(self._active.items()):
                if active is handle:
                    self._active.pop(key, None)

    def _response(self, text: str, tools: Optional[list[Mapping[str, Any]]]) -> Any:
        tool_calls = []
        content = text
        if tools:
            try:
                structured = json.loads(text)
            except json.JSONDecodeError as exc:
                raise CodexSubscriptionError("Codex outputSchema response was not valid JSON") from exc
            if not isinstance(structured, Mapping):
                raise CodexSubscriptionError("Codex outputSchema response must be an object")
            content = structured.get("content") or ""
            schemas = self._tool_schema(tools)
            for raw in structured.get("tool_calls") or []:
                if not isinstance(raw, Mapping):
                    raise CodexSubscriptionError("Invalid tool call object")
                name = raw.get("name")
                call_id = f"call_{uuid.uuid4().hex}"
                if not isinstance(name, str) or name not in schemas:
                    raise CodexSubscriptionError(f"Model requested an unavailable tool: {name}")
                args = raw.get("arguments")
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError as exc:
                        raise CodexSubscriptionError(
                            f"Tool arguments for {name} were not valid JSON"
                        ) from exc
                if not isinstance(args, Mapping):
                    raise CodexSubscriptionError(f"Tool arguments for {name} must be an object")
                self._validate_arguments(schemas[name], args)
                tool_calls.append(SimpleNamespace(id=call_id, type="function", function=SimpleNamespace(name=name, arguments=json.dumps(args, ensure_ascii=False, separators=(",", ":")))))
        message = SimpleNamespace(content=str(content), tool_calls=tool_calls or None, role="assistant")
        choice = SimpleNamespace(index=0, message=message, finish_reason="tool_calls" if tool_calls else "stop")
        return SimpleNamespace(id=f"chatcmpl-{uuid.uuid4().hex}", choices=[choice], model=self.model)

    def generate_with_messages(self, messages: List[Any], **kwargs: Any) -> Any:
        request_key = uuid.uuid4().hex
        developer, history, current = self._prepare(messages)
        tools = kwargs.get("tools") or None
        thread_id = self._new_thread(developer, tools)
        handle = self._turn_input(thread_id, history, current, tools, request_key)
        return self._response(self._collect_turn(handle), tools)

    async def generate_with_messages_async(self, messages: List[Any], **kwargs: Any) -> Any:
        request_key = uuid.uuid4().hex
        try:
            return await asyncio.to_thread(self._generate_with_key, request_key, messages, kwargs)
        except asyncio.CancelledError:
            self._cancel_key(request_key)
            raise

    def _generate_with_key(self, request_key: str, messages: List[Any], kwargs: Mapping[str, Any]) -> Any:
        try:
            developer, history, current = self._prepare(messages)
            tools = kwargs.get("tools") or None
            thread_id = self._new_thread(developer, tools)
            handle = self._turn_input(thread_id, history, current, tools, request_key)
            return self._response(self._collect_turn(handle), tools)
        except Exception:
            with self._active_lock:
                if request_key not in self._active:
                    self._cancelled.discard(request_key)
            raise

    def _stream_sync(self, messages: List[Any], **kwargs: Any):
        request_key = kwargs.pop("_request_key", None) or uuid.uuid4().hex
        developer, history, current = self._prepare(messages)
        tools = kwargs.get("tools") or None
        thread_id = self._new_thread(developer, tools)
        handle = self._turn_input(thread_id, history, current, tools, request_key)
        text_parts: list[str] = []
        completed_text: Optional[str] = None
        completed = False
        try:
            while True:
                event = handle.next_event(timeout=self.timeout)
                method = event.get("method")
                params = event.get("params") or {}
                if not self._event_matches(event, handle.thread_id, handle.turn_id):
                    continue
                if method == "item/agentMessage/delta":
                    delta = str(params.get("delta", ""))
                    if delta:
                        text_parts.append(delta)
                        yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=delta), index=0)])
                elif method == "item/completed":
                    item = params.get("item") or {}
                    if item.get("type") == "agentMessage":
                        completed_text = str(item.get("text", ""))
                elif method == "turn/completed":
                    turn = params.get("turn") or {}
                    completed = True
                    if turn.get("status") != "completed":
                        error = turn.get("error") or {}
                        raise CodexSubscriptionError(error.get("message") or "Codex turn failed")
                    if not text_parts and completed_text:
                        yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=completed_text), index=0)])
                    return
        finally:
            if not completed:
                self.runtime.cancel_current_turn(handle)
            handle.close()
            self._deactivate(handle)

    def generate_with_messages_stream(self, messages: List[Any], **kwargs: Any):
        return self._stream_sync(messages, **kwargs)

    async def generate_with_messages_stream_async(self, messages: List[Any], **kwargs: Any) -> AsyncIterator[Any]:
        request_key = uuid.uuid4().hex
        kwargs = dict(kwargs)
        kwargs["_request_key"] = request_key
        iterator = self._stream_sync(messages, **kwargs)
        sentinel = object()

        def _next() -> Any:
            try:
                return next(iterator)
            except StopIteration:
                return sentinel

        try:
            while True:
                chunk = await asyncio.to_thread(_next)
                if chunk is sentinel:
                    return
                yield chunk
        except asyncio.CancelledError:
            self._cancel_key(request_key)
            raise

    async def _openai_chat_completion_stream_async(self, messages: List[Any], **kwargs: Any) -> AsyncIterator[Any]:
        async for chunk in self.generate_with_messages_stream_async(messages, **kwargs):
            yield chunk

    def _cancel_key(self, request_key: str) -> None:
        with self._active_lock:
            handle = self._active.get(request_key)
            if handle is None:
                self._cancelled.add(request_key)
        self.runtime.cancel_current_turn(handle)

    def close(self) -> None:
        with self._active_lock:
            if self._closed:
                return
            self._closed = True
            active_handles = list(self._active.values())
        for handle in active_handles:
            self.runtime.cancel_current_turn(handle)
        release = getattr(self.runtime, "release", None)
        if release and self._runtime_ref:
            release()
        elif not self._runtime_ref:
            self.runtime.close()
