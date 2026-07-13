"""Small OpenAI-compatible HTTP server for ordinary chat only."""
import json
import queue
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from urllib.parse import urlsplit

from . import __version__
from .config import CONFIG
from .gemini import generate_stream, generate_with_state, log
from .messages import compact_messages, messages_to_prompt, serializable_messages
from .models import MODELS, resolve_model
from .multimodal import fetch_image_bytes, upload_image
from .sessions import ConversationStore


BACKGROUND_TASK_MARKERS = (
    "Generate a concise, 3-5 word title with an emoji summarizing the chat history.",
    "Generate 1-3 broad tags categorizing the main themes of the chat history",
    "Suggest 3-5 relevant follow-up questions or prompts that the user might naturally ask next",
    "Generate a detailed prompt for am image generation task based on the given language and context.",
)

_STORE = None


def _is_background_request(prompt: str) -> bool:
    return bool(
        CONFIG.get("temporary_background_tasks", True)
        and isinstance(prompt, str)
        and any(marker in prompt for marker in BACKGROUND_TASK_MARKERS)
    )


def _store() -> ConversationStore:
    global _STORE
    if _STORE is None:
        _STORE = ConversationStore(
            CONFIG.get("conversation_store_path", "conversations.db"),
            CONFIG.get("conversation_store_ttl_sec", 86400),
            CONFIG.get("conversation_store_max_rows", 2000),
        )
    return _STORE


