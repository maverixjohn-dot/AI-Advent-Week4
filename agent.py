"""
MCP-клиент + агентный цикл DeepSeek (function calling).

Архитектура:
    - MCP-сервер (mcp_server.py) поднимается как subprocess, транспорт stdio.
    - Схемы инструментов получаются через MCP list_tools() и конвертируются
      в формат tools OpenAI (DeepSeek API OpenAI-совместим).
    - Агентный цикл: LLM сам решает, какой инструмент вызвать; системный
      промпт предписывает цепочку search -> summarize -> save_to_file.
    - Трасса каждого прогона пишется в logs.json.

Потоковая модель: MCP SDK асинхронный, поэтому выделенный фоновый поток
с собственным event loop; публичные методы Agent — синхронные обёртки.
"""

import asyncio
import json
import os
import sys
import threading
import time
from contextlib import AsyncExitStack
from datetime import datetime, timezone
from typing import Any, Callable, Optional

# --- Ключ DeepSeek: экспортируется в окружение при старте программы ---------
# Требование ТЗ: ключ живёт в DEEPSEEK_API_KEY и прошит в коде.
# setdefault = код экспортирует переменную, но не затирает уже заданную в ОС.
os.environ.setdefault("DEEPSEEK_API_KEY", "sk-ВСТАВЬТЕ_СЮДА_КЛЮЧ_DEEPSEEK")

from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402
from openai import OpenAI  # noqa: E402

# --- Конфигурация ------------------------------------------------------------
MODEL_NAME = "deepseek-v4-pro"   # см. примечание: с 14.09.2026 маршрутизируется на V4.1-Flash
BASE_URL = "https://api.deepseek.com"
MAX_ITERATIONS = 10              # защита агентного цикла от зависания
SEARCH_MAX_RESULTS = 5           # дефолт для инструмента search

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SERVER_SCRIPT = os.path.join(BASE_DIR, "mcp_server.py")
RESULTS_FILE = os.path.join(BASE_DIR, "results.json")
LOGS_FILE = os.path.join(BASE_DIR, "logs.json")

STEP_PREVIEW_LIMIT = 1500  # обрезка payload в трассе, чтобы logs.json не раздувался

SYSTEM_PROMPT = (
    "Ты — оркестратор пайплайна из трёх MCP-инструментов. Для КАЖДОГО запроса "
    "пользователя строго выполняй цепочку в указанном порядке, не пропуская шаги:\n"
    "1. Вызови search с запросом пользователя.\n"
    "2. Вызови summarize: в параметр text передай объединённые сниппеты "
    "(title + snippet) всех результатов, полученных от search.\n"
    "3. Вызови save_to_file: в параметр data передай объект "
    '{"query": <исходный запрос>, "search": <полный результат шага 1>, '
    '"summary": <полный результат шага 2>}.\n'
    "Порядок менять нельзя. После успешного выполнения всех трёх вызовов дай "
    "пользователю краткий связный дайджест найденного (3–5 предложений) "
    "и сообщи, что данные сохранены в файл."
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
    """Синхронная обёртка над асинхронным MCP-клиентом + агентный цикл DeepSeek."""

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever, daemon=True, name="mcp-event-loop"
        )
        self._thread.start()
        self._stack: Optional[AsyncExitStack] = None
        self.session: Optional[ClientSession] = None
        self.mcp_tools: list = []
        self.tools_for_llm: list[dict] = []
        self._log_lock = threading.Lock()
        self._client = OpenAI(
            api_key=os.environ["DEEPSEEK_API_KEY"], base_url=BASE_URL
        )
        self.start()

    # ------------------------------------------------------------------ MCP

    def start(self) -> None:
        fut = asyncio.run_coroutine_threadsafe(self._connect(), self._loop)
        fut.result(timeout=60)

    def restart(self) -> None:
        """Перезапуск MCP-сервера (старый subprocess убивается закрытием стека)."""
        fut = asyncio.run_coroutine_threadsafe(self._connect(), self._loop)
        fut.result(timeout=60)

    async def _connect(self) -> None:
        await self._disconnect()
        self._stack = AsyncExitStack()
        params = StdioServerParameters(command=sys.executable, args=[SERVER_SCRIPT])
        read, write = await self._stack.enter_async_context(stdio_client(params))
        self.session = await self._stack.enter_async_context(
            ClientSession(read, write)
        )
        await self.session.initialize()
        listing = await self.session.list_tools()
        self.mcp_tools = list(listing.tools)
        self.tools_for_llm = [self._to_openai_tool(t) for t in self.mcp_tools]

    async def _disconnect(self) -> None:
        if self._stack is not None:
            try:
                await self._stack.aclose()
            except Exception:
                pass
        self._stack = None
        self.session = None

    @staticmethod
    def _to_openai_tool(tool) -> dict:
        schema = tool.inputSchema or {"type": "object", "properties": {}}
        if schema.get("type") == "object" and "properties" not in schema:
            schema = {**schema, "properties": {}}
        return {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description or "",
                "parameters": schema,
            },
        }

    def status(self) -> dict:
        """Статус MCP-сервера: соединение живо + список инструментов со схемами."""
        if self.session is None:
            return {"connected": False}
        try:
            fut = asyncio.run_coroutine_threadsafe(
                self.session.list_tools(), self._loop
            )
            tools = fut.result(timeout=10).tools
            return {
                "connected": True,
                "tools": [
                    {
                        "name": t.name,
                        "description": t.description or "",
                        "schema": t.inputSchema,
                    }
                    for t in tools
                ],
            }
        except Exception as exc:
            return {"connected": False, "error": str(exc)}

    # -------------------------------------------------------------- pipeline

    def run_pipeline(
        self,
        query: str,
        on_step: Optional[Callable[[dict], None]] = None,
    ) -> dict:
        """Синхронный запуск агентного цикла. on_step вызывается на каждый шаг."""
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

        def log(kind: str, name: str = "", payload: Any = None) -> None:
            if isinstance(payload, str) and len(payload) > STEP_PREVIEW_LIMIT:
                payload = payload[:STEP_PREVIEW_LIMIT] + " …[обрезано]"
            rec = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "kind": kind,
                "name": name,
                "payload": payload,
            }
            steps.append(rec)
            if on_step is not None:
                on_step(rec)

        started = time.time()
        status, final_answer, error = "success", "", None
        try:
            if self.session is None:
                raise RuntimeError("MCP-сервер не подключён")
            log("info", "pipeline", f"Старт пайплайна, запрос: {query}")
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

                messages.append(message)  # assistant с tool_calls

                for tc in message.tool_calls:
                    name = tc.function.name
                    try:
                        args = json.loads(tc.function.arguments or "{}")
                    except json.JSONDecodeError:
                        args = {}
                    log("tool_call", name, json.dumps(args, ensure_ascii=False))

                    result = await self.session.call_tool(name, args)
                    text = "\n".join(
                        getattr(c, "text", str(c)) for c in result.content
                    )
                    if result.isError:
                        raise RuntimeError(f"Инструмент {name} вернул ошибку: {text}")
                    log("tool_result", name, text)

                    messages.append(
                        {"role": "tool", "tool_call_id": tc.id, "content": text}
                    )
            else:
                status, error = "error", "Превышен лимит итераций агентного цикла"
                log("error", "pipeline", error)
        except Exception as exc:  # noqa: BLE001 — фиксируем любую ошибку прогона
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
        fut = asyncio.run_coroutine_threadsafe(self._disconnect(), self._loop)
        try:
            fut.result(timeout=10)
        except Exception:
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)
