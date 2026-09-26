"""Flet UI (web-режим): чат с агентом DeepSeek + MCP-инструменты.

Страницы:
  /       — чат с агентом (вызовы инструментов, отчёты по расписанию)
  /admin  — администрирование: город сбора, интервал, расписание отчётов
  /logs   — логи приложения и MCP-сервера (автообновление)

Запуск:
    python3 app.py
Откроется http://localhost:8550
"""

import asyncio
import json
import os
import sys
import traceback
from datetime import datetime, timezone

import flet as ft

from agent import WeatherAgent

HOST = os.environ.get("APP_HOST", "127.0.0.1")
PORT = int(os.environ.get("APP_PORT", "8550"))

# Периодический сбор погоды и отчёты (автостарт при подключении агента)
COLLECT_CITY = os.environ.get("COLLECT_CITY", "Санкт-Петербург")
COLLECT_INTERVAL_MIN = float(os.environ.get("COLLECT_INTERVAL_MIN", "5"))
REPORT_INTERVAL_MIN = float(os.environ.get("REPORT_INTERVAL_MIN", "15"))
REPORT_CRON = os.environ.get("REPORT_CRON", "")  # если задан — вместо интервала
REPORT_POLL_SEC = int(os.environ.get("REPORT_POLL_SEC", "30"))

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# Единый лог: события приложения [app] + stderr MCP-сервера [collect]/[report]
LOG_PATH = os.environ.get("APP_LOG_PATH", os.path.join(_BASE_DIR, "app_server.log"))
LOG_TAIL_LINES = 500
LOG_REFRESH_SEC = 5


def log_line(message: str) -> None:
    """Событие приложения — в консоль и в общий лог-файл."""
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    line = f"{ts} [app] {message}"
    print(line, flush=True)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


