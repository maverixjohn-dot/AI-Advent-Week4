"""Агент: DeepSeek (function calling) + MCP-клиент (stdio).

Цикл работы:
  1. Список инструментов берётся у MCP-сервера (list_tools) и конвертируется
     в формат tools OpenAI-совместимого API DeepSeek.
  2. Сообщение пользователя уходит в модель deepseek-v4-pro вместе с tools.
  3. Если модель вернула tool_calls — каждый вызов исполняется через
     MCP-сессию (call_tool), результат добавляется в историю как сообщение
     role="tool", и цикл повторяется.
  4. Когда модель отвечает без tool_calls — это финальный ответ.

Каждый вызов инструмента возвращается наружу через on_tool_call — UI
использует это, чтобы показать факт обращения к MCP.
"""

import json
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass, field

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from openai import AsyncOpenAI

MODEL = "deepseek-v4-pro"
MAX_TOOL_ROUNDS = 8  # защита от бесконечного цикла tool_calls

SYSTEM_PROMPT = (
    "Ты — ассистент с доступом к инструментам погоды через MCP-сервер. "
    "Для вопросов о погоде всегда вызывай инструмент и отвечай только "
    "на основе возвращённых данных, не выдумывай числа. "
    "У тебя есть периодические инструменты: start_weather_collection / "
    "stop_weather_collection (сбор погоды в JSON каждые N минут), "
    "set_report_schedule (расписание отчётов), get_latest_report и "
    "get_new_reports (агрегированные отчёты). "
    "Для вопросов о статистике («какая была погода», «нужен ли зонт», "
    "«дай сводку») используй get_latest_report — там агрегированные данные "
    "из файла наблюдений. "
    "Отвечай кратко и по-русски."
)


@dataclass
class ToolCallTrace:
    """Запись о выполненном вызове MCP-инструмента (для отображения в UI)."""
    name: str
    arguments: dict
    result: str
    is_error: bool = False


@dataclass
class AgentResponse:
    text: str
    tool_calls: list[ToolCallTrace] = field(default_factory=list)


class WeatherAgent:
    def __init__(self, server_script: str = "mcp_weather_server.py"):
        api_key = os.environ.get("DEEPSEEK_API_KEY")
        if not api_key:
            raise RuntimeError("Переменная окружения DEEPSEEK_API_KEY не задана")

        self._llm = AsyncOpenAI(
            api_key=api_key,
            base_url="https://api.deepseek.com",
        )
        self._server_params = StdioServerParameters(
            command="python3",
            args=[server_script],
        )
        self._errlog = None
        self._session: ClientSession | None = None
        self._stdio_ctx = None
        self._session_ctx = None
        self._tools: list[dict] = []
        # История диалога (user/assistant), tool-сообщения в неё не входят
        self._history: list[dict] = []

    async def connect(self, errlog_path: str | None = None) -> None:
        """Запуск MCP-сервера подпроцессом и установка stdio-сессии.

        errlog_path: если задан, stderr сервера (логи [collect]/[report])
        пишется в этот файл — его читает страница логов в UI.
        """
        if errlog_path:
            self._errlog = open(errlog_path, "a", encoding="utf-8", buffering=1)
        self._stdio_ctx = stdio_client(
            self._server_params, errlog=self._errlog or sys.stderr
        )
        read, write = await self._stdio_ctx.__aenter__()
        self._session_ctx = ClientSession(read, write)
        self._session = await self._session_ctx.__aenter__()
        await self._session.initialize()

        listed = await self._session.list_tools()
        self._tools = [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description or "",
                    "parameters": t.inputSchema,
                },
            }
            for t in listed.tools
        ]

    async def close(self) -> None:
        # anyio запрещает выходить из cancel scope в задаче, отличной от той,
        # где он был создан (on_disconnect у Flet — другая задача). Ошибку
        # глушим: сервер-подпроцесс завершится вместе с приложением.
        for ctx in (self._session_ctx, self._stdio_ctx):
            if ctx:
                try:
                    await ctx.__aexit__(None, None, None)
                except Exception:
                    pass

    async def call_tool(self, name: str, arguments: dict) -> str:
        """Прямой вызов MCP-инструмента вне диалога с LLM
        (используется UI для фонового поллинга алертов)."""
        if not self._session:
            raise RuntimeError("Агент не подключён к MCP-серверу (вызовите connect)")
        result = await self._session.call_tool(name, arguments)
        return "\n".join(p.text for p in result.content if hasattr(p, "text"))

    async def chat(self, user_message: str) -> AgentResponse:
        """Один ход диалога: запрос -> tool_calls через MCP -> финальный ответ."""
        if not self._session:
            raise RuntimeError("Агент не подключён к MCP-серверу (вызовите connect)")

        self._history.append({"role": "user", "content": user_message})
        messages = [{"role": "system", "content": SYSTEM_PROMPT}, *self._history]
        traces: list[ToolCallTrace] = []

        for round_no in range(1, MAX_TOOL_ROUNDS + 1):
            print(f"[agent] раунд {round_no}: запрос к DeepSeek ({MODEL})…", flush=True)
            response = await self._llm.chat.completions.create(
                model=MODEL,
                messages=messages,
                tools=self._tools,
            )
            choice = response.choices[0]
            msg = choice.message
            print(f"[agent] ответ получен, tool_calls: {len(msg.tool_calls or [])}",
                  flush=True)

            if not msg.tool_calls:
                # Финальный ответ модели
                self._history.append({"role": "assistant", "content": msg.content or ""})
                return AgentResponse(text=msg.content or "", tool_calls=traces)

            # Фиксируем сообщение ассистента с tool_calls в рабочей истории
            messages.append(msg.model_dump(exclude_none=True))

            for call in msg.tool_calls:
                name = call.function.name
                try:
                    args = json.loads(call.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}

                print(f"[agent] MCP-вызов: {name}({args})", flush=True)
                result = await self._session.call_tool(name, args)
                print(f"[agent] MCP-ответ: is_error={result.isError}, "
                      f"{sum(len(p.text) for p in result.content if hasattr(p, 'text'))} символов",
                      flush=True)
                text = "\n".join(
                    part.text for part in result.content if hasattr(part, "text")
                )
                traces.append(
                    ToolCallTrace(
                        name=name,
                        arguments=args,
                        result=text,
                        is_error=bool(result.isError),
                    )
                )
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": text,
                    }
                )

        return AgentResponse(
            text="Превышен лимит вызовов инструментов, диалог прерван.",
            tool_calls=traces,
        )
