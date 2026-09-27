"""
Общая конфигурация проекта (config.json).

Читается всеми процессами: MCP-серверами (charts, email) и Flet UI.
Секреты здесь не хранятся: пароль SMTP — в переменной окружения SMTP_PASSWORD,
ключ DeepSeek — в DEEPSEEK_API_KEY (экспортируются кодом, см. agent.py / mcp_email.py).
"""

import copy
import json
import os
import threading

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")

DEFAULT_CONFIG = {
    "email": {
        "smtp_host": "smtp.yandex.ru",
        "smtp_port": 465,
        "use_ssl": True,
        "smtp_user": "",
        "from_addr": "",
        "default_to": "",
        "dry_run": True,  # True: письма складываются в outbox/ как .eml, реальной отправки нет
    },
    "charts": {
        "output_dir": "charts",
        "dpi": 120,
    },
    "telegram": {
        "default_to": "",   # username (без @) или числовой chat_id получателя по умолчанию
        "dry_run": True,    # True: сообщения в outbox_telegram/, Bot API не вызывается
        "min_interval_sec": 1.1,  # троттлинг: ~1 сообщение/сек на чат (rate limit Bot API)
    },
}

_lock = threading.Lock()


def _deep_merge(base: dict, override: dict) -> dict:
    for key, value in override.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def load_config() -> dict:
    """Конфигурация = DEFAULT_CONFIG + переопределения из config.json."""
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                user_cfg = json.load(f)
            if isinstance(user_cfg, dict):
                _deep_merge(cfg, user_cfg)
        except (json.JSONDecodeError, OSError):
            pass
    return cfg


def save_config(cfg: dict) -> None:
    with _lock:
        tmp = CONFIG_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CONFIG_FILE)


def charts_dir() -> str:
    path = load_config()["charts"]["output_dir"]
    if not os.path.isabs(path):
        path = os.path.join(BASE_DIR, path)
    os.makedirs(path, exist_ok=True)
    return path


def outbox_dir() -> str:
    path = os.path.join(BASE_DIR, "outbox")
    os.makedirs(path, exist_ok=True)
    return path


def telegram_outbox_dir() -> str:
    path = os.path.join(BASE_DIR, "outbox_telegram")
    os.makedirs(path, exist_ok=True)
    return path
