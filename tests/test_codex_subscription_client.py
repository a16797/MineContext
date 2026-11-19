import asyncio
import json
import queue
import threading

import pytest

from opencontext.llm.codex_subscription_client import CodexSubscriptionClient
from opencontext.llm.codex_subscription_runtime import CodexSubscriptionRuntime, CodexTurnHandle


class FakeRuntime:
    def __init__(self, response_text, block=False, authenticated=True):
        self.response_text = response_text
        self.block = block
        self.authenticated = authenticated
        self.requests = []
        self.interrupted = []
        self._subscribers = set()

    def account_read(self, refresh_token=False):
        return {"account": {"type": "chatgpt"} if self.authenticated else None}

    def request(self, method, params=None, timeout=None):
        self.requests.append((method, params))
        if method == "thread/start":
            return {"thread": {"id": "thread-test"}}
        if method == "thread/inject_items":
            return {}
        raise AssertionError(f"unexpected request: {method}")

    def subscribe(self):
        subscriber = queue.Queue()
        self._subscribers.add(subscriber)
        return subscriber

    def unsubscribe(self, subscriber):
        self._subscribers.discard(subscriber)

    def start_turn(self, params):
        self.requests.append(("turn/start", params))
        subscriber = self.subscribe()
        handle = CodexTurnHandle(self, "thread-test", "turn-test", subscriber)
        self.handle = handle

        def emit():
            subscriber.put({"method": "item/agentMessage/delta", "params": {"threadId": "thread-test", "turnId": "turn-test", "delta": self.response_text}})
            subscriber.put({"method": "turn/completed", "params": {"threadId": "thread-test", "turn": {"id": "turn-test", "status": "completed"}}})

        if not self.block:
            threading.Thread(target=emit, daemon=True).start()
        return handle

    def cancel_current_turn(self, handle):
        self.interrupted.append((handle.thread_id, handle.turn_id))
        handle._queue.put({"method": "turn/completed", "params": {"threadId": handle.thread_id, "turn": {"id": handle.turn_id, "status": "interrupted"}}})

    def close(self):
        pass


def _client(runtime, **config):
    return CodexSubscriptionClient({"model": "mock-vision", **config}, runtime=runtime)


def test_prepare_preserves_images_and_function_call_history():
    client = _client(FakeRuntime("answer"))
    messages = [
        {"role": "system", "content": "be concise"},
        {"role": "user", "content": [{"type": "text", "text": "look"}, {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}}]},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "call-1", "type": "function", "function": {"name": "find", "arguments": {"q": "look"}}}]},
        {"role": "tool", "tool_call_id": "call-1", "content": "result"},
    ]
    developer, history, current = client._prepare(messages)
    assert developer == "be concise"
    assert current == []  # empty continuation after a tool result
    assert history[-2]["type"] == "function_call"
    assert history[-2]["call_id"] == "call-1"
    assert history[-1] == {"type": "function_call_output", "call_id": "call-1", "output": "result"}

    _, _, current = client._prepare(messages[:2])
    assert current[-1] == {"type": "image", "url": "data:image/png;base64,AA=="}


def test_tools_are_returned_as_openai_like_calls_and_validated():
    structured = json.dumps({"content": "", "tool_calls": [{"id": "call-1", "name": "find", "arguments": json.dumps({"query": "today"})}]})
    runtime = FakeRuntime(structured)
    client = _client(runtime)
    response = client.generate_with_messages(
        [{"role": "user", "content": "search"}],
        tools=[{"type": "function", "function": {"name": "find", "parameters": {"type": "object", "required": ["query"], "properties": {"query": {"type": "string"}}}}}],
    )
    call = response.choices[0].message.tool_calls[0]
    assert call.id.startswith("call_")
    assert call.function.name == "find"
    assert json.loads(call.function.arguments) == {"query": "today"}
    assert runtime.requests[1][0] == "turn/start"
    assert runtime.requests[1][1]["outputSchema"]["required"] == ["content", "tool_calls"]
    tool_item = runtime.requests[1][1]["outputSchema"]["properties"]["tool_calls"]["items"]
    assert tool_item["properties"]["name"]["enum"] == ["find"]
    assert tool_item["properties"]["arguments"] == {"type": "string"}


def test_invalid_tool_name_fails_closed():
    runtime = FakeRuntime(json.dumps({"content": "", "tool_calls": [{"id": "call-1", "name": "shell", "arguments": {}}]}))
    client = _client(runtime)
    with pytest.raises(Exception, match="unavailable tool"):
        client.generate_with_messages(
            [{"role": "user", "content": "run"}],
            tools=[{"type": "function", "function": {"name": "find", "parameters": {"type": "object"}}}],
        )


def test_generation_stops_before_inference_when_subscription_is_logged_out():
    runtime = FakeRuntime("unused", authenticated=False)
    client = _client(runtime)

    with pytest.raises(Exception, match="login is unavailable"):
        client.generate_with_messages([{"role": "user", "content": "hello"}])

    assert runtime.requests == []


def test_invalid_nested_tool_arguments_fail_closed():
    runtime = FakeRuntime(
        json.dumps(
            {
                "content": "",
                "tool_calls": [
                    {
                        "name": "find",
                        "arguments": {
                            "time_range": {"time_type": "not-a-real-time-type"}
                        },
                    }
                ],
            }
        )
    )
    client = _client(runtime)
    with pytest.raises(ValueError, match="Invalid value"):
        client.generate_with_messages(
            [{"role": "user", "content": "search"}],
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "find",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "time_range": {
                                    "type": "object",
                                    "properties": {
                                        "time_type": {
                                            "type": "string",
                                            "enum": ["event_time_ts"],
                                        }
                                    },
                                }
                            },
                        },
                    },
                }
            ],
        )


