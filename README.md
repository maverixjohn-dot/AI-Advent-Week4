# Погодный агент: DeepSeek + MCP + Flet

Агент на DeepSeek (`deepseek-v4-pro`, function calling) вызывает инструменты
собственного MCP-сервера, оборачивающего Open-Meteo API, и отвечает в чате
на Flet (web-режим).

## Структура

| Файл | Назначение |
|---|---|
| `mcp_weather_server.py` | MCP-сервер (stdio). Инструменты `get_current_weather(city)`, `get_weather_forecast(city, days)` |
| `agent.py` | Агентский цикл: DeepSeek function calling → вызовы через MCP-клиент → финальный ответ |
| `app.py` | Flet-чат в web-режиме с панелью MCP-вызовов (имя, аргументы, сырой результат) |
| `test_mcp_direct.py` | Тест сервера без LLM: list_tools, call_tool, обработка ошибки |
| `requirements.txt` | Зависимости |

## Запуск

```bash
pip install -r requirements.txt
python3 app.py
```

Ключ API читается из уже существующей переменной окружения `DEEPSEEK_API_KEY`
(`os.environ.get("DEEPSEEK_API_KEY")` в `agent.py`, класс `WeatherAgent`).
Ничего дополнительно задавать не нужно — переменная должна быть видна
в том shell, из которого запускается `app.py`.

Открыть http://localhost:8550

Проверка MCP-сервера отдельно (ключ не нужен):

```bash
python3 test_mcp_direct.py
```

Диагностика полного цикла DeepSeek → MCP без UI (ключ нужен,
логи каждого шага печатаются в консоль):

```bash
python3 agent.py "Что сейчас за погода в Москве?"
```

## Как это работает

1. `app.py` при старте запускает `mcp_weather_server.py` подпроцессом и
   подключается к нему по stdio (`agent.connect()`).
2. Список инструментов читается через `list_tools()` и конвертируется в
   формат `tools` OpenAI-совместимого API DeepSeek. Схемы параметров
   генерируются из type hints и docstring-ов функций с декоратором `@mcp.tool()`.
3. Сообщение пользователя уходит в `deepseek-v4-pro` вместе с инструментами.
4. Если модель вернула `tool_calls`, каждый вызов исполняется через
   `session.call_tool()`, результат добавляется в историю как сообщение
   `role="tool"`, цикл повторяется (лимит — 8 раундов).
5. Финальный текстовый ответ и трасса вызовов отображаются в чате.

## Замечания

- Ключ DeepSeek остаётся на сервере: в web-режиме Flet исполняет Python
  на стороне сервера, в браузер ключ не попадает.
- Open-Meteo не требует API-ключа.
- Зафиксировано `mcp<2`: в mcp 2.x FastMCP переименован в MCPServer, код
  написан под API v1.
- Для web-режима Flet 1.x нужен отдельный пакет `flet-web` (уже в requirements).
