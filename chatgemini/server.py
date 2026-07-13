"""Small OpenAI-compatible HTTP server for ordinary chat only."""
import json
import mimetypes
import os
import queue
import re
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from urllib.parse import urlsplit

from . import __version__
from .config import CONFIG
from .gemini import generate_stream, generate_with_state, log
from .messages import (
    compact_messages,
    google_request_to_messages,
    messages_to_prompt,
    messages_to_web_prompt,
    serializable_messages,
)
from .media import MEDIA_PLACEHOLDER
from .models import MODELS, resolve_model
from .multimodal import fetch_image_bytes, upload_image
from .richtext import RichTextSanitizer, sanitize_rich_text
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
        supplied_key = bearer or self.headers.get("x-api-key", "") or self.headers.get("x-goog-api-key", "")
        return supplied_key in keys

    def send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_google_error(self, message, status=400):
        google_status = {
            400: "INVALID_ARGUMENT",
            401: "UNAUTHENTICATED",
            404: "NOT_FOUND",
            413: "RESOURCE_EXHAUSTED",
            502: "UNAVAILABLE",
            500: "INTERNAL",
        }.get(status, "UNKNOWN")
        self.send_json({"error": {
            "code": status,
            "message": message,
            "status": google_status,
        }}, status)

    def _public_base_url(self):
        configured = str(CONFIG.get("public_base_url") or "").strip().rstrip("/")
        if configured.startswith(("http://", "https://")):
            return configured
        forwarded_proto = self.headers.get("X-Forwarded-Proto", "").split(",", 1)[0].strip()
        scheme = forwarded_proto if forwarded_proto in ("http", "https") else "http"
        forwarded_host = self.headers.get("X-Forwarded-Host", "").split(",", 1)[0].strip()
        host = forwarded_host or self.headers.get("Host", "")
        if not re.fullmatch(r"[A-Za-z0-9.:[\]-]+", host or ""):
            host = f"127.0.0.1:{CONFIG.get('port', 8081)}"
        return f"{scheme}://{host}"

    def _render_media_urls(self, text):
        if not isinstance(text, str) or MEDIA_PLACEHOLDER not in text:
            return text
        return text.replace(MEDIA_PLACEHOLDER, self._public_base_url() + "/media/")

    def _render_output_text(self, text):
        return sanitize_rich_text(self._render_media_urls(text))

    def _serve_media(self):
        filename = self.route[len("/media/"):]
        if not re.fullmatch(r"[A-Fa-f0-9]{32}\.[A-Za-z0-9]{1,8}", filename):
            self.send_json({"error": {"message": "not found", "type": "not_found_error"}}, 404)
            return
        directory = os.path.abspath(CONFIG.get("media_store_path") or "/app/data/media")
        path = os.path.abspath(os.path.join(directory, filename))
        if os.path.dirname(path) != directory or not os.path.isfile(path):
            self.send_json({"error": {"message": "not found", "type": "not_found_error"}}, 404)
            return
        try:
            size = os.path.getsize(path)
            self.send_response(200)
            self.send_header("Content-Type", mimetypes.guess_type(path)[0] or "application/octet-stream")
            self.send_header("Content-Length", str(size))
            self.send_header("Cache-Control", "private, max-age=86400, immutable")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            with open(path, "rb") as file:
                while True:
                    chunk = file.read(64 * 1024)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except (OSError, BrokenPipeError, ConnectionResetError):
            return

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

    def _prepare_messages(self, raw_messages: list, model_name: str):
        messages = compact_messages(
            raw_messages or [],
            int(CONFIG.get("max_history_messages", 60) or 60),
            int(CONFIG.get("max_history_chars", 80000) or 80000),
        )
        full_prompt, images = messages_to_prompt(messages)
        web_prompt, _ = messages_to_web_prompt(messages)
        stored_messages = serializable_messages(messages)
        temporary = _is_background_request(full_prompt)
        prefer_web_session = bool(
            CONFIG.get("reuse_upstream_sessions", False)
            and CONFIG.get("upstream_session_backend") == "gemini_webapi"
            and not images
            and not temporary
        )
        prompt = web_prompt if prefer_web_session else full_prompt
        upstream_state = None
        resumed = False

        if CONFIG.get("reuse_upstream_sessions", False) and not images and not temporary:
            try:
                session = _store().find(model_name, stored_messages)
                if session:
                    render_prompt = messages_to_web_prompt if prefer_web_session else messages_to_prompt
                    delta_prompt, _ = render_prompt(session["delta_messages"])
                    if delta_prompt.strip():
                        prompt = delta_prompt
                        upstream_state = session["upstream_state"]
                        resumed = True
            except Exception as exc:
                log(f"Conversation cache lookup failed; using full history: {exc}")
        return messages, stored_messages, prompt, full_prompt, images, upstream_state, temporary, resumed

    def _prepare_chat(self, request: dict, model_name: str):
        return self._prepare_messages(request.get("messages") or [], model_name)

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
        if self.route.startswith("/media/"):
            self._serve_media()
            return
        if self.route.startswith(("/v1/", "/v1beta/")) and not self._authorized():
            self.close_connection = True
            if self.route.startswith("/v1beta/"):
                self.send_google_error("invalid api key", 401)
            else:
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
        elif self.route == "/v1beta/models":
            self.send_json({
                "models": [
                    {
                        "name": f"models/{name}",
                        "version": "chatgemini-1",
                        "displayName": name,
                        "description": config["desc"],
                        "inputTokenLimit": int(CONFIG.get("max_history_chars", 80000) or 80000) // 4,
                        "outputTokenLimit": 20000,
                        "supportedGenerationMethods": ["generateContent", "streamGenerateContent"],
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
        is_google_route = self.route.startswith("/v1beta/")
        if self.route.startswith(("/v1/", "/v1beta/")) and not self._authorized():
            self.close_connection = True
            if is_google_route:
                self.send_google_error("invalid api key", 401)
            else:
                self.send_json({"error": {"message": "invalid api key", "type": "authentication_error"}}, 401)
            return
        google_operation = None
        google_model = None
        if is_google_route and self.route.startswith("/v1beta/models/"):
            model_and_operation = self.route[len("/v1beta/models/"):]
            google_model, separator, google_operation = model_and_operation.partition(":")
            if not separator or google_operation not in ("generateContent", "streamGenerateContent"):
                google_operation = None
        if self.route != "/v1/chat/completions" and not google_operation:
            # The body belongs to an unsupported endpoint. Closing prevents an
            # HTTP/1.1 keep-alive parser from treating it as a second request.
            self.close_connection = True
            if is_google_route:
                self.send_google_error("not found", 404)
            else:
                self.send_json({"error": {"message": "not found", "type": "not_found_error"}}, 404)
            return

        try:
            length = int(self.headers.get("Content-Length", "0") or 0)
        except ValueError:
            if is_google_route:
                self.send_google_error("invalid Content-Length")
            else:
                self.send_json({"error": {"message": "invalid Content-Length", "type": "invalid_request_error"}}, 400)
            return
        limit = int(CONFIG.get("max_request_body_bytes", 20 * 1024 * 1024) or 0)
        if limit > 0 and length > limit:
            if is_google_route:
                self.send_google_error("request body too large", 413)
            else:
                self.send_json({"error": {"message": "request body too large", "type": "invalid_request_error"}}, 413)
            return
        body = self.rfile.read(length) if length else b""
        try:
            request = json.loads(body)
        except (TypeError, ValueError):
            if is_google_route:
                self.send_google_error("invalid JSON")
            else:
                self.send_json({"error": {"message": "invalid JSON", "type": "invalid_request_error"}}, 400)
            return
        try:
            if google_operation:
                self._handle_google_chat(request, google_model, google_operation == "streamGenerateContent")
            else:
                self._handle_chat(request)
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as exc:
            log(f"Unexpected {'Google' if is_google_route else 'OpenAI'} chat request error: {exc}")
            if is_google_route:
                self.send_google_error("internal server error", 500)
            else:
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

    def _handle_google_chat(self, request: dict, requested_model: str, stream: bool):
        if not isinstance(request, dict):
            self.send_google_error("request body must be an object")
            return
        model_name, model_id, think_mode, error, extra_fields = resolve_model(
            requested_model or CONFIG["default_model"]
        )
        if error:
            self.send_google_error(error)
            return

        prepared = self._prepare_messages(google_request_to_messages(request), model_name)
        messages, stored_messages, prompt, full_prompt, images, state, temporary, resumed = prepared
        if not prompt.strip() and not images:
            self.send_google_error("contents must include text or image data")
            return
        ignored_tools = bool(request.get("tools") or request.get("toolConfig"))
        log(
            f"Google text request: model={model_name} stream={stream} "
            f"temporary={temporary} resumed={resumed} ignored_tools={ignored_tools} "
            f"prompt_len={len(prompt)} fallback_len={len(full_prompt)}"
        )
        if stream:
            self._stream_google_chat(
                model_name, model_id, think_mode, extra_fields, stored_messages,
                prompt, full_prompt, images, state, temporary,
            )
        else:
            self._complete_google_chat(
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

        text = self._render_output_text(text)
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

    def _complete_google_chat(
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
            self.send_google_error(f"upstream error: {exc}", 502)
            return

        text = self._render_output_text(text)
        self._save_turn(model_name, state, messages, text, temporary)
        usage = _usage(usage_prompt, text)
        self.send_json({
            "candidates": [{
                "index": 0,
                "content": {"role": "model", "parts": [{"text": text}]},
                "finishReason": "STOP",
            }],
            "usageMetadata": {
                "promptTokenCount": usage["prompt_tokens"],
                "candidatesTokenCount": usage["completion_tokens"],
                "totalTokenCount": usage["total_tokens"],
            },
            "modelVersion": model_name,
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
            stateful_stream = None
            use_webapi = (
                CONFIG.get("reuse_upstream_sessions", False)
                and CONFIG.get("upstream_session_backend") == "gemini_webapi"
                and not file_refs
            )
            if use_webapi:
                from .webapi_backend import generate_stream_with_state

                stateful_stream = generate_stream_with_state(
                    prompt, model_name, state, temporary=temporary
                )
                deltas = stateful_stream
            else:
                stateful_stream = generate_stream(
                    prompt, model_id, think_mode, file_refs, extra_fields, temporary,
                    conversation=state,
                    fallback_prompt=full_prompt,
                )
                deltas = stateful_stream

            text = ""
            emitted = False
            sanitizer = RichTextSanitizer()
            try:
                for delta in self._heartbeat_iter(deltas):
                    if not delta:
                        continue
                    delta = sanitizer.feed(self._render_media_urls(delta))
                    if not delta:
                        continue
                    emitted = True
                    text += delta
                    self._sse_data(chunk({"content": delta}))
            except Exception:
                if emitted or not use_webapi or not CONFIG.get("upstream_session_fallback_direct", True):
                    raise
                log("Gemini Web stream failed before output; retrying with the direct transport")
                stateful_stream = generate_stream(
                    full_prompt, model_id, think_mode, file_refs, extra_fields, temporary,
                    fallback_prompt=full_prompt,
                )
                sanitizer = RichTextSanitizer()
                for delta in self._heartbeat_iter(stateful_stream):
                    if delta:
                        delta = sanitizer.feed(self._render_media_urls(delta))
                        if not delta:
                            continue
                        text += delta
                        self._sse_data(chunk({"content": delta}))

            final_delta = sanitizer.feed(final=True)
            if final_delta:
                emitted = True
                text += final_delta
                self._sse_data(chunk({"content": final_delta}))

            if stateful_stream is not None and getattr(stateful_stream, "state", None):
                self._save_turn(model_name, stateful_stream.state, messages, text, temporary)
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

    def _stream_google_chat(
        self, model_name, model_id, think_mode, extra_fields, messages,
        prompt, full_prompt, images, state, temporary,
    ):
        self._start_sse()
        self.close_connection = True
        file_refs = _upload_images(images)
        stateful_stream = None
        try:
            use_webapi = (
                CONFIG.get("reuse_upstream_sessions", False)
                and CONFIG.get("upstream_session_backend") == "gemini_webapi"
                and not file_refs
            )
            if use_webapi:
                from .webapi_backend import generate_stream_with_state

                stateful_stream = generate_stream_with_state(
                    prompt, model_name, state, temporary=temporary
                )
                deltas = stateful_stream
            else:
                stateful_stream = generate_stream(
                    prompt, model_id, think_mode, file_refs, extra_fields, temporary,
                    conversation=state,
                    fallback_prompt=full_prompt,
                )
                deltas = stateful_stream

            text = ""
            emitted = False
            sanitizer = RichTextSanitizer()
            try:
                for delta in self._heartbeat_iter(deltas):
                    if not delta:
                        continue
                    delta = sanitizer.feed(self._render_media_urls(delta))
                    if not delta:
                        continue
                    emitted = True
                    text += delta
                    self._sse_data({"candidates": [{
                        "index": 0,
                        "content": {"role": "model", "parts": [{"text": delta}]},
                    }], "modelVersion": model_name})
            except Exception:
                if emitted or not use_webapi or not CONFIG.get("upstream_session_fallback_direct", True):
                    raise
                log("Gemini Web stream failed before output; retrying with the direct transport")
                stateful_stream = generate_stream(
                    full_prompt, model_id, think_mode, file_refs, extra_fields, temporary,
                    fallback_prompt=full_prompt,
                )
                sanitizer = RichTextSanitizer()
                for delta in self._heartbeat_iter(stateful_stream):
                    if delta:
                        delta = sanitizer.feed(self._render_media_urls(delta))
                        if not delta:
                            continue
                        text += delta
                        self._sse_data({"candidates": [{
                            "index": 0,
                            "content": {"role": "model", "parts": [{"text": delta}]},
                        }], "modelVersion": model_name})

            final_delta = sanitizer.feed(final=True)
            if final_delta:
                emitted = True
                text += final_delta
                self._sse_data({"candidates": [{
                    "index": 0,
                    "content": {"role": "model", "parts": [{"text": final_delta}]},
                }], "modelVersion": model_name})

            if stateful_stream is not None and getattr(stateful_stream, "state", None):
                self._save_turn(model_name, stateful_stream.state, messages, text, temporary)
            usage = _usage(prompt, text)
            self._sse_data({
                "candidates": [{
                    "index": 0,
                    "content": {"role": "model", "parts": []},
                    "finishReason": "STOP",
                }],
                "usageMetadata": {
                    "promptTokenCount": usage["prompt_tokens"],
                    "candidatesTokenCount": usage["completion_tokens"],
                    "totalTokenCount": usage["total_tokens"],
                },
                "modelVersion": model_name,
            })
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as exc:
            try:
                self._sse_data({"error": {
                    "code": 502,
                    "message": f"upstream error: {exc}",
                    "status": "UNAVAILABLE",
                }})
            except (BrokenPipeError, ConnectionResetError):
                pass


class ThreadedServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True