def test_async_stream_returns_deltas_without_duplicate_final_text():
    async def run():
        runtime = FakeRuntime("hello")
        client = _client(runtime)
        chunks = []
        async for chunk in client.generate_with_messages_stream_async([{"role": "user", "content": "hi"}]):
            chunks.append(chunk.choices[0].delta.content)
        return chunks

    assert asyncio.run(run()) == ["hello"]


def test_async_cancellation_interrupts_the_matching_turn():
    async def run():
        runtime = FakeRuntime("never-used", block=True)
        client = _client(runtime)
        task = asyncio.create_task(client.generate_with_messages_async([{"role": "user", "content": "wait"}]))
        for _ in range(100):
            if getattr(client, "_active", {}):
                break
            await asyncio.sleep(0.001)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.01)  # let the cancelled worker perform cleanup
        return runtime.interrupted

    assert asyncio.run(run()) == [("thread-test", "turn-test")]


def test_timeout_interrupts_and_cleans_up_the_turn():
    runtime = FakeRuntime("never-used", block=True)
    client = _client(runtime, timeout=0.01)

    with pytest.raises(TimeoutError):
        client.generate_with_messages([{"role": "user", "content": "wait"}])

    assert runtime.interrupted == [("thread-test", "turn-test")]
    assert client._active == {}


def test_server_requests_are_rejected_without_execution():
    runtime = CodexSubscriptionRuntime({"disable_features": []})
    sent = []
    runtime._write = sent.append
    runtime._reply_server_request({"id": 7, "method": "item/commandExecution/requestApproval", "params": {}})
    assert sent[0]["id"] == 7
    assert sent[0]["error"]["code"] == -32001


def test_relative_context_path_never_stores_auth_in_the_repository(monkeypatch, tmp_path):
    monkeypatch.setenv("CONTEXT_PATH", ".")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

    home, cwd = CodexSubscriptionRuntime({})._paths()

    assert home == tmp_path / "MineContext" / "codex-home"
    assert cwd == tmp_path / "MineContext" / "codex-workspace"


def test_codex_login_path_is_stable_across_context_paths(monkeypatch, tmp_path):
    monkeypatch.setenv("CONTEXT_PATH", str(tmp_path / "electron-user-data"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

    home, cwd = CodexSubscriptionRuntime({})._paths()

    assert home == tmp_path / "MineContext" / "codex-home"
    assert cwd == tmp_path / "MineContext" / "codex-workspace"


def test_codex_runtime_uses_isolated_file_credentials(monkeypatch):
    monkeypatch.setattr(CodexSubscriptionRuntime, "_find_cli", staticmethod(lambda explicit=None: "codex.exe"))

    command = CodexSubscriptionRuntime({})._command()

    assert 'cli_auth_credentials_store="file"' in command


def test_runtime_stages_inline_images_until_handle_closes(tmp_path):
    import base64
    from pathlib import Path
    runtime = CodexSubscriptionRuntime({"codex_home": str(tmp_path/'home'), "cwd": str(tmp_path/'work')})
    payload = b'\x89PNG\r\n\x1a\nexample'
    original = {"threadId": "t", "input": [{"type": "text", "text": "look"},
                {"type": "image", "url": "data:image/png;base64,"+base64.b64encode(payload).decode(), "detail": "high"}]}
    requests = []
    def request(method, params):
        requests.append(params)
        assert Path(params['input'][1]['path']).read_bytes() == payload
        return {"turn": {"id": "turn"}}
    runtime.request = request
    handle = runtime.start_turn(original)
    path = Path(requests[0]['input'][1]['path'])
    assert requests[0]['input'][1]['type'] == 'localImage'
    assert requests[0]['input'][1]['detail'] == 'high'
    assert 'base64' not in json.dumps(requests[0])
    assert original['input'][1]['type'] == 'image'
    handle.close()
    handle.close()
    assert not path.exists()
    assert not runtime._subscribers


def test_runtime_staged_images_are_removed_when_start_fails(tmp_path):
    runtime = CodexSubscriptionRuntime({"codex_home": str(tmp_path/'home'), "cwd": str(tmp_path/'work')})
    def fail(method, params):
        raise RuntimeError('start failed')
    runtime.request = fail
    with pytest.raises(RuntimeError, match='start failed'):
        runtime.start_turn({"threadId":"t", "input":[{"type":"image","url":"data:image/png;base64,YQ=="}]})
    assert not list((tmp_path/'work').iterdir())
    assert not runtime._subscribers


def test_runtime_rejects_bad_inline_image_without_sending(tmp_path):
    runtime = CodexSubscriptionRuntime({"codex_home": str(tmp_path/'home'), "cwd": str(tmp_path/'work')})
    runtime.request = lambda *args: pytest.fail('Malformed image was submitted')
    with pytest.raises(ValueError):
        runtime.start_turn({"threadId":"t", "input":[{"type":"image","url":"data:image/png;base64,!!"}]})
    assert not runtime._subscribers


def test_runtime_remote_images_and_text_pass_through():
    runtime = CodexSubscriptionRuntime({})
    params = {"threadId":"t", "input":[{"type":"text","text":"look"}, {"type":"image","url":"https://example.com/a.png"}]}
    def request(method, wire):
        assert wire == params
        return {"turn":{"id":"turn"}}
    runtime.request = request
    runtime.start_turn(params).close()
