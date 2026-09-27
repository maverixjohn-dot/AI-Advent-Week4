"""
MCP-сервер «telegram» — отправка результата через Telegram-бота (Bot API).

Инструменты (поверхность идентична community-серверу telegram-notify-mcp,
поэтому его можно подменить тем сервером заменой одной записи в SERVERS):
    send_message      — текстовое сообщение
    send_photo        — фото/график по абсолютному пути (PNG из сервера «charts»)
    send_document     — файл-документ по абсолютному пути
    check_connection  — диагностика: токен, getMe, резолв получателя

Отличия от community-сервера (осознанные):
    - получатель — параметр `to` на каждый вызов (username или числовой chat_id),
      а не фиксированный env; пустой `to` -> default_to из config.json;
    - режим dry-run: сообщения и файлы складываются в outbox_telegram/,
      Bot API не вызывается — весь флоу проверяется без бота.

Секреты: TELEGRAM_BOT_TOKEN — в переменной окружения (экспортируется кодом ниже).
Prerequisites боевого режима (ограничения Bot API, обхода нет):
    1. Бот создан через @BotFather, токен получен.
    2. У получателя задан username в настройках Telegram.
    3. Получатель нажал /start боту — иначе резолв username через getUpdates
       не найдёт чат (бот не может писать первым).

Запуск: python mcp_telegram.py  (транспорт stdio; поднимается клиентом из agent.py)
"""

import json
import os
import shutil
import time
from datetime import datetime

import httpx

# --- Токен бота: экспортируется в окружение при старте (паттерн как у DeepSeek) ---
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "ВСТАВЬТЕ_ТОКЕН_БОТА")

from mcp.server.fastmcp import FastMCP  # noqa: E402

from config import load_config, telegram_outbox_dir  # noqa: E402

API_BASE = "https://api.telegram.org"
MAX_RETRIES = 3

mcp = FastMCP("telegram")

# кеш username -> chat_id на время жизни процесса (как у community-сервера)
_chat_cache: dict[str, int] = {}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _cfg() -> dict:
    return load_config()["telegram"]


def _token() -> str:
    return os.environ.get("TELEGRAM_BOT_TOKEN", "")


def _token_ok() -> bool:
    t = _token()
    return bool(t) and not t.startswith("ВСТАВЬТЕ") and ":" in t


def _resolve_recipient(to: str) -> str:
    recipient = (to or "").strip().lstrip("@") or _cfg().get("default_to", "").strip().lstrip("@")
    if not recipient:
        raise ValueError(
            "не указан получатель: передайте 'to' (username или chat_id) "
            "или задайте telegram.default_to в config.json"
        )
    return recipient


def _api_call(client: httpx.Client, token: str, method: str, **kwargs) -> dict:
    """POST к Bot API с retry на 429 (retry_after) — требование из контекстного документа."""
    url = f"{API_BASE}/bot{token}/{method}"
    for attempt in range(MAX_RETRIES):
        resp = client.post(url, **kwargs)
        if resp.status_code == 429:
            retry_after = resp.json().get("parameters", {}).get("retry_after", 2)
            if attempt < MAX_RETRIES - 1:
                time.sleep(float(retry_after) + 0.5)
                continue
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"Bot API {method}: {data.get('description', resp.text)}")
        return data["result"]
    raise RuntimeError(f"Bot API {method}: превышено число retry (429)")


def _resolve_chat_id(client: httpx.Client, token: str, recipient: str) -> int:
    """Числовой chat_id используется как есть; username резолвится через getUpdates."""
    if recipient.lstrip("-").isdigit():
        return int(recipient)
    if recipient in _chat_cache:
        return _chat_cache[recipient]
    updates = _api_call(client, token, "getUpdates", json={"limit": 100})
    for upd in updates:
        msg = upd.get("message") or upd.get("channel_post") or {}
        user = msg.get("from") or {}
        if (user.get("username") or "").lower() == recipient.lower():
            chat_id = msg["chat"]["id"]
            _chat_cache[recipient] = chat_id
            return chat_id
    raise RuntimeError(
        f"чат с @{recipient} не найден в getUpdates. "
        f"Проверьте: у пользователя задан username и он нажал /start боту "
        f"(бот не может инициировать диалог первым — ограничение Bot API)"
    )


