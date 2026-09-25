"""Flet UI (web-режим): чат с агентом DeepSeek + MCP-инструменты.

Запуск:
    export DEEPSEEK_API_KEY=sk-...
    python3 app.py
Откроется http://localhost:8550
"""

import json
import os
import sys
import traceback

import flet as ft

from agent import WeatherAgent

HOST = os.environ.get("APP_HOST", "0.0.0.0")
PORT = int(os.environ.get("APP_PORT", "8550"))


async def main(page: ft.Page):
    page.title = "DeepSeek + MCP: погодный агент"
    page.theme_mode = ft.ThemeMode.DARK
    page.padding = 20

    # ---------- состояние ----------
    agent: WeatherAgent | None = None

    # ---------- элементы ----------
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
            chat.controls.remove(thinking)
            chat.controls.append(
                ft.Text(f"Ошибка: {exc}", color=ft.Colors.RED_300, selectable=True)
            )
        page.update()

    page.add(
        ft.Column(
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
    )
    page.update()

    # ---------- подключение агента ----------
    if not os.environ.get("DEEPSEEK_API_KEY"):
        status.value = "Ошибка: переменная окружения DEEPSEEK_API_KEY не задана"
        status.color = ft.Colors.RED_300
        page.update()
        return

    try:
        agent = WeatherAgent(server_script=os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "mcp_weather_server.py"))
        await agent.connect()
        status.value = "MCP-сервер подключён (stdio). Инструменты: get_current_weather, get_weather_forecast"
        status.color = ft.Colors.GREEN_300
        input_field.disabled = False
        send_button.disabled = False
    except Exception as exc:
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
