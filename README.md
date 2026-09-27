# MCP Pipeline — учебный проект (DeepSeek + Flet)

Длинный флоу взаимодействия LLM с **несколькими MCP-серверами**:
агент сам выбирает инструменты, оркестратор маршрутизирует вызовы
на нужный сервер через реестр.

```
Flet UI (web, 127.0.0.1:8550)
  → агентный цикл DeepSeek (function calling, модель deepseek-v4-pro)
    → реестр инструментов: видимое_имя → (сервер, ориг. имя, сессия)
       ├─ MCP-сервер «pipeline» (mcp_server.py)
       │    ├─ search(query)        — веб-поиск DuckDuckGo
       │    ├─ summarize(text)      — алгоритмическая выжимка (без LLM)
       │    └─ save_to_file(data)   — допись в results.json
       ├─ MCP-сервер «charts» (mcp_charts.py)
       │    ├─ plot_logs_stats(metric) — графики статистики прогонов (читает logs.json сам)
       │    └─ plot_series(title, labels, values) — график по данным, извлечённым LLM
       ├─ MCP-сервер «email» (mcp_email.py)
       │    └─ send_email(subject, body, to, attachment_path) — SMTP или dry-run в outbox/
       └─ MCP-сервер «telegram» (mcp_telegram.py)
            ├─ send_message(text, to, parse_mode)  — текст
            ├─ send_photo(photo, caption, to)      — PNG-график по chart_path
            ├─ send_document(document, caption, to) — файл
            └─ check_connection()                  — диагностика токена/получателя
```

Telegram-канал реализован по контекстному документу `Telegram-MCP — контекст
для ветки разработки агента.md`: Bot API (не MTProto), троттлинг ~1 сообщ./сек
и retry на 429 в оркестраторе, ошибки уведомлений не роняют флоу. Вместо
community-сервера (Node.js, получатель фиксирован в env) — свой тонкий сервер
с той же поверхностью инструментов: получатель — параметр вызова, есть dry-run.
Замена на community-сервер = одна запись в `SERVERS`.

LLM видит все инструменты единым плоским списком и сама выбирает нужные под
запрос; про топологию серверов знает только клиент. Данные между инструментами
передаются «по ссылке»: графики — путями к PNG, письмо получает путь как
вложение; большие массивы через контекст LLM не гоняются.

## Установка и запуск

```bash
pip install -r requirements.txt
# вставьте ключ в agent.py: os.environ.setdefault("DEEPSEEK_API_KEY", "sk-...")
python app.py
# открыть http://127.0.0.1:8550
```

## Файлы

| Файл | Назначение |
|---|---|
| `mcp_server.py` | MCP-сервер «pipeline»: search, summarize, save_to_file |
| `mcp_charts.py` | MCP-сервер «charts»: plot_logs_stats, plot_series |
| `mcp_email.py` | MCP-сервер «email»: send_email (SMTP / dry-run) |
| `mcp_telegram.py` | MCP-сервер «telegram»: send_message/photo/document, check_connection |
| `agent.py` | мультисерверный MCP-клиент: реестр, маршрутизация, агентный цикл |
| `config.py` / `config.json` | настройки SMTP и графиков (редактируются в UI) |
| `app.py` | Flet UI: «Пайплайн» / «Администрирование» / «Логи» |
| `results.json` | данные пайплайна (пишет save_to_file) |
| `logs.json` | телеметрия прогонов (пишет оркестратор) |
| `charts/` | PNG-графики |
| `outbox/` | .eml-письма в режиме dry-run |
| `outbox_telegram/` | сообщения и файлы Telegram в режиме dry-run |

## Проверка сценариев (инструменты с разных серверов)

1. «Найди X, сделай выжимку и сохрани» → только сервер pipeline:
   search → summarize → save_to_file.
2. «Построй график по логам и отправь мне на почту» → charts → email,
   без поиска: plot_logs_stats → send_email (chart_path как вложение).
3. Длинный флоу: «Найди X, сохрани, построй график по статистике и отправь
   на почту» → все три сервера в одном прогоне.

Корректность выбора и порядка проверяется по живому логу (у каждого шага
указан сервер) и по записи прогона на странице «Логи».

## Почта

По умолчанию включён **dry-run**: письма со вложениями складываются в
`outbox/` как .eml — весь флоу проверяется без боевого SMTP. Для реальной
отправки: снимите dry-run в «Администрировании», задайте SMTP host/user и
пароль приложения в `mcp_email.py` (`SMTP_PASSWORD`). Для Gmail/Яндекса нужен
именно пароль приложения, не основной.

## Telegram

По умолчанию **dry-run**: сообщения и вложения складываются в
`outbox_telegram/` (JSON + копия файла). Для боевого режима: впишите токен в
`mcp_telegram.py` (`TELEGRAM_BOT_TOKEN`), задайте получателя в
«Администрировании», снимите dry-run. Обязательные условия Bot API: бот создан
через @BotFather, у получателя есть username, получатель нажал /start боту
(бот не может писать первым). Кнопка «Проверить связь» в админке вызывает
`check_connection`: токен, getMe, резолв чата.

## Примечания

- Версии зафиксированы: `mcp<2` (в 2.x переименован FastMCP) и `flet==0.28.3`
  (в 1.0 удалён `ft.app` и изменена потоковая модель UI). Обе ветки 1.x
  потребуют отдельной миграции кода.
- `deepseek-v4-pro` — рабочее имя модели, но с 14.09.2026 запросы
  маршрутизируются на V4.1-Flash (V4-Pro выводится из эксплуатации до выхода
  V4.1-Pro). Имя вынесено в константу `MODEL_NAME` в `agent.py`.
- Секреты прошиты через `os.environ.setdefault` по ТЗ: переменная
  экспортируется кодом, дальше читается из окружения. Для боя — плохая практика.