def _save_dry_run(kind: str, recipient: str, payload: dict, attachment: str = "") -> dict:
    record = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "kind": kind,
        "to": recipient,
        **payload,
    }
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    out_dir = telegram_outbox_dir()
    if attachment:
        copied = os.path.join(out_dir, f"{stamp}_{os.path.basename(attachment)}")
        shutil.copy2(attachment, copied)
        record["attachment_saved_to"] = copied
    path = os.path.join(out_dir, f"tg_{kind}_{stamp}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(record, f, ensure_ascii=False, indent=2)
    return {
        "dry_run": True,
        "saved_to": path,
        "to": recipient,
        "kind": kind,
        "note": "реальная отправка отключена (telegram.dry_run=true в config.json)",
    }


def _check_file(path: str, kind: str) -> str:
    if not path or not os.path.exists(path):
        raise ValueError(f"файл для {kind} не найден: {path!r}")
    size_mb = os.path.getsize(path) / (1024 * 1024)
    limit = 10 if kind == "send_photo" else 50  # лимиты Bot API
    if size_mb > limit:
        raise ValueError(f"файл {size_mb:.1f} МБ превышает лимит Bot API ({limit} МБ)")
    return path


# ---------------------------------------------------------------------------
# tools
# ---------------------------------------------------------------------------

@mcp.tool()
def send_message(text: str, to: str = "", parse_mode: str = "") -> dict:
    """Отправляет текстовое сообщение в Telegram.

    to — username (с @ или без) или числовой chat_id; пустой — default_to из конфига.
    parse_mode — "HTML" или "MarkdownV2"; пустой — обычный текст (рекомендуется,
    LLM-вывод часто ломает разметку).
    """
    recipient = _resolve_recipient(to)
    if _cfg().get("dry_run", True):
        return _save_dry_run("message", recipient, {"text": text, "parse_mode": parse_mode})

    if not _token_ok():
        return {"error": "TELEGRAM_BOT_TOKEN не задан (dry_run=false, а токен отсутствует)"}
    with httpx.Client(timeout=30) as client:
        chat_id = _resolve_chat_id(client, _token(), recipient)
        payload = {"chat_id": chat_id, "text": text}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        try:
            _api_call(client, _token(), "sendMessage", json=payload)
        except RuntimeError:
            if not parse_mode:
                raise
            # разметка сломалась — повторяем обычным текстом
            payload.pop("parse_mode")
            _api_call(client, _token(), "sendMessage", json=payload)
    return {"sent": True, "to": recipient, "kind": "message"}


@mcp.tool()
def send_photo(photo: str, caption: str = "", to: str = "") -> dict:
    """Отправляет фото/изображение по абсолютному пути (например, PNG-график
    из plot_logs_stats/plot_series — передайте chart_path). Лимит Bot API: 10 МБ."""
    recipient = _resolve_recipient(to)
    _check_file(photo, "send_photo")
    if _cfg().get("dry_run", True):
        return _save_dry_run("photo", recipient, {"caption": caption}, attachment=photo)

    if not _token_ok():
        return {"error": "TELEGRAM_BOT_TOKEN не задан (dry_run=false, а токен отсутствует)"}
    with httpx.Client(timeout=60) as client:
        chat_id = _resolve_chat_id(client, _token(), recipient)
        with open(photo, "rb") as f:
            _api_call(
                client, _token(), "sendPhoto",
                data={"chat_id": str(chat_id), "caption": caption},
                files={"photo": (os.path.basename(photo), f)},
            )
    return {"sent": True, "to": recipient, "kind": "photo", "file": photo}


@mcp.tool()
def send_document(document: str, caption: str = "", to: str = "") -> dict:
    """Отправляет файл-документ по абсолютному пути (отчёты, results.json и т.п.).
    Лимит Bot API: 50 МБ."""
    recipient = _resolve_recipient(to)
    _check_file(document, "send_document")
    if _cfg().get("dry_run", True):
        return _save_dry_run("document", recipient, {"caption": caption}, attachment=document)

    if not _token_ok():
        return {"error": "TELEGRAM_BOT_TOKEN не задан (dry_run=false, а токен отсутствует)"}
    with httpx.Client(timeout=60) as client:
        chat_id = _resolve_chat_id(client, _token(), recipient)
        with open(document, "rb") as f:
            _api_call(
                client, _token(), "sendDocument",
                data={"chat_id": str(chat_id), "caption": caption},
                files={"document": (os.path.basename(document), f)},
            )
    return {"sent": True, "to": recipient, "kind": "document", "file": document}


@mcp.tool()
def check_connection(to: str = "") -> dict:
    """Диагностика настройки Telegram-канала (setup-чеклист из админки).

    Проверяет: задан ли токен, отвечает ли бот (getMe), резолвится ли получатель.
    В dry-run возвращает статус конфигурации без обращения к Bot API.
    """
    cfg = _cfg()
    result = {
        "dry_run": cfg.get("dry_run", True),
        "token_configured": _token_ok(),
        "default_to": cfg.get("default_to") or None,
    }
    if cfg.get("dry_run", True):
        result["note"] = "dry-run: Bot API не вызывается; боевые проверки (getMe, резолв чата) пропущены"
        return result
    if not _token_ok():
        result["error"] = "TELEGRAM_BOT_TOKEN не задан — впишите токен в mcp_telegram.py"
        return result
    try:
        with httpx.Client(timeout=30) as client:
            me = _api_call(client, _token(), "getMe", json={})
            result["bot"] = f"@{me.get('username')}"
            recipient = _resolve_recipient(to)
            if not recipient.lstrip("-").isdigit():
                chat_id = _resolve_chat_id(client, _token(), recipient)
                result["recipient_chat_id"] = chat_id
            result["recipient_ok"] = True
    except Exception as exc:  # noqa: BLE001
        result["error"] = str(exc)
        result["recipient_ok"] = False
    return result


if __name__ == "__main__":
    mcp.run(transport="stdio")
