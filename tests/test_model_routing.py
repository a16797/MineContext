import asyncio
import queue
import threading
from types import SimpleNamespace

from opencontext.llm import global_vlm_client as global_module
from opencontext.llm.codex_subscription_client import CodexSubscriptionClient
from opencontext.llm.codex_subscription_runtime import CodexTurnHandle


class _Runtime:
    def __init__(self, text="ok"):
        self.text = text
        self.requests = []

    def request(self, method, params=None, timeout=None):
        self.requests.append((method, params))
        if method == "thread/start":
            return {"thread": {"id": "thread"}}
        if method == "thread/inject_items":
            return {}
        raise AssertionError(method)

    def subscribe(self):
        return queue.Queue()

    def unsubscribe(self, subscriber):
        return None

    def unsubscribe_thread(self, thread_id):
        return None

    def start_turn(self, params):
        self.requests.append(("turn/start", params))
        subscriber = self.subscribe()
        handle = CodexTurnHandle(self, "thread", "turn", subscriber)

        def emit():
            subscriber.put(
                {
                    "method": "item/agentMessage/delta",
                    "params": {"threadId": "thread", "turnId": "turn", "delta": self.text},
                }
            )
            subscriber.put(
                {
                    "method": "turn/completed",
                    "params": {"threadId": "thread", "turn": {"id": "turn", "status": "completed"}},
                }
            )

        threading.Thread(target=emit, daemon=True).start()
        return handle

    def cancel_current_turn(self, handle):
        return None


def test_subscription_turn_includes_configured_reasoning_effort():
    runtime = _Runtime()
    client = CodexSubscriptionClient(
        {"model": "gpt-5.6-sol", "reasoning_effort": "high"}, runtime=runtime
    )

    client.generate_with_messages([{"role": "user", "content": "hello"}])

    turn = next(params for method, params in runtime.requests if method == "turn/start")
    assert turn["model"] == "gpt-5.6-sol"
    assert turn["effort"] == "high"


def test_global_routes_purposes_and_strips_purpose_from_client(monkeypatch):
    class FakeClient:
        def __init__(self, config):
            self.config = config
            self.calls = []

        async def generate_with_messages_async(self, messages, **kwargs):
            self.calls.append(kwargs)
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ok", tool_calls=None))])

        async def _openai_chat_completion_stream_async(self, messages, **kwargs):
            self.calls.append(kwargs)
            if False:
                yield None

        def close(self):
            return None

    configs = {
        "chat_model": {"provider": "fake", "model": "sol"},
        "background_model": {"provider": "fake", "model": "luna"},
        "vision_model": {"provider": "fake", "model": "luna-vision"},
    }
    clients = {}

    def fake_create(config):
        client = FakeClient(config)
        clients[config["model"]] = client
        return client

    monkeypatch.setattr(global_module, "get_config", lambda name: configs.get(name))
    monkeypatch.setattr(global_module, "create_vlm_client", fake_create)
    global_module.GlobalVLMClient.reset()
    manager = global_module.GlobalVLMClient.get_instance()

    assert manager._client_for_messages([], "chat") is clients["sol"]
    assert manager._client_for_messages([], "background") is clients["luna"]
    assert manager._client_for_messages([], "vision") is clients["luna-vision"]
    assert manager._client_for_messages([{"role": "user", "content": "plain"}]) is clients["luna"]
    assert manager._client_for_messages(
        [{"role": "user", "content": [{"type": "image_url", "image_url": "data:image/png;base64,AA=="}]}]
    ) is clients["luna-vision"]

    asyncio.run(manager.generate_for_agent_async([], purpose="chat"))
    assert clients["sol"].calls == [{"tools": None}]
    manager.close()


def test_global_partial_initialization_closes_created_clients(monkeypatch):
    class FakeClient:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    first = FakeClient()
    calls = iter([first, RuntimeError("vision failed")])

    def fake_create(_config):
        value = next(calls)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(global_module, "create_vlm_client", fake_create)
    try:
        global_module.GlobalVLMClient._create_clients(
            {"model": "chat"}, {"model": "background"}, {"model": "vision"}
        )
    except RuntimeError:
        pass
    else:
        raise AssertionError("expected client initialization failure")
    assert first.closed is True
