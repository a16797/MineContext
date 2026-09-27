import asyncio
import json
from types import SimpleNamespace

from opencontext.context_consumption.context_agent.models.schemas import ChatMessage, Query
from opencontext.context_consumption.context_agent.core.state import WorkflowState
from opencontext.context_consumption.context_agent.nodes import intent as intent_module
from opencontext.context_consumption.context_agent.nodes.intent import IntentNode
from opencontext.server.routes import agent_chat


class _StreamingManager:
    async def emit(self, _event):
        return None


def test_simple_chat_keeps_history_order_and_submits_current_query_once(monkeypatch):
    captured = []

    monkeypatch.setattr(
        intent_module,
        "get_prompt_group",
        lambda _name: {"system": "system", "user": "Current: {query}"},
    )

    async def fake_stream(messages, **_kwargs):
        captured.extend(messages)
        yield SimpleNamespace(
            choices=[SimpleNamespace(delta=SimpleNamespace(content="reply"))]
        )

    monkeypatch.setattr(intent_module, "generate_stream_for_agent", fake_stream)

    state = WorkflowState(query=Query(text="current"))
    state.contexts.chat_history = [
        ChatMessage(role="user", content="first"),
        ChatMessage(role="assistant", content="second"),
    ]

    asyncio.run(IntentNode(streaming_manager=_StreamingManager())._simple_chat(state))

    assert [message["role"] for message in captured] == ["system", "user", "assistant", "user"]
    assert [message["content"] for message in captured] == ["system", "first", "second", "Current: current"]


def test_stream_returns_immediate_sse_error_when_busy():
    async def run():
        await agent_chat.chat_stream_lock.acquire()
        try:
            response = await agent_chat.chat_stream(agent_chat.ChatRequest(query="second"), _auth="")
            return [chunk async for chunk in response.body_iterator]
        finally:
            agent_chat.chat_stream_lock.release()

    chunks = asyncio.run(run())

    chunk = chunks[0].decode() if isinstance(chunks[0], bytes) else chunks[0]
    payload = json.loads(chunk.split("data: ", 1)[1])
    assert payload["type"] == "error"
    assert "上一条回答尚未结束" in payload["content"]
