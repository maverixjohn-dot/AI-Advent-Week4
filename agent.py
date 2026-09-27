"""
MCP-клиент + агентный цикл DeepSeek (function calling), мультисерверная версия.

Архитектура:
    - Несколько MCP-серверов поднимаются как subprocess'ы, транспорт stdio.
      Список серверов — константа SERVERS (конфигурация как код).
    - При старте агент опрашивает каждый сервер через list_tools() и строит
      РЕЕСТР:  видимое_имя_инструмента -> (сервер, оригинальное_имя, сессия).
      Для LLM все инструменты выглядят единым плоским списком; про топологию
      серверов знает только клиент — это и есть маршрутизация.
    - Коллизии имён между серверами разрешаются префиксом "сервер__инструмент".
    - Агентный цикл: LLM сама ВЫБИРАЕТ нужные инструменты под запрос
      (жёсткой цепочки больше нет); порядок и корректность выбора
      проверяются по трассе в logs.json.

Потоковая модель: MCP SDK асинхронный — выделенный фоновый поток
с собственным event loop; публичные методы Agent — синхронные обёртки.
"""

import asyncio
import json
import os
import sys
import threading
import time
from collections import Counter
from contextlib import AsyncExitStack
from datetime import datetime, timezone
from typing import Any, Callable, Optional

# --- Ключ DeepSeek: экспортируется в окружение при старте программы ---------
os.environ.setdefault("DEEPSEEK_API_KEY", "sk-ВСТАВЬТЕ_СЮДА_КЛЮЧ_DEEPSEEK")

from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402
from openai import OpenAI  # noqa: E402

# --- Конфигурация ------------------------------------------------------------
MODEL_NAME = "deepseek-v4-pro"   # с 14.09.2026 маршрутизируется на V4.1-Flash
BASE_URL = "https://api.deepseek.com"
MAX_ITERATIONS = 15              # длинный флоу: до 5 инструментов + запас
SEARCH_MAX_RESULTS = 5

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RESULTS_FILE = os.path.join(BASE_DIR, "results.json")
LOGS_FILE = os.path.join(BASE_DIR, "logs.json")

# Реестр MCP-серверов проекта
SERVERS = [
    {
        "name": "pipeline",
        "script": os.path.join(BASE_DIR, "mcp_server.py"),
        "description": "Поиск, суммаризация, сохранение результатов",
    },
    {
        "name": "charts",
        "script": os.path.join(BASE_DIR, "mcp_charts.py"),
        "description": "Построение графиков (по логам и произвольным данным)",
    },
    {
        "name": "email",
        "script": os.path.join(BASE_DIR, "mcp_email.py"),
        "description": "Отправка результата на почту (SMTP / dry-run)",
    },
    {
        "name": "telegram",
        "script": os.path.join(BASE_DIR, "mcp_telegram.py"),
        "description": "Отправка результата в Telegram (Bot API / dry-run)",
    },
]

# Серверы-уведомления: их ошибки НЕ роняют основной флоу (требование из
# контекстного документа по Telegram), фиксируются в трассе и продолжают цикл
NOTIFICATION_SERVERS = {"email", "telegram"}
# Троттлинг исходящих сообщений Telegram (~1 сообщение/сек на чат, Bot API)
TG_MIN_INTERVAL_SEC = 1.1

STEP_PREVIEW_LIMIT = 1500

