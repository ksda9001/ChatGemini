import asyncio
import json
import sqlite3
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import chatgemini.server as server
from chatgemini.config import CONFIG, DEFAULT_CONFIG
from chatgemini.cookies import cookie_pairs_from_content
from chatgemini.gemini import DirectStream, _append_continuation, _was_truncated
from chatgemini.messages import (
    compact_messages,
    google_request_to_messages,
    messages_to_prompt,
    messages_to_web_prompt,
    normalize_messages,
)
from chatgemini.media import cache_image_object, image_markdown, output_deltas
from chatgemini.sessions import ConversationStore
from chatgemini.webapi_backend import GeminiWebAPIBackend, metadata_to_state, state_to_metadata


class HttpHarness:
    def __init__(self, tmpdir, responses, reuse=False, stream_responses=None):
        self.prompts = []
        self.calls = []
        self.responses = iter(responses)
        self.stream_responses = iter(stream_responses or responses)
        self.original_generate = server.generate_with_state
        self.original_stream = server.generate_stream
        self.original_config = dict(CONFIG)
        self.original_store = server._STORE

        def fake_generate(prompt, *args, **kwargs):
            self.prompts.append(prompt)
            self.calls.append((prompt, args, kwargs))
            response = next(self.responses)
            if isinstance(response, BaseException):
                raise response
            index = len(self.prompts)
            return response, {
                "backend": "gemini_webapi",
                "conversation_id": "cid",
                "response_id": f"rid-{index}",
                "choice_id": f"choice-{index}",
            }, prompt

        def fake_stream(prompt, *args, **kwargs):
            self.prompts.append(prompt)
            response = next(self.stream_responses)
            if isinstance(response, BaseException):
                raise response
            if callable(response):
                yield from response()
                return
            if isinstance(response, (list, tuple)):
                yield from response
            else:
                yield response

        server.generate_with_state = fake_generate
        server.generate_stream = fake_stream
        CONFIG.clear()
        CONFIG.update(DEFAULT_CONFIG)
        CONFIG.update({
            "host": "127.0.0.1",
            "api_keys": [],
            "conversation_store_path": str(Path(tmpdir) / "conversations.db"),
            "media_store_path": str(Path(tmpdir) / "media"),
            "reuse_upstream_sessions": reuse,
            "max_history_messages": 20,
            "max_history_chars": 10000,
            "sse_heartbeat_sec": 1,
            "log_requests": False,
        })
        server._STORE = None
        self.httpd = server.ThreadedServer(("127.0.0.1", 0), server.ChatHandler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def request(self, path, payload=None, headers=None):
        data = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=data,
            headers={"Content-Type": "application/json", **(headers or {})},
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.headers, response.read().decode()

    def post_json(self, path, payload, headers=None):
        status, _, body = self.request(path, payload, headers)
        return status, json.loads(body)

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        server.generate_with_state = self.original_generate
        server.generate_stream = self.original_stream
        CONFIG.clear()
        CONFIG.update(self.original_config)
        server._STORE = self.original_store


class MessageTests(unittest.TestCase):
    def test_normalize_keeps_only_chat_roles(self):
        messages = normalize_messages([
            {"role": "system", "content": "Be concise"},
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi", "tool_calls": [{"id": "ignored"}]},
            {"role": "tool", "content": "secret tool output"},
            {"role": "function", "content": "function output"},
        ])
        self.assertEqual([item["role"] for item in messages], ["system", "user", "assistant"])
        prompt, _ = messages_to_prompt(messages)
        self.assertNotIn("secret tool output", prompt)
        self.assertNotIn("function output", prompt)
        self.assertNotIn("tool_calls", prompt)

    def test_multimodal_data_url_is_decoded(self):
        messages = normalize_messages([{
            "role": "user",
            "content": [
                {"type": "text", "text": "describe"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,aGk="}},
            ],
        }])
        prompt, images = messages_to_prompt(messages)
        self.assertIn("describe", prompt)
        self.assertEqual(images, [(b"hi", "image/png")])

    def test_compaction_keeps_system_and_recent_turns(self):
        messages = [{"role": "system", "content": "rules"}]
        messages.extend({"role": "user", "content": f"message-{index}"} for index in range(10))
        compacted = compact_messages(messages, max_messages=3, max_chars=10000)
        self.assertEqual(compacted[0]["content"], "rules")
        self.assertEqual([item["content"] for item in compacted[1:]], ["message-7", "message-8", "message-9"])

    def test_compaction_caps_large_last_message(self):
        compacted = compact_messages(
            [{"role": "user", "content": "x" * 1000 + "TAIL"}],
            max_messages=10,
            max_chars=100,
        )
        self.assertLess(len(compacted[0]["content"]), 180)
        self.assertIn("TAIL", compacted[0]["content"])

    def test_web_prompt_preserves_a_plain_user_turn(self):
        prompt, _ = messages_to_web_prompt([{"role": "user", "content": "666"}])
        self.assertEqual(prompt, "666")
        self.assertNotIn("[User]", prompt)

    def test_web_prompt_keeps_context_without_protocol_delimiters(self):
        prompt, _ = messages_to_web_prompt([
            {"role": "system", "content": "be concise"},
            {"role": "user", "content": "remember blue"},
            {"role": "assistant", "content": "remembered"},
            {"role": "user", "content": "what color?"},
        ])
        self.assertTrue(prompt.endswith("what color?"))
        self.assertNotIn("[User]", prompt)
        self.assertNotIn("[/Assistant]", prompt)

    def test_google_message_normalization_discards_function_protocol(self):
        messages = google_request_to_messages({
            "systemInstruction": {"parts": [{"text": "be concise"}]},
            "contents": [
                {"role": "user", "parts": [{"text": "hello"}]},
                {"role": "model", "parts": [{"text": "hi"}, {"functionCall": {
                    "name": "shell_command", "args": {"command": "secret"},
                }}]},
                {"role": "user", "parts": [{"functionResponse": {
                    "name": "shell_command", "response": {"output": "secret output"},
                }}]},
            ],
        })
        prompt, _ = messages_to_prompt(messages)
        self.assertEqual([message["role"] for message in messages], ["system", "user", "assistant"])
        self.assertIn("be concise", prompt)
        self.assertIn("hello", prompt)
        self.assertIn("hi", prompt)
        self.assertNotIn("shell_command", prompt)
        self.assertNotIn("secret output", prompt)


class DirectStreamTests(unittest.TestCase):
    def test_direct_stream_extracts_conversation_state(self):
        inner = [None] * 26
        inner[1] = ["conversation-id", "response-id"]
        inner[4] = [["choice-id", ["hello"]]]
        inner[25] = "context"
        frame = json.dumps([["wrb.fr", None, json.dumps(inner)]]) + "\n"

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def iter_text(self):
                yield frame[:40]
                yield frame[40:]

        class Client:
            def stream(self, *args, **kwargs):
                return Response()

        with patch("chatgemini.gemini.HAS_HTTPX", True), patch(
            "chatgemini.gemini._get_httpx_client", return_value=Client()
        ):
            stream = DirectStream("hello", 1, 4)
            self.assertEqual("".join(stream), "hello")
        self.assertEqual(stream.state["backend"], "direct")
        self.assertEqual(stream.state["conversation_id"], "conversation-id")
        self.assertEqual(stream.state["response_id"], "response-id")
        self.assertEqual(stream.state["choice_id"], "choice-id")

    def test_generated_image_only_response_is_not_empty(self):
        inner = [None] * 26
        inner[1] = ["conversation-id", "response-id"]
        candidate = [None] * 13
        candidate[0] = "choice-id"
        candidate[1] = [""]
        media = [None] * 8
        generated = [[None, None, None, [None, None, "a red circle", "https://images.example/red.png"]]]
        media[7] = [[generated]]
        candidate[12] = media
        inner[4] = [candidate]
        frame = json.dumps([["wrb.fr", None, json.dumps(inner)]]) + "\n"

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def iter_text(self):
                yield frame

        class Client:
            def stream(self, *args, **kwargs):
                return Response()

        with patch("chatgemini.gemini.HAS_HTTPX", True), patch(
            "chatgemini.gemini._get_httpx_client", return_value=Client()
        ), patch("chatgemini.gemini._cache_direct_image", return_value=""):
            stream = DirectStream("draw", 1, 4)
            result = "".join(stream)
        self.assertIn("![a red circle](https://images.example/red.png)", result)
        self.assertEqual(stream.state["conversation_id"], "conversation-id")


class MediaTests(unittest.TestCase):
    def test_image_markdown_rejects_non_http_urls(self):
        self.assertEqual(image_markdown("javascript:alert(1)"), "")

    def test_webapi_output_forwards_each_image_once(self):
        async def no_cache(_image):
            return ""

        image = SimpleNamespace(
            url="https://images.example/generated.png",
            alt="generated result",
            title="[Generated Image 0]",
        )
        output = SimpleNamespace(text_delta="done", images=[image])
        seen = set()
        with patch("chatgemini.media.cache_image_object", side_effect=no_cache):
            self.assertEqual(asyncio.run(output_deltas(output, seen)), [
                "done",
                "\n\n![generated result](https://images.example/generated.png)",
            ])
            self.assertEqual(asyncio.run(output_deltas(output, seen)), ["done"])

    def test_webapi_cached_image_gets_an_opaque_filename(self):
        class Image:
            async def save(self, path, filename):
                saved = Path(path) / f"20260713_hash_{filename}.jpg"
                saved.write_bytes(b"jpeg-data")
                return str(saved)

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            with patch.dict(CONFIG, {"media_store_path": str(Path(tmpdir) / "media")}):
                placeholder = asyncio.run(cache_image_object(Image()))
                filename = placeholder.rsplit("/", 1)[-1]
                self.assertRegex(filename, r"^[a-f0-9]{32}\.jpg$")
                self.assertEqual((Path(tmpdir) / "media" / filename).read_bytes(), b"jpeg-data")


class WebAPIBackendTests(unittest.TestCase):
    def test_visible_completion_returns_before_tail_drain(self):
        async def scenario():
            instance = object.__new__(GeminiWebAPIBackend)
            instance._loop = asyncio.get_running_loop()
            instance._background_tasks = set()
            instance._pending_chats = {}

            async def ensure_client():
                return object()

            instance._ensure_client = ensure_client
            instance._model = lambda _client, model: model
            tail_finished = asyncio.Event()

            def output(text_delta):
                return SimpleNamespace(
                    text_delta=text_delta,
                    images=[],
                    thoughts_delta="",
                    videos=[],
                    media=[],
                    metadata=["cid", "rid"],
                    rcid="choice",
                )

            class Chat:
                metadata = ["cid", "rid", "choice", None, None, None, None, None, None, ""]

                async def send_message_stream(self, _prompt, temporary=False):
                    yield output("pong")
                    yield output("")
                    await asyncio.sleep(0.05)
                    tail_finished.set()

            with patch("chatgemini.webapi_backend._start_isolated_chat", return_value=Chat()):
                text, state = await instance._generate("ping", "gemini-3.5-flash")
                self.assertEqual(text, "pong")
                self.assertEqual(state["conversation_id"], "cid")
                self.assertFalse(tail_finished.is_set())
                await asyncio.gather(*list(instance._background_tasks))
                self.assertTrue(tail_finished.is_set())

        asyncio.run(scenario())

    def test_next_turn_waits_for_previous_tail_drain(self):
        async def scenario():
            instance = object.__new__(GeminiWebAPIBackend)
            instance._loop = asyncio.get_running_loop()
            instance._background_tasks = set()
            instance._pending_chats = {}

            async def ensure_client():
                return object()

            instance._ensure_client = ensure_client
            instance._model = lambda _client, model: model
            release_tail = asyncio.Event()
            second_started = asyncio.Event()
            chats_created = 0

            def output(text_delta, cid):
                return SimpleNamespace(
                    text_delta=text_delta,
                    images=[],
                    thoughts_delta="",
                    videos=[],
                    media=[],
                    metadata=[cid, "rid"],
                    rcid="choice",
                )

            class Chat:
                def __init__(self, index):
                    self.index = index
                    self.metadata = ["cid", f"rid-{index}", "choice", None, None, None, None, None, None, ""]

                async def send_message_stream(self, _prompt, temporary=False):
                    if self.index == 2:
                        second_started.set()
                    yield output(f"answer-{self.index}", "cid")
                    yield output("", "cid")
                    if self.index == 1:
                        await release_tail.wait()

            def start_chat(*_args, **_kwargs):
                nonlocal chats_created
                chats_created += 1
                return Chat(chats_created)

            with patch("chatgemini.webapi_backend._start_isolated_chat", side_effect=start_chat):
                first_text, first_state = await instance._generate("first", "gemini-3.5-flash")
                self.assertEqual(first_text, "answer-1")
                second = asyncio.create_task(
                    instance._generate("second", "gemini-3.5-flash", first_state)
                )
                await asyncio.sleep(0.01)
                self.assertFalse(second_started.is_set())
                release_tail.set()
                second_text, _ = await second
                self.assertEqual(second_text, "answer-2")
                self.assertTrue(second_started.is_set())
                await asyncio.gather(*list(instance._background_tasks))

        asyncio.run(scenario())


class StoreTests(unittest.TestCase):
    def test_store_contains_only_plain_conversation_table(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            path = str(Path(tmpdir) / "chat.db")
            store = ConversationStore(path)
            history = [{"role": "user", "content": "one"}, {"role": "assistant", "content": "two"}]
            store.save("gemini-3.5-flash", {"conversation_id": "cid"}, history)
            with sqlite3.connect(path) as connection:
                tables = {
                    row[0] for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
            self.assertEqual(tables, {"conversation_sessions"})

    def test_store_returns_only_new_chat_messages(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            store = ConversationStore(str(Path(tmpdir) / "chat.db"))
            known = [{"role": "user", "content": "remember 123"}, {"role": "assistant", "content": "ok"}]
            state = {"conversation_id": "cid"}
            store.save("gemini-3.5-flash", state, known)
            current = known + [{"role": "user", "content": "what was it?"}]
            found = store.find("gemini-3.5-flash", current)
            self.assertEqual(found["upstream_state"], state)
            self.assertEqual(found["delta_messages"], current[-1:])


class ProtocolTests(unittest.TestCase):
    def test_cached_media_route_serves_an_opaque_image(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            harness = HttpHarness(tmpdir, [])
            try:
                filename = "a" * 32 + ".png"
                media_dir = Path(CONFIG["media_store_path"])
                media_dir.mkdir(parents=True)
                (media_dir / filename).write_bytes(b"png-data")
                status, headers, body = harness.request(f"/media/{filename}")
                self.assertEqual(status, 200)
                self.assertEqual(headers.get("Content-Type"), "image/png")
                self.assertEqual(body, "png-data")
            finally:
                harness.close()

    def test_health_and_models(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            harness = HttpHarness(tmpdir, [])
            try:
                status, _, body = harness.request("/")
                self.assertEqual(status, 200)
                self.assertTrue(json.loads(body)["chat_only"])
                status, _, body = harness.request("/v1/models")
                self.assertEqual(status, 200)
                self.assertGreater(len(json.loads(body)["data"]), 0)
                status, _, body = harness.request("/v1beta/models")
                self.assertEqual(status, 200)
                self.assertTrue(json.loads(body)["models"][0]["name"].startswith("models/"))
            finally:
                harness.close()

    def test_non_stream_chat_is_openai_compatible(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            harness = HttpHarness(tmpdir, ["hello back"])
            try:
                status, response = harness.post_json("/v1/chat/completions", {
                    "model": "gemini-3.5-flash",
                    "messages": [{"role": "user", "content": "hello"}],
                })
                self.assertEqual(status, 200)
                self.assertEqual(response["object"], "chat.completion")
                self.assertEqual(response["choices"][0]["message"]["content"], "hello back")
                self.assertEqual(response["choices"][0]["finish_reason"], "stop")
            finally:
                harness.close()

    def test_stream_chat_has_role_text_stop_and_done(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            harness = HttpHarness(tmpdir, [], stream_responses=[["hello", " world"]])
            try:
                status, headers, body = harness.request("/v1/chat/completions?source=openwebui", {
                    "model": "gemini-3.5-flash",
                    "messages": [{"role": "user", "content": "hello"}],
                    "stream": True,
                    "stream_options": {"include_usage": True},
                })
                self.assertEqual(status, 200)
                self.assertIn("text/event-stream", headers.get("Content-Type"))
                self.assertIn('"role": "assistant"', body)
                self.assertIn('"content": "hello"', body)
                self.assertIn('"finish_reason": "stop"', body)
                self.assertIn('"choices": [], "usage":', body)
                self.assertTrue(body.rstrip().endswith("data: [DONE]"))
            finally:
                harness.close()

    def test_stream_sends_heartbeat_while_waiting_for_first_text(self):
        def slow_response():
            time.sleep(1.2)
            yield "ready"

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            harness = HttpHarness(tmpdir, [], stream_responses=[slow_response])
            try:
                _, _, body = harness.request("/v1/chat/completions", {
                    "model": "gemini-3.5-flash",
                    "messages": [{"role": "user", "content": "wait"}],
                    "stream": True,
                })
                self.assertIn(": keep-alive", body)
                self.assertIn('"content": "ready"', body)
            finally:
                harness.close()

    def test_tools_are_ignored_and_never_reach_prompt(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            harness = HttpHarness(tmpdir, ["plain chat answer"])
            try:
                _, response = harness.post_json("/v1/chat/completions", {
                    "model": "gemini-3.5-flash",
                    "messages": [{"role": "user", "content": "explain the weather"}],
                    "tools": [{"type": "function", "function": {
                        "name": "shell_command",
                        "description": "execute commands",
                        "parameters": {"type": "object"},
                    }}],
                    "tool_choice": "required",
                })
                self.assertEqual(response["choices"][0]["message"]["content"], "plain chat answer")
                prompt = harness.prompts[0]
                self.assertNotIn("shell_command", prompt)
                self.assertNotIn("execute commands", prompt)
                self.assertNotIn("Tool Use", prompt)
            finally:
                harness.close()

    def test_google_non_stream_is_text_only_and_ignores_tools(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            harness = HttpHarness(tmpdir, ["plain Gemini answer"])
            try:
                status, response = harness.post_json("/v1beta/models/gemini-3.5-flash-thinking:generateContent", {
                    "systemInstruction": {"parts": [{"text": "answer succinctly"}]},
                    "contents": [{"role": "user", "parts": [
                        {"text": "explain the weather"},
                        {"functionResponse": {"name": "shell_command", "response": {"output": "secret"}}},
                    ]}],
                    "tools": [{"functionDeclarations": [{
                        "name": "shell_command",
                        "description": "execute commands",
                    }]}],
                    "toolConfig": {"functionCallingConfig": {"mode": "ANY"}},
                })
                self.assertEqual(status, 200)
                self.assertEqual(response["candidates"][0]["content"]["role"], "model")
                self.assertEqual(response["candidates"][0]["content"]["parts"][0]["text"], "plain Gemini answer")
                self.assertEqual(response["candidates"][0]["finishReason"], "STOP")
                prompt = harness.prompts[0]
                self.assertIn("explain the weather", prompt)
                self.assertNotIn("shell_command", prompt)
                self.assertNotIn("execute commands", prompt)
                self.assertNotIn("secret", prompt)
            finally:
                harness.close()

    def test_google_stream_uses_google_sse_without_done_frame(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            harness = HttpHarness(tmpdir, [], stream_responses=[["hello", " world"]])
            try:
                status, headers, body = harness.request(
                    "/v1beta/models/gemini-3.5-flash:streamGenerateContent?alt=sse",
                    {"contents": [{"role": "user", "parts": [{"text": "hello"}]}]},
                )
                self.assertEqual(status, 200)
                self.assertIn("text/event-stream", headers.get("Content-Type"))
                self.assertIn('"text": "hello"', body)
                self.assertIn('"text": " world"', body)
                self.assertIn('"finishReason": "STOP"', body)
                self.assertNotIn("[DONE]", body)
            finally:
                harness.close()

    def test_non_chat_routes_are_not_exposed(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            harness = HttpHarness(tmpdir, [])
            try:
                for path in ("/v1/responses", "/v1/messages", "/v1beta/models/x:countTokens"):
                    with self.assertRaises(urllib.error.HTTPError) as caught:
                        harness.request(path, {})
                    self.assertEqual(caught.exception.code, 404)
                    caught.exception.close()
            finally:
                harness.close()

    def test_plain_conversation_resumes_with_delta_only(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            harness = HttpHarness(tmpdir, ["remembered", "123"], reuse=True)
            try:
                first_messages = [{"role": "user", "content": "remember 123"}]
                _, first = harness.post_json("/v1/chat/completions", {
                    "model": "gemini-3.5-flash", "messages": first_messages,
                })
                second_messages = first_messages + [
                    first["choices"][0]["message"],
                    {"role": "user", "content": "what was it?"},
                ]
                _, second = harness.post_json("/v1/chat/completions", {
                    "model": "gemini-3.5-flash", "messages": second_messages,
                })
                self.assertEqual(second["choices"][0]["message"]["content"], "123")
                self.assertIn("what was it?", harness.prompts[1])
                self.assertNotIn("remember 123", harness.prompts[1])
                self.assertEqual(harness.calls[1][1][4]["conversation_id"], "cid")
            finally:
                harness.close()

    def test_stream_conversation_saves_web_state_and_resumes(self):
        class FakeWebStream:
            def __init__(self, text, state):
                self.text = text
                self.next_state = state
                self.state = None

            def __iter__(self):
                yield self.text
                self.state = self.next_state

        state = {
            "backend": "gemini_webapi",
            "conversation_id": "stream-cid",
            "response_id": "stream-rid",
            "choice_id": "stream-choice",
        }
        calls = []

        def fake_web_stream(prompt, model, upstream_state, temporary=False):
            calls.append((prompt, model, upstream_state, temporary))
            return FakeWebStream("first" if len(calls) == 1 else "second", state)

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            harness = HttpHarness(tmpdir, [], reuse=True)
            try:
                CONFIG["upstream_session_backend"] = "gemini_webapi"
                with patch("chatgemini.webapi_backend.generate_stream_with_state", side_effect=fake_web_stream):
                    _, _, first_body = harness.request("/v1/chat/completions", {
                        "model": "gemini-3.5-flash",
                        "messages": [{"role": "user", "content": "remember"}],
                        "stream": True,
                    })
                    _, _, second_body = harness.request("/v1/chat/completions", {
                        "model": "gemini-3.5-flash",
                        "messages": [
                            {"role": "user", "content": "remember"},
                            {"role": "assistant", "content": "first"},
                            {"role": "user", "content": "continue"},
                        ],
                        "stream": True,
                    })
                self.assertIn('"content": "first"', first_body)
                self.assertIn('"content": "second"', second_body)
                self.assertEqual(calls[0][0], "remember")
                self.assertEqual(calls[1][0], "continue")
                self.assertEqual(calls[1][2]["conversation_id"], "stream-cid")
            finally:
                harness.close()

    def test_stream_direct_conversation_saves_and_resumes(self):
        class FakeDirectStream:
            def __init__(self, text, state):
                self.text = text
                self.state = state

            def __iter__(self):
                yield self.text

        state = {
            "backend": "direct",
            "conversation_id": "direct-cid",
            "response_id": "direct-rid",
            "choice_id": "direct-choice",
            "metadata": ["direct-cid", "direct-rid", "direct-choice", None, None, None, None, None, None, ""],
        }
        calls = []

        def fake_direct_stream(prompt, *args, **kwargs):
            calls.append((prompt, kwargs))
            return FakeDirectStream("first" if len(calls) == 1 else "second", state)

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            harness = HttpHarness(tmpdir, [], reuse=True)
            try:
                CONFIG["upstream_session_backend"] = "direct"
                with patch("chatgemini.server.generate_stream", side_effect=fake_direct_stream):
                    _, _, first_body = harness.request("/v1/chat/completions", {
                        "model": "gemini-3.5-flash",
                        "messages": [{"role": "user", "content": "remember"}],
                        "stream": True,
                    })
                    _, _, second_body = harness.request("/v1/chat/completions", {
                        "model": "gemini-3.5-flash",
                        "messages": [
                            {"role": "user", "content": "remember"},
                            {"role": "assistant", "content": "first"},
                            {"role": "user", "content": "continue"},
                        ],
                        "stream": True,
                    })
                self.assertIn('"content": "first"', first_body)
                self.assertIn('"content": "second"', second_body)
                self.assertEqual(calls[0][0], "[User]\nremember\n[/User]")
                self.assertEqual(calls[1][0], "[User]\ncontinue\n[/User]")
                self.assertIsNone(calls[0][1]["conversation"])
                self.assertEqual(calls[1][1]["conversation"]["conversation_id"], "direct-cid")
            finally:
                harness.close()

    def test_background_request_is_temporary(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            harness = HttpHarness(tmpdir, ["A title"])
            try:
                harness.post_json("/v1/chat/completions", {
                    "model": "gemini-3.5-flash",
                    "messages": [{"role": "user", "content": (
                        "Generate a concise, 3-5 word title with an emoji summarizing the chat history."
                    )}],
                })
                self.assertTrue(harness.calls[0][2]["temporary"])
            finally:
                harness.close()

    def test_upstream_error_uses_openai_error_shape(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            harness = HttpHarness(tmpdir, [RuntimeError("offline")])
            try:
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    harness.request("/v1/chat/completions", {
                        "model": "gemini-3.5-flash",
                        "messages": [{"role": "user", "content": "hello"}],
                    })
                self.assertEqual(caught.exception.code, 502)
                body = json.loads(caught.exception.read().decode())
                self.assertEqual(body["error"]["type"], "upstream_error")
                caught.exception.close()
            finally:
                harness.close()

    def test_api_key_authentication(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            harness = HttpHarness(tmpdir, ["ok"])
            CONFIG["api_keys"] = ["secret"]
            try:
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    harness.request("/v1/models")
                self.assertEqual(caught.exception.code, 401)
                caught.exception.close()
                status, _, _ = harness.request("/v1/models", headers={"Authorization": "Bearer secret"})
                self.assertEqual(status, 200)
            finally:
                harness.close()


class TransportTests(unittest.TestCase):
    def test_truncation_overlap_is_removed(self):
        self.assertEqual(_append_continuation("hello world", "world again"), "hello world again")

    def test_1155_is_recognized_as_truncation(self):
        self.assertTrue(_was_truncated('"BardErrorInfo",[1155]'))

    def test_cookie_export_ignores_expired_records(self):
        content = json.dumps([
            {"name": "__Secure-1PSID", "value": "login", "domain": ".google.com", "expirationDate": 3000},
            {"name": "expired", "value": "no", "domain": ".google.com", "expirationDate": 999},
        ])
        pairs, _ = cookie_pairs_from_content(content, now=1000)
        self.assertEqual(pairs, {"__Secure-1PSID": "login"})

    def test_metadata_round_trip(self):
        metadata = ["cid", "rid", "choice", None, None, None, None, None, None, "ctx"]
        self.assertEqual(state_to_metadata(metadata_to_state(metadata)), metadata)


if __name__ == "__main__":
    unittest.main()