async def main(page: ft.Page):
    page.title = "DeepSeek + MCP: погодный агент"
    page.theme_mode = ft.ThemeMode.DARK
    page.padding = 20

    # ---------- состояние ----------
    agent: WeatherAgent | None = None
    logs_refresh_task: asyncio.Task | None = None

    # ---------- общие элементы чата (создаются один раз) ----------
    chat = ft.ListView(expand=True, spacing=8, auto_scroll=True)
    status = ft.Text("Подключение к MCP-серверу…", color=ft.Colors.AMBER)
    input_field = ft.TextField(
        hint_text="Спросите о погоде, например: «Что сейчас в Мурманске?»",
        expand=True,
        disabled=True,
        on_submit=lambda e: page.run_task(send_message, e),
    )
    send_button = ft.FilledButton(
        "Отправить",
        icon=ft.Icons.SEND,
        disabled=True,
        on_click=lambda e: page.run_task(send_message, e),
    )

    def bubble(text: str, is_user: bool) -> ft.Container:
        return ft.Container(
            content=ft.Text(text, selectable=True),
            bgcolor=ft.Colors.BLUE_GREY_800 if is_user else ft.Colors.GREY_900,
            border_radius=10,
            padding=12,
            margin=ft.Margin.only(
                left=80 if is_user else 0,
                right=0 if is_user else 80,
            ),
            alignment=ft.Alignment.CENTER_RIGHT if is_user else ft.Alignment.CENTER_LEFT,
        )

    def tool_card(trace) -> ft.Container:
        args = json.dumps(trace.arguments, ensure_ascii=False)
        return ft.Container(
            content=ft.Column(
                controls=[
                    ft.Row(
                        controls=[
                            ft.Icon(
                                ft.Icons.ERROR_OUTLINE if trace.is_error else ft.Icons.BUILD_OUTLINED,
                                color=ft.Colors.RED_300 if trace.is_error else ft.Colors.GREEN_300,
                                size=16,
                            ),
                            ft.Text(
                                f"MCP-вызов: {trace.name}({args})",
                                size=13,
                                weight=ft.FontWeight.BOLD,
                                selectable=True,
                            ),
                        ]
                    ),
                    ft.Container(
                        content=ft.Text(
                            trace.result,
                            size=12,
                            selectable=True,
                            font_family="monospace",
                        ),
                        bgcolor=ft.Colors.BLACK_45,
                        border_radius=6,
                        padding=8,
                    ),
                ],
                spacing=6,
            ),
            border=ft.Border.all(1, ft.Colors.GREY_700),
            border_radius=10,
            padding=10,
            margin=ft.Margin.only(right=80),
        )

    def report_card(report: dict) -> ft.Container:
        """Карточка агрегированного отчёта, сгенерированного по расписанию."""
        if report.get("checks", 0) == 0:
            body = report.get("note", "нет данных")
        else:
            freq = ", ".join(f"{k} ×{v}" for k, v in
                             report.get("conditions_freq", {}).items())
            body = (
                f"Замеров: {report['checks']} | "
                f"t° ср {report['temp_avg_c']} °C "
                f"({report['temp_min_c']}…{report['temp_max_c']}) | "
                f"осадки {report['total_precipitation_mm']} мм | "
                f"дождь: {report['rain_share_pct']}% замеров\n"
                f"Состояния: {freq}\n"
                f"Сейчас: {report['last_check']['conditions']}, "
                f"{report['last_check']['temperature_c']} °C — "
                f"зонт: {report['umbrella']}"
            )
        return ft.Container(
            content=ft.Column(
                controls=[
                    ft.Row(controls=[
                        ft.Icon(ft.Icons.SUMMARIZE, color=ft.Colors.BLUE_200,
                                size=20),
                        ft.Text(
                            f"Отчёт по расписанию: {report.get('city', '?')} "
                            f"({report.get('generated_at', '')} UTC)",
                            size=13, weight=ft.FontWeight.BOLD, selectable=True),
                    ]),
                    ft.Text(body, size=13, selectable=True),
                ],
                spacing=6,
            ),
            bgcolor=ft.Colors.BLUE_GREY_900,
            border=ft.Border.all(1, ft.Colors.BLUE_400),
            border_radius=10,
            padding=12,
            margin=ft.Margin.only(right=80),
        )

    async def send_message(e):
        text = (input_field.value or "").strip()
        if not text or not agent:
            return
        input_field.value = ""
        chat.controls.append(bubble(text, is_user=True))
        thinking = ft.Text("Агент думает…", italic=True, color=ft.Colors.GREY_500)
        chat.controls.append(thinking)
        page.update()

        try:
            response = await agent.chat(text)
            chat.controls.remove(thinking)
            for trace in response.tool_calls:
                chat.controls.append(tool_card(trace))
            chat.controls.append(bubble(response.text, is_user=False))
        except Exception as exc:
            traceback.print_exc()  # полный стек — в консоль, где запущено app.py
            chat.controls.remove(thinking)
            chat.controls.append(
                ft.Text(f"Ошибка: {type(exc).__name__}: {exc}",
                        color=ft.Colors.RED_300, selectable=True)
            )
        page.update()

    # ---------- страница чата (собирается один раз) ----------
    chat_view = ft.Column(
        controls=[
            ft.Text("DeepSeek (deepseek-v4-pro) + MCP + Open-Meteo", size=18,
                    weight=ft.FontWeight.BOLD),
            status,
            ft.Container(content=chat, expand=True, border_radius=10,
                         padding=10, bgcolor="#101418"),
            ft.Row(controls=[input_field, send_button]),
        ],
        expand=True,
        spacing=10,
    )

    # ---------- страница администрирования ----------
    def build_admin_view() -> ft.Column:
        city_field = ft.TextField(label="Город M (сбор погоды)",
                                  value=COLLECT_CITY, width=400)
        collect_interval_field = ft.TextField(
            label="Интервал сбора N, минут (0.05–1440)",
            value=str(COLLECT_INTERVAL_MIN), width=400)
        schedule_type = ft.Dropdown(
            label="Тип расписания отчёта",
            value="interval",
            width=400,
            options=[
                ft.dropdown.Option("interval", "Интервал в минутах"),
                ft.dropdown.Option("cron", "Cron-выражение"),
            ],
        )
        report_interval_field = ft.TextField(
            label="Отчёт каждые N минут (0.05–10080)",
            value=str(REPORT_INTERVAL_MIN), width=400)
        cron_field = ft.TextField(
            label='Cron, 5 полей (например: "0 9 * * *")',
            value=REPORT_CRON, width=400, visible=bool(REPORT_CRON))
        result_text = ft.Text("", selectable=True)

        def on_schedule_type_change(e):
            cron_field.visible = schedule_type.value == "cron"
            report_interval_field.visible = schedule_type.value == "interval"
            page.update()

        schedule_type.on_select = on_schedule_type_change

        async def load_config():
            """Предзаполнение формы текущей конфигурацией из сервера."""
            # Агент подключается после первого рендера — ждём до 10 секунд
            for _ in range(20):
                if agent and agent._session:
                    break
                await asyncio.sleep(0.5)
            if not agent or not agent._session:
                result_text.value = "Агент не подключён — настройки недоступны"
                result_text.color = ft.Colors.RED_300
                page.update()
                return
            try:
                raw = await agent.call_tool("get_config", {})
                cfg = json.loads(raw)
                collection = cfg.get("collection")
                if collection:
                    city_field.value = collection["city"]
                    collect_interval_field.value = str(collection["interval_minutes"])
                schedule = cfg.get("report_schedule")
                if schedule:
                    if "cron" in schedule:
                        schedule_type.value = "cron"
                        cron_field.value = schedule["cron"]
                    else:
                        schedule_type.value = "interval"
                        report_interval_field.value = str(schedule["interval_minutes"])
                    cron_field.visible = schedule_type.value == "cron"
                    report_interval_field.visible = schedule_type.value == "interval"
                page.update()
            except Exception as exc:
                result_text.value = f"Не удалось прочитать конфигурацию: {exc}"
                result_text.color = ft.Colors.RED_300
                page.update()

        async def apply_settings(e):
            if not agent:
                result_text.value = "Агент не подключён"
                result_text.color = ft.Colors.RED_300
                page.update()
                return
            result_text.value = "Применяю…"
            result_text.color = ft.Colors.AMBER
            page.update()
            try:
                interval = float(collect_interval_field.value)
                raw = await agent.call_tool(
                    "start_weather_collection",
                    {"city": city_field.value.strip(), "interval_minutes": interval},
                )
                log_line(f"админ: сбор -> {city_field.value.strip()}, {interval} мин")

                if schedule_type.value == "cron":
                    args = {"cron": cron_field.value.strip()}
                else:
                    args = {"interval_minutes": float(report_interval_field.value)}
                raw2 = await agent.call_tool("set_report_schedule", args)
                log_line(f"админ: расписание отчёта -> {args}")

                result_text.value = f"Применено. Сбор: {json.loads(raw)['collecting']}, {interval} мин. Расписание: {args}"
                result_text.color = ft.Colors.GREEN_300
            except (ValueError, Exception) as exc:
                result_text.value = f"Ошибка: {exc}"
                result_text.color = ft.Colors.RED_300
                log_line(f"админ: ошибка применения настроек: {exc}")
            page.update()

        async def stop_collection(e):
            if not agent:
                return
            try:
                await agent.call_tool("stop_weather_collection", {})
                log_line("админ: сбор остановлен")
                result_text.value = "Сбор остановлен."
                result_text.color = ft.Colors.AMBER
            except Exception as exc:
                result_text.value = f"Ошибка: {exc}"
                result_text.color = ft.Colors.RED_300
            page.update()

        view = ft.Column(
            controls=[
                ft.Text("Администрирование", size=18, weight=ft.FontWeight.BOLD),
                ft.Text("Периодический сбор погоды и расписание отчётов. "
                        "Настройки сохраняются в JSON на сервере и "
                        "восстанавливаются при рестарте.", size=13),
                city_field,
                collect_interval_field,
                schedule_type,
                report_interval_field,
                cron_field,
                ft.Row(controls=[
                    ft.FilledButton("Применить", icon=ft.Icons.SAVE,
                                    on_click=lambda e: page.run_task(apply_settings, e)),
                    ft.OutlinedButton("Остановить сбор", icon=ft.Icons.STOP,
                                      on_click=lambda e: page.run_task(stop_collection, e)),
                ]),
                result_text,
            ],
            spacing=14,
            scroll=ft.ScrollMode.AUTO,
            expand=True,
        )
        page.run_task(load_config)
        return view

    # ---------- страница логов ----------
    def build_logs_view() -> ft.Column:
        logs_list = ft.ListView(expand=True, spacing=2, auto_scroll=True)
        info = ft.Text(f"Файл: {LOG_PATH} — обновление каждые "
                       f"{LOG_REFRESH_SEC} сек, последние {LOG_TAIL_LINES} строк",
                       size=12, color=ft.Colors.GREY_500)

        def reload_logs():
            logs_list.controls.clear()
            try:
                # errors="replace": файл пишется двумя процессами параллельно,
                # чтение может попасть на середину многобайтового UTF-8-символа
                with open(LOG_PATH, encoding="utf-8", errors="replace") as f:
                    lines = f.readlines()[-LOG_TAIL_LINES:]
            except OSError:
                lines = ["(лог-файл пока не создан)\n"]
            for line in lines:
                color = (ft.Colors.RED_300 if "ERROR" in line or "ошибка" in line.lower()
                         else ft.Colors.GREY_300)
                logs_list.controls.append(
                    ft.Text(line.rstrip("\n"), size=12, selectable=True,
                            font_family="monospace", color=color))

        async def auto_refresh():
            while True:
                await asyncio.sleep(LOG_REFRESH_SEC)
                try:
                    reload_logs()
                    page.update()
                except Exception:
                    traceback.print_exc()

        reload_logs()

        nonlocal logs_refresh_task
        if logs_refresh_task is None or logs_refresh_task.done():
            logs_refresh_task = asyncio.create_task(auto_refresh())

        return ft.Column(
            controls=[
                ft.Row(controls=[
                    ft.Text("Логи", size=18, weight=ft.FontWeight.BOLD),
                    ft.FilledTonalButton(
                        "Обновить", icon=ft.Icons.REFRESH,
                        on_click=lambda e: (reload_logs(), page.update())),
                ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
                info,
                ft.Container(content=logs_list, expand=True, border_radius=10,
                             padding=10, bgcolor="#101418"),
            ],
            expand=True,
            spacing=10,
        )

    # ---------- маршрутизация ----------
    def nav_bar(current: str) -> ft.Row:
        def btn(text, route):
            return ft.TextButton(
                text,
                style=ft.ButtonStyle(
                    color=ft.Colors.BLUE_200 if current == route else ft.Colors.GREY_400),
                on_click=lambda e: page.navigate(route),
            )
        return ft.Row(controls=[
            btn("Чат", "/"),
            btn("Администрирование", "/admin"),
            btn("Логи", "/logs"),
        ])

    def render(route: str):
        page.controls.clear()
        if route == "/admin":
            view = build_admin_view()
        elif route == "/logs":
            view = build_logs_view()
        else:
            view = chat_view
        page.add(nav_bar(route), view)
        page.update()

    def on_route_change(e):
        render(page.route)

    page.on_route_change = on_route_change
    render(page.route or "/")

    # ---------- подключение агента ----------
    if not os.environ.get("DEEPSEEK_API_KEY"):
        status.value = "Ошибка: переменная окружения DEEPSEEK_API_KEY не задана"
        status.color = ft.Colors.RED_300
        page.update()
        return

    try:
        agent = WeatherAgent(server_script=os.path.join(
            _BASE_DIR, "mcp_weather_server.py"))
        await agent.connect(errlog_path=LOG_PATH)

        # Автостарт периодического сбора и расписания отчётов
        collect_raw = await agent.call_tool(
            "start_weather_collection",
            {"city": COLLECT_CITY, "interval_minutes": COLLECT_INTERVAL_MIN},
        )
        log_line(f"сбор запущен: {json.loads(collect_raw)['collecting']}, "
                 f"{COLLECT_INTERVAL_MIN} мин")

        schedule_args = (
            {"cron": REPORT_CRON} if REPORT_CRON
            else {"interval_minutes": REPORT_INTERVAL_MIN}
        )
        sched_raw = await agent.call_tool("set_report_schedule", schedule_args)
        log_line(f"расписание отчёта: {schedule_args}")

        status.value = (f"MCP-сервер подключён (stdio). Сбор: {COLLECT_CITY} "
                        f"каждые {COLLECT_INTERVAL_MIN} мин, отчёт: "
                        f"{REPORT_CRON or f'каждые {REPORT_INTERVAL_MIN} мин'}")
        status.color = ft.Colors.GREEN_300
        input_field.disabled = False
        send_button.disabled = False
        page.update()

        async def report_poller():
            """Фоновый поллинг: новые отчёты по расписанию — карточкой в чат."""
            while True:
                await asyncio.sleep(REPORT_POLL_SEC)
                try:
                    raw = await agent.call_tool("get_new_reports", {})
                    data = json.loads(raw)
                    for report in data.get("new_reports", []):
                        chat.controls.append(report_card(report))
                    if data.get("new_reports"):
                        page.update()
                except Exception:
                    traceback.print_exc()

        asyncio.create_task(report_poller())
    except Exception as exc:
        traceback.print_exc()
        status.value = f"Не удалось подключиться к MCP-серверу: {exc}"
        status.color = ft.Colors.RED_300
    page.update()

    async def on_disconnect(e):
        if agent:
            await agent.close()

    page.on_disconnect = on_disconnect


if __name__ == "__main__":
    shown_host = "localhost" if HOST in ("0.0.0.0", "::") else HOST
    print(f"Откройте в браузере: http://{shown_host}:{PORT}", flush=True)
    # view=WEB_BROWSER — запуск формы в веб-режиме
    ft.run(main, host=HOST, port=PORT, view=ft.AppView.WEB_BROWSER)