SYSTEM_PROMPT = (
    "Ты — оркестратор, работающий с MCP-инструментами, размещёнными на нескольких "
    "серверах. Доступные возможности:\n"
    "- поиск и обработка: search (веб-поиск), summarize (алгоритмическая выжимка "
    "из текста), save_to_file (сохранение результата в JSON);\n"
    "- визуализация: plot_logs_stats (графики статистики прошлых прогонов; данные "
    "инструмент читает сам из logs.json), plot_series (график по произвольному "
    "числовому ряду: title, labels, values);\n"
    "- почта: send_email (отправка письма; может прикрепить файл по пути "
    "attachment_path, например PNG-график);\n"
    "- telegram: send_message (текстовое сообщение), send_photo (изображение по "
    "пути, например chart_path графика), send_document (файл по пути). У всех "
    "трёх параметр to — username или chat_id; если не указан, оставляй пустым.\n\n"
    "Правила:\n"
    "1. Сам выбирай, какие инструменты нужны для запроса пользователя, и порядок "
    "их вызова. Вызывай только необходимое.\n"
    "2. Для исследовательских запросов выполняй цепочку: search -> summarize -> "
    "save_to_file (в data передай {\"query\":..., \"search\":..., \"summary\":...}).\n"
    "3. График по статистике работы системы — plot_logs_stats; график по данным "
    "из найденного результата — извлеки числовой ряд и вызови plot_series.\n"
    "4. Отправка на почту — send_email; если до этого строился график, передай "
    "его путь (chart_path) в attachment_path. Если получатель не указан — не "
    "выдумывай адрес, оставь to пустым.\n"
    "5. Отправка в Telegram: график — send_photo с chart_path, файл — "
    "send_document, итоговый текст — send_message. В Telegram отправляй ТОЛЬКО "
    "итоговое сообщение по завершении флоу, без промежуточных статусов (лимит "
    "~1 сообщение/сек на чат). Если до этого строился график и отправлен "
    "send_photo, дублировать его содержимое длинным send_message не нужно — "
    "хватит короткой подписи.\n"
    "6. Инструменты с разных серверов можно и нужно комбинировать в одном флоу, "
    "если этого требует запрос.\n"
    "После всех вызовов дай краткий итог: что сделано, какие инструменты "
    "использованы, где сохранены результаты."
)


def _append_json_array(path: str, record: dict, lock: threading.Lock) -> None:
    with lock:
        records = []
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                records = data if isinstance(data, list) else []
            except (json.JSONDecodeError, OSError):
                records = []
        records.append(record)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(records, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)


