"""Configuration management for the chat-only service."""
import json
import os


DEFAULT_CONFIG = {
    "port": 8081,
    "host": "0.0.0.0",
    "retry_attempts": 3,
    "retry_delay_sec": 2,
    "request_timeout_sec": 180,
    "gemini_bl": "boq_assistant-bard-web-server_20260525.09_p0",
    "auth_user": None,
    "xsrf_token": None,
    "default_model": "gemini-3.5-flash",
    "log_requests": True,
    "cookie_file": None,
    "proxy": None,
    "api_keys": [],
    "conversation_store_path": "conversations.db",
    "conversation_store_ttl_sec": 86400,
    "conversation_store_max_rows": 2000,
    "media_store_path": "/app/data/media",
    "media_store_ttl_sec": 86400,
    "media_store_max_files": 500,
    "public_base_url": None,
    "max_history_messages": 60,
    "max_history_chars": 80000,
    "max_request_body_bytes": 20 * 1024 * 1024,
    "continuation_attempts": 2,
    "continuation_context_chars": 16000,
    "sse_heartbeat_sec": 10,
    "reuse_upstream_sessions": False,
    "upstream_session_backend": "gemini_webapi",
    "upstream_session_fallback_direct": True,
    "cookie_cache_path": "/app/data/gemini_cookies",
    "cookie_auto_refresh": True,
    "cookie_refresh_interval_sec": 600,
    "webapi_watchdog_sec": 120,
    "webapi_request_timeout_sec": 180,
    "temporary_background_tasks": True,
    "require_authenticated_webapi": True,
    "webapi_allow_unverified_account": False,
}

CONFIG = dict(DEFAULT_CONFIG)


def load_config(path: str = None):
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as file:
            CONFIG.update(json.load(file))
    return CONFIG


def find_config():
    for path in ["./config.json", os.path.expanduser("~/.config/chatgemini/config.json")]:
        if os.path.exists(path):
            return path
    return None
