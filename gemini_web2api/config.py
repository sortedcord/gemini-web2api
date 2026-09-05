"""Configuration management."""
import json
import os

DEFAULT_CONFIG = {
    "port": 8081,
    "host": "0.0.0.0",
    "retry_attempts": 3,
    "retry_delay_sec": 2,
    "request_timeout_sec": 180,
    "gemini_bl": "boq_assistant-bard-web-server_20260716.08_p0",
    "auth_user": None,
    "xsrf_token": None,
    "default_model": "gemini-3.6-flash",
    "log_requests": True,
    "cookie_file": None,
    "proxy": None,
    "api_keys": [],
    "temporary_chats": False,
    # Native Gemini continuation state. Disable to retain fully stateless behavior.
    "conversation_state_enabled": False,
    "conversation_store_path": "/data/conversations.db",
    "conversation_ttl_sec": 604800,
    "conversation_max_conversations": 10000,
    "conversation_max_turns_per_conversation": 200,
    "conversation_account_id": "default",
    # Generated image output only; values above the hard safety caps are ignored.
    "generated_image_max_bytes": 10 * 1024 * 1024,
    "generated_image_max_redirects": 3,
    # Set both values to replace temporary Google URLs with durable local URLs.
    "generated_image_store_dir": None,
    "generated_image_base_url": None,
}

CONFIG = dict(DEFAULT_CONFIG)


def load_config(path: str = None):
    """Load config from JSON file."""
    if path and os.path.exists(path):
        with open(path) as f:
            CONFIG.update(json.load(f))
    return CONFIG


def find_config():
    """Search for config file in standard locations."""
    for p in ["./config.json", os.path.expanduser("~/.config/gemini-web2api/config.json")]:
        if os.path.exists(p):
            return p
    return None