class Agent:
    """Синхронная обёртка над асинхронным мультисерверным MCP-клиентом."""

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever, daemon=True, name="mcp-event-loop"
        )
        self._thread.start()
        self._servers: dict[str, dict] = {}   # имя сервера -> запись подключения
        self._tool_routes: dict[str, tuple] = {}  # видимое имя -> (сервер, ориг. имя)
        self.tools_for_llm: list[dict] = []
        self._log_lock = threading.Lock()
        self._client = OpenAI(
            api_key=os.environ["DEEPSEEK_API_KEY"], base_url=BASE_URL
        )
        self.start()

    # ------------------------------------------------------------------ MCP

    def start(self) -> None:
        fut = asyncio.run_coroutine_threadsafe(self._connect_all(), self._loop)
        fut.result(timeout=120)

    def restart(self, server_name: Optional[str] = None) -> None:
        """Перезапуск одного сервера по имени или всех (server_name=None)."""
        fut = asyncio.run_coroutine_threadsafe(self._restart(server_name), self._loop)
        fut.result(timeout=120)

    async def _restart(self, server_name: Optional[str]) -> None:
        if server_name is None:
            await self._connect_all()
            return
        entry = self._servers.get(server_name)
        if entry is None:
            raise KeyError(f"неизвестный сервер: {server_name}")
        if entry.get("stack") is not None:
            try:
                await entry["stack"].aclose()
            except Exception:
                pass
        self._servers[server_name] = await self._connect_one(entry["spec"])
        self._build_registry()

    async def _connect_all(self) -> None:
        await self._disconnect_all()
        self._servers = {}
        for spec in SERVERS:
            self._servers[spec["name"]] = await self._connect_one(spec)
        self._build_registry()

    async def _connect_one(self, spec: dict) -> dict:
        """Подключение к одному серверу; сбой одного сервера не роняет остальные."""
        stack = AsyncExitStack()
        try:
            # ВАЖНО: MCP SDK по умолчанию НЕ наследует os.environ родителя —
            # subprocess получает только whitelist «безопасных» переменных
            # (get_default_environment). Свои серверы — передаём окружение явно,
            # иначе TELEGRAM_BOT_TOKEN / SMTP_PASSWORD до них не доходят.
            # Для сторонних (community) серверов передавайте whitelist, а не всё.
            params = StdioServerParameters(
                command=sys.executable,
                args=[spec["script"]],
                env=dict(os.environ),
            )
            read, write = await stack.enter_async_context(stdio_client(params))
            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            tools = list((await session.list_tools()).tools)
            return {
                "spec": spec, "stack": stack, "session": session,
                "tools": tools, "connected": True, "error": None,
            }
        except Exception as exc:  # noqa: BLE001
            try:
                await stack.aclose()
            except Exception:
                pass
            return {
                "spec": spec, "stack": None, "session": None,
                "tools": [], "connected": False, "error": str(exc),
            }

    async def _disconnect_all(self) -> None:
        for entry in getattr(self, "_servers", {}).values():
            if entry.get("stack") is not None:
                try:
                    await entry["stack"].aclose()
                except Exception:
                    pass
        self._servers = {}

    def _build_registry(self) -> None:
        """Реестр маршрутизации + плоский список инструментов для LLM."""
        name_count = Counter(
            t.name for entry in self._servers.values() for t in entry["tools"]
        )
        self._tool_routes = {}
        self.tools_for_llm = []
        for srv_name, entry in self._servers.items():
            for tool in entry["tools"]:
                exposed = (
                    tool.name
                    if name_count[tool.name] == 1
                    else f"{srv_name}__{tool.name}"  # коллизия имён -> префикс
                )
                self._tool_routes[exposed] = (srv_name, tool.name)
                self.tools_for_llm.append(self._to_openai_tool(tool, exposed, srv_name))

    @staticmethod
    def _to_openai_tool(tool, exposed_name: str, server_name: str) -> dict:
        schema = tool.inputSchema or {"type": "object", "properties": {}}
        if schema.get("type") == "object" and "properties" not in schema:
            schema = {**schema, "properties": {}}
        return {
            "type": "function",
            "function": {
                "name": exposed_name,
                "description": f"[сервер: {server_name}] {tool.description or ''}",
                "parameters": schema,
            },
        }

    def status(self) -> dict:
        """Статус по каждому серверу: соединение + инструменты со схемами."""
        servers = []
        for name, entry in self._servers.items():
            servers.append(
                {
                    "name": name,
                    "description": entry["spec"]["description"],
                    "connected": entry["connected"],
                    "error": entry.get("error"),
                    "tools": [
                        {
                            "name": t.name,
                            "description": t.description or "",
                            "schema": t.inputSchema,
                        }
                        for t in entry["tools"]
                    ],
                }
            )
        return {
            "connected": bool(servers) and all(s["connected"] for s in servers),
            "servers": servers,
            "tools_total": sum(len(s["tools"]) for s in servers),
        }

    def call_tool(self, exposed_name: str, args: dict) -> str:
        """Прямой вызов инструмента через реестр (тесты/отладка, без LLM)."""
        fut = asyncio.run_coroutine_threadsafe(
            self._call_tool(exposed_name, args), self._loop
        )
        return fut.result(timeout=120)

    async def _call_tool(self, exposed_name: str, args: dict) -> str:
        route = self._tool_routes.get(exposed_name)
        if route is None:
            raise KeyError(f"инструмент не найден в реестре: {exposed_name}")
        srv_name, orig_name = route
        session = self._servers[srv_name]["session"]
        if session is None:
            raise RuntimeError(f"сервер '{srv_name}' не подключён")
        if srv_name == "telegram":
            await self._throttle_telegram()
        result = await session.call_tool(orig_name, args)
        text = "\n".join(getattr(c, "text", str(c)) for c in result.content)
        if result.isError:
            raise RuntimeError(f"инструмент {exposed_name} вернул ошибку: {text}")
        return text

    async def _throttle_telegram(self) -> None:
        """Не чаще ~1 сообщения/сек на чат (rate limit Bot API, см. контекстный документ)."""
        last = getattr(self, "_tg_last_call", 0.0)
        wait = TG_MIN_INTERVAL_SEC - (time.monotonic() - last)
        if wait > 0:
            await asyncio.sleep(wait)
        self._tg_last_call = time.monotonic()

    # -------------------------------------------------------------- pipeline

    def run_pipeline(
        self,
        query: str,
        on_step: Optional[Callable[[dict], None]] = None,
    ) -> dict:
        fut = asyncio.run_coroutine_threadsafe(
            self._run(query, on_step), self._loop
        )
        return fut.result()

    async def _run(
        self,
        query: str,
        on_step: Optional[Callable[[dict], None]],
    ) -> dict:
        steps: list[dict] = []

        def log(kind: str, name: str = "", payload: Any = None, server: str = "") -> None:
            if isinstance(payload, str) and len(payload) > STEP_PREVIEW_LIMIT:
                payload = payload[:STEP_PREVIEW_LIMIT] + " …[обрезано]"
            rec = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "kind": kind,
                "name": name,
                "server": server,
                "payload": payload,
            }
            steps.append(rec)
            if on_step is not None:
                on_step(rec)

        started = time.time()
        status, final_answer, error = "success", "", None
        try:
            if not self._tool_routes:
                raise RuntimeError("нет подключённых MCP-серверов / инструментов")
            log("info", "pipeline", f"Старт флоу, запрос: {query}")
            messages: list[dict] = [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": query},
            ]
            for _ in range(MAX_ITERATIONS):
                response = await asyncio.to_thread(
                    self._client.chat.completions.create,
                    model=MODEL_NAME,
                    messages=messages,
                    tools=self.tools_for_llm,
                )
                message = response.choices[0].message

                if not message.tool_calls:
                    final_answer = message.content or ""
                    log("final", "llm", final_answer)
                    break

                messages.append(message)

                for tc in message.tool_calls:
                    exposed_name = tc.function.name
                    try:
                        args = json.loads(tc.function.arguments or "{}")
                    except json.JSONDecodeError:
                        args = {}
                    srv_name = self._tool_routes.get(exposed_name, ("?", ""))[0]
                    log("tool_call", exposed_name,
                        json.dumps(args, ensure_ascii=False), server=srv_name)

                    # маршрутизация вызова на нужный сервер через реестр
                    try:
                        text = await self._call_tool(exposed_name, args)
                    except Exception as tool_exc:  # noqa: BLE001
                        if srv_name not in NOTIFICATION_SERVERS:
                            raise
                        # ошибка уведомления некритична: фиксируем и продолжаем флоу
                        text = f"ОШИБКА отправки (некритично, флоу продолжен): {tool_exc}"
                        log("error", exposed_name, str(tool_exc), server=srv_name)
                    else:
                        log("tool_result", exposed_name, text, server=srv_name)

                    messages.append(
                        {"role": "tool", "tool_call_id": tc.id, "content": text}
                    )
            else:
                status, error = "error", "Превышен лимит итераций агентного цикла"
                log("error", "pipeline", error)
        except Exception as exc:  # noqa: BLE001
            status, error = "error", str(exc)
            log("error", "pipeline", error)

        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "query": query,
            "status": status,
            "duration_sec": round(time.time() - started, 2),
            "steps": steps,
            "final_answer": final_answer,
            "error": error,
        }
        _append_json_array(LOGS_FILE, record, self._log_lock)
        return {
            "status": status,
            "answer": final_answer,
            "error": error,
            "steps": steps,
        }

    # --------------------------------------------------------------- cleanup

    def shutdown(self) -> None:
        fut = asyncio.run_coroutine_threadsafe(self._disconnect_all(), self._loop)
        try:
            fut.result(timeout=10)
        except Exception:
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)
