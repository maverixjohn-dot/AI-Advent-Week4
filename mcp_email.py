"""
MCP-сервер «email» — отправка результата на почту.

Инструмент:
    send_email — отправляет письмо через SMTP, опционально с вложением
                 (например, PNG-графиком, построенным сервером «charts»).

Режим dry-run (по умолчанию, "dry_run": true в config.json):
    реальной отправки нет, письмо со вложением сохраняется в outbox/ как .eml.
    Позволяет проверить весь флоу end-to-end без боевого SMTP-аккаунта.

Секреты: пароль SMTP — в переменной окружения SMTP_PASSWORD
(по ТЗ экспортируется кодом ниже). Для Gmail/Яндекса нужен пароль приложения,
не основной пароль аккаунта.

Запуск: python mcp_email.py  (транспорт stdio; поднимается клиентом из agent.py)
"""

import mimetypes
import os
import smtplib
from datetime import datetime
from email.message import EmailMessage

# --- Пароль SMTP: экспортируется в окружение при старте (паттерн как у DeepSeek) ---
os.environ.setdefault("SMTP_PASSWORD", "ВСТАВЬТЕ_ПАРОЛЬ_ПРИЛОЖЕНИЯ")

from mcp.server.fastmcp import FastMCP  # noqa: E402

from config import load_config, outbox_dir  # noqa: E402

mcp = FastMCP("email")


def _build_message(cfg: dict, to: str, subject: str, body: str, attachment_path: str | None) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = cfg.get("from_addr") or cfg.get("smtp_user") or "mcp-pipeline@localhost"
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)

    if attachment_path:
        if not os.path.exists(attachment_path):
            raise ValueError(f"вложение не найдено: {attachment_path}")
        ctype, _ = mimetypes.guess_type(attachment_path)
        maintype, subtype = (ctype or "application/octet-stream").split("/", 1)
        with open(attachment_path, "rb") as f:
            msg.add_attachment(
                f.read(),
                maintype=maintype,
                subtype=subtype,
                filename=os.path.basename(attachment_path),
            )
    return msg


@mcp.tool()
def send_email(
    subject: str,
    body: str,
    to: str = "",
    attachment_path: str = "",
) -> dict:
    """Отправляет письмо с результатом через SMTP.

    to — адрес получателя; если пустой, берётся default_to из конфигурации.
    attachment_path — путь к файлу-вложению (например, chart_path из plot_*);
    пустая строка — без вложения.
    В режиме dry_run письмо не отправляется, а сохраняется в outbox/ как .eml.
    """
    cfg = load_config()["email"]
    recipient = to.strip() or cfg.get("default_to", "").strip()
    if not recipient:
        return {"error": "не указан получатель: передайте 'to' или задайте default_to в config.json"}

    attachment = attachment_path.strip() or None
    msg = _build_message(cfg, recipient, subject, body, attachment)

    if cfg.get("dry_run", True):
        filename = f"mail_{datetime.now().strftime('%Y%m%d_%H%M%S')}.eml"
        path = os.path.join(outbox_dir(), filename)
        with open(path, "wb") as f:
            f.write(bytes(msg))
        return {
            "dry_run": True,
            "saved_to": path,
            "to": recipient,
            "subject": subject,
            "attachment": attachment or None,
            "note": "реальная отправка отключена (dry_run=true в config.json)",
        }

    password = os.environ.get("SMTP_PASSWORD", "")
    if not password or password.startswith("ВСТАВЬТЕ"):
        return {"error": "SMTP_PASSWORD не задан (dry_run=false, а пароль отсутствует)"}

    host = cfg["smtp_host"]
    port = int(cfg.get("smtp_port", 465))
    if cfg.get("use_ssl", True):
        server = smtplib.SMTP_SSL(host, port, timeout=30)
    else:
        server = smtplib.SMTP(host, port, timeout=30)
        server.starttls()
    try:
        if cfg.get("smtp_user"):
            server.login(cfg["smtp_user"], password)
        server.send_message(msg)
    finally:
        server.quit()

    return {
        "dry_run": False,
        "sent": True,
        "to": recipient,
        "subject": subject,
        "attachment": attachment or None,
    }


if __name__ == "__main__":
    mcp.run(transport="stdio")