def _usage(prompt: str, text: str) -> dict:
    prompt_tokens = max(0, len(prompt or "") // 4)
    completion_tokens = max(0, len(text or "") // 4)
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
    }


def _upload_images(images: list):
    references = []
    for source, mime in images or []:
        try:
            data = fetch_image_bytes(source) if isinstance(source, str) else source
            if data:
                references.append(upload_image(data, "image", mime or "image/png"))
        except Exception as exc:
            log(f"Image upload failed: {exc}")
    return references or None


class ChatHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        log(fmt % args)

    @property
    def route(self):
        return urlsplit(self.path).path

    def _authorized(self):
        keys = CONFIG.get("api_keys") or []
        if not keys:
            return True
        authorization = self.headers.get("Authorization", "")
        bearer = authorization[7:] if authorization.startswith("Bearer ") else ""
        return (bearer or self.headers.get("x-api-key", "")) in keys

    def send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _start_sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Connection", "close")
        self.end_headers()

    def _sse_data(self, data):
        payload = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
        self.wfile.write(f"data: {payload}\n\n".encode())
        self.wfile.flush()

    def _heartbeat_iter(self, iterable):
        output = queue.Queue()

        def consume():
            try:
                for item in iterable:
                    output.put(("item", item))
                output.put(("done", None))
            except BaseException as exc:
                output.put(("error", exc))

        threading.Thread(target=consume, daemon=True).start()
        heartbeat = max(1, int(CONFIG.get("sse_heartbeat_sec", 10) or 10))
        while True:
            try:
                kind, value = output.get(timeout=heartbeat)
            except queue.Empty:
                self.wfile.write(b": keep-alive\n\n")
                self.wfile.flush()
                continue
            if kind == "item":
                yield value
            elif kind == "error":
                raise value
            else:
                return

    def _prepare_chat(self, request: dict, model_name: str):
        messages = compact_messages(
            request.get("messages") or [],
            int(CONFIG.get("max_history_messages", 60) or 60),
            int(CONFIG.get("max_history_chars", 80000) or 80000),
        )
        full_prompt, images = messages_to_prompt(messages)
        stored_messages = serializable_messages(messages)
        temporary = _is_background_request(full_prompt)
        prompt = full_prompt
        upstream_state = None
        resumed = False

        if CONFIG.get("reuse_upstream_sessions", False) and not images and not temporary:
            try:
                session = _store().find(model_name, stored_messages)
                if session:
                    delta_prompt, _ = messages_to_prompt(session["delta_messages"])
                    if delta_prompt.strip():
                        prompt = delta_prompt
                        upstream_state = session["upstream_state"]
                        resumed = True
            except Exception as exc:
                log(f"Conversation cache lookup failed; using full history: {exc}")
        return messages, stored_messages, prompt, full_prompt, images, upstream_state, temporary, resumed

    def _save_turn(self, model_name, upstream_state, messages, text, temporary):
        if not CONFIG.get("reuse_upstream_sessions", False) or not upstream_state or temporary:
            return
        known = list(messages) + [{"role": "assistant", "content": text or ""}]
        try:
            _store().save(model_name, upstream_state, known)
        except Exception as exc:
            log(f"Conversation cache save failed: {exc}")

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        if self.route.startswith("/v1/") and not self._authorized():
            self.close_connection = True
            self.send_json({"error": {"message": "invalid api key", "type": "authentication_error"}}, 401)
            return
        if self.route == "/v1/models":
            self.send_json({
                "object": "list",
                "data": [
                    {
                        "id": name,
                        "object": "model",
                        "created": 1700000000,
                        "owned_by": "google-web",
                        "description": config["desc"],
                    }
                    for name, config in MODELS.items()
                ],
            })
        elif self.route in ("/", "/health", "/healthz"):
            self.send_json({
                "status": "ok",
                "service": "ChatGemini",
                "version": __version__,
                "chat_only": True,
                "models": list(MODELS),
            })
        else:
            self.send_json({"error": {"message": "not found", "type": "not_found_error"}}, 404)

    def do_POST(self):
        if self.route.startswith("/v1/") and not self._authorized():
            self.close_connection = True
            self.send_json({"error": {"message": "invalid api key", "type": "authentication_error"}}, 401)
            return
        if self.route != "/v1/chat/completions":
            # The body belongs to an unsupported endpoint. Closing prevents an
            # HTTP/1.1 keep-alive parser from treating it as a second request.
            self.close_connection = True
            self.send_json({"error": {"message": "not found", "type": "not_found_error"}}, 404)
            return

        try:
            length = int(self.headers.get("Content-Length", "0") or 0)
        except ValueError:
            self.send_json({"error": {"message": "invalid Content-Length", "type": "invalid_request_error"}}, 400)
            return
        limit = int(CONFIG.get("max_request_body_bytes", 20 * 1024 * 1024) or 0)
        if limit > 0 and length > limit:
            self.send_json({"error": {"message": "request body too large", "type": "invalid_request_error"}}, 413)
            return
        body = self.rfile.read(length) if length else b""
        try:
            request = json.loads(body)
        except (TypeError, ValueError):
            self.send_json({"error": {"message": "invalid JSON", "type": "invalid_request_error"}}, 400)
            return
        try:
            self._handle_chat(request)
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as exc:
            log(f"Unexpected chat request error: {exc}")
            self.send_json({
                "error": {
                    "message": "internal server error",
                    "type": "server_error",
                }
            }, 500)

    def _handle_chat(self, request: dict):
        if not isinstance(request, dict) or not isinstance(request.get("messages"), list):
            self.send_json({"error": {"message": "messages must be an array", "type": "invalid_request_error"}}, 400)
            return
        model_name, model_id, think_mode, error, extra_fields = resolve_model(
            request.get("model") or CONFIG["default_model"]
        )
        if error:
            self.send_json({"error": {"message": error, "type": "invalid_request_error"}}, 400)
            return

        prepared = self._prepare_chat(request, model_name)
        messages, stored_messages, prompt, full_prompt, images, state, temporary, resumed = prepared
        if not prompt.strip() and not images:
            self.send_json({"error": {"message": "empty chat messages", "type": "invalid_request_error"}}, 400)
            return
        ignored_tools = bool(request.get("tools") or request.get("functions") or request.get("tool_choice"))
        log(
            f"Chat request: model={model_name} stream={bool(request.get('stream'))} "
            f"temporary={temporary} resumed={resumed} ignored_tools={ignored_tools} "
            f"prompt_len={len(prompt)} fallback_len={len(full_prompt)}"
        )

        if request.get("stream"):
            stream_options = request.get("stream_options") or {}
            self._stream_chat(
                model_name, model_id, think_mode, extra_fields, stored_messages,
                prompt, full_prompt, images, state, temporary,
                bool(stream_options.get("include_usage")) if isinstance(stream_options, dict) else False,
            )
        else:
            self._complete_chat(
                model_name, model_id, think_mode, extra_fields, stored_messages,
                prompt, full_prompt, images, state, temporary,
            )

    def _complete_chat(
        self, model_name, model_id, think_mode, extra_fields, messages,
        prompt, full_prompt, images, state, temporary,
    ):
        try:
            text, state, usage_prompt = generate_with_state(
                prompt,
                model_id,
                think_mode,
                _upload_images(images),
                extra_fields,
                state,
                full_prompt,
                model_name=model_name,
                temporary=temporary,
            )
        except Exception as exc:
            self.send_json({"error": {"message": f"upstream error: {exc}", "type": "upstream_error"}}, 502)
            return

        self._save_turn(model_name, state, messages, text, temporary)
        completion_id = f"chatcmpl-{uuid.uuid4().hex[:20]}"
        self.send_json({
            "id": completion_id,
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model_name,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }],
            "usage": _usage(usage_prompt, text),
        })

    def _stream_chat(
        self, model_name, model_id, think_mode, extra_fields, messages,
        prompt, full_prompt, images, state, temporary, include_usage,
    ):
        completion_id = f"chatcmpl-{uuid.uuid4().hex[:20]}"
        created = int(time.time())
        self._start_sse()
        self.close_connection = True

        def chunk(delta, finish_reason=None):
            return {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model_name,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
            }

        try:
            self._sse_data(chunk({"role": "assistant", "content": ""}))
            file_refs = _upload_images(images)
            web_stream = None
            use_webapi = (
                CONFIG.get("reuse_upstream_sessions", False)
                and CONFIG.get("upstream_session_backend") == "gemini_webapi"
                and not file_refs
            )
            if use_webapi:
                from .webapi_backend import generate_stream_with_state

                web_stream = generate_stream_with_state(
                    prompt, model_name, state, temporary=temporary
                )
                deltas = web_stream
            else:
                deltas = generate_stream(
                    full_prompt, model_id, think_mode, file_refs, extra_fields, temporary
                )

            text = ""
            emitted = False
            try:
                for delta in self._heartbeat_iter(deltas):
                    if not delta:
                        continue
                    emitted = True
                    text += delta
                    self._sse_data(chunk({"content": delta}))
            except Exception:
                if emitted or web_stream is None or not CONFIG.get("upstream_session_fallback_direct", True):
                    raise
                log("Gemini Web stream failed before output; retrying with the direct transport")
                for delta in self._heartbeat_iter(
                    generate_stream(full_prompt, model_id, think_mode, file_refs, extra_fields, temporary)
                ):
                    if delta:
                        text += delta
                        self._sse_data(chunk({"content": delta}))
                web_stream = None

            if web_stream is not None and web_stream.state:
                self._save_turn(model_name, web_stream.state, messages, text, temporary)
            self._sse_data(chunk({}, "stop"))
            if include_usage:
                self._sse_data({
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model_name,
                    "choices": [],
                    "usage": _usage(prompt, text),
                })
            self._sse_data("[DONE]")
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as exc:
            try:
                self._sse_data({"error": {"message": f"upstream error: {exc}", "type": "upstream_error"}})
                self._sse_data("[DONE]")
            except (BrokenPipeError, ConnectionResetError):
                pass


class ThreadedServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True
