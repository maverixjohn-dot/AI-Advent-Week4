"""
Flet UI (web-режим) для учебного MCP-пайплайна.

Страницы:
    0 «Пайплайн»         — запрос к LLM, живой лог шагов, финальный ответ
    1 «Администрирование» — статус MCP-сервера, инструменты, конфигурация, хранилище
    2 «Логи»             — история прогонов из logs.json с полной трассой шагов

Запуск: python app.py  ->  http://127.0.0.1:8550
"""

import json
import os

import flet as ft

from agent import (
    Agent,
    BASE_URL,
    LOGS_FILE,
    MAX_ITERATIONS,
    MODEL_NAME,
    RESULTS_FILE,
    SEARCH_MAX_RESULTS,
)

_agent_holder: dict = {"agent": None}


def get_agent() -> Agent:
    if _agent_holder["agent"] is None:
        _agent_holder["agent"] = Agent()
    return _agent_holder["agent"]


def _masked_key() -> str:
    key = os.environ.get("DEEPSEEK_API_KEY", "")
    if len(key) > 12 and not key.startswith("sk-ВСТАВЬТЕ"):
        return key[:7] + "…" + key[-4:]
    return "не задан (вставьте ключ в agent.py)"


def _read_json_array(path: str) -> list:
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _storage_info(path: str) -> str:
    records = _read_json_array(path)
    size = os.path.getsize(path) if os.path.exists(path) else 0
    return f"{os.path.basename(path)} — записей: {len(records)}, размер: {size / 1024:.1f} КБ"


# ---------------------------------------------------------------------------
# Страница «Пайплайн»
# ---------------------------------------------------------------------------

_STEP_STYLE = {
    "info": ("Запуск", ft.Colors.BLUE_GREY_50),
    "tool_call": ("Вызов инструмента", ft.Colors.BLUE_50),
    "tool_result": ("Результат инструмента", ft.Colors.GREEN_50),
    "final": ("Финальный ответ LLM", ft.Colors.AMBER_50),
    "error": ("Ошибка", ft.Colors.RED_50),
}


def _step_card(rec: dict) -> ft.Control:
    label, color = _STEP_STYLE.get(rec["kind"], (rec["kind"], ft.Colors.GREY_50))
    name = f": {rec['name']}" if rec.get("name") else ""
    payload = rec.get("payload")
    body = str(payload) if payload else ""
    if len(body) > 1200:
        body = body[:1200] + " …[обрезано]"
    return ft.Card(
        color=color,
        content=ft.Container(
            padding=10,
            content=ft.Column(
                spacing=4,
                controls=[
                    ft.Text(f"{label}{name}", weight=ft.FontWeight.BOLD, size=13),
                    ft.Text(body, size=12, selectable=True),
                ],
            ),
        ),
    )


def build_pipeline_view(page: ft.Page) -> ft.Control:
    query_tf = ft.TextField(
        label="Запрос к LLM",
        hint_text="Например: последние новости о релизах DeepSeek",
        expand=True,
        on_submit=lambda e: run_btn.on_click(e),
    )
    run_btn = ft.ElevatedButton("Запустить пайплайн", icon=ft.Icons.PLAY_ARROW)
    progress = ft.ProgressRing(visible=False, width=18, height=18, stroke_width=2)
    status_txt = ft.Text("", size=12, color=ft.Colors.GREY_700)
    steps_lv = ft.ListView(expand=True, spacing=8, auto_scroll=True)
    answer_box = ft.Container(
        visible=False,
        padding=12,
        border_radius=8,
        bgcolor=ft.Colors.AMBER_50,
        content=ft.Column(
            spacing=4,
            controls=[
                ft.Text("Ответ", weight=ft.FontWeight.BOLD),
                ft.Markdown("", selectable=True),
            ],
        ),
    )

    def on_run(e):
        query = (query_tf.value or "").strip()
        if not query:
            return
        run_btn.disabled = True
        progress.visible = True
        status_txt.value = "Выполняется…"
        steps_lv.controls.clear()
        answer_box.visible = False
        page.update()

        def worker():
            def on_step(rec: dict):
                steps_lv.controls.append(_step_card(rec))
                page.update()

            try:
                result = get_agent().run_pipeline(query, on_step=on_step)
                if result["status"] == "success":
                    answer_box.content.controls[1].value = result["answer"]
                    answer_box.visible = True
                    status_txt.value = f"Готово. Запись добавлена в {RESULTS_FILE}"
                else:
                    status_txt.value = f"Ошибка: {result['error']}"
            except Exception as exc:  # noqa: BLE001
                status_txt.value = f"Ошибка: {exc}"
            finally:
                run_btn.disabled = False
                progress.visible = False
                page.update()

        page.run_thread(worker)

    run_btn.on_click = on_run

    return ft.Column(
        expand=True,
        spacing=12,
        controls=[
            ft.Row([query_tf, run_btn, progress], alignment=ft.MainAxisAlignment.START),
            status_txt,
            answer_box,
            ft.Text("Шаги пайплайна", weight=ft.FontWeight.BOLD),
            steps_lv,
        ],
    )


# ---------------------------------------------------------------------------
# Страница «Администрирование»
# ---------------------------------------------------------------------------

def build_admin_view(page: ft.Page) -> ft.Control:
    status_txt = ft.Text("MCP-сервер: проверка…", size=14)
    tools_col = ft.Column(spacing=6)
    restart_btn = ft.ElevatedButton("Перезапустить MCP-сервер", icon=ft.Icons.RESTART_ALT)
    storage_txt = ft.Text("", size=13)

    def refresh():
        def worker():
            try:
                st = get_agent().status()
            except Exception as exc:  # noqa: BLE001
                st = {"connected": False, "error": str(exc)}
            tools_col.controls.clear()
            if st.get("connected"):
                status_txt.value = f"MCP-сервер: подключён, инструментов: {len(st['tools'])}"
                status_txt.color = ft.Colors.GREEN_800
                for t in st["tools"]:
                    tools_col.controls.append(
                        ft.ExpansionTile(
                            title=ft.Text(t["name"], weight=ft.FontWeight.BOLD, size=13),
                            subtitle=ft.Text((t["description"] or "").split("\n")[0], size=12),
                            controls=[
                                ft.Container(
                                    padding=10,
                                    content=ft.Text(
                                        json.dumps(t["schema"], ensure_ascii=False, indent=2),
                                        size=11,
                                        selectable=True,
                                        font_family="monospace",
                                    ),
                                )
                            ],
                        )
                    )
            else:
                status_txt.value = f"MCP-сервер: НЕ подключён ({st.get('error', 'нет соединения')})"
                status_txt.color = ft.Colors.RED_800
            storage_txt.value = (
                _storage_info(RESULTS_FILE) + "\n" + _storage_info(LOGS_FILE)
            )
            page.update()

        page.run_thread(worker)

    def on_restart(e):
        restart_btn.disabled = True
        page.update()

        def worker():
            try:
                get_agent().restart()
            except Exception:  # noqa: BLE001
                pass
            restart_btn.disabled = False
            refresh()

        page.run_thread(worker)

    def on_clear(path: str):
        def handler(e):
            with open(path, "w", encoding="utf-8") as f:
                json.dump([], f)
            refresh()

        return handler

    restart_btn.on_click = on_restart

    config_rows = [
        ("Модель", MODEL_NAME),
        ("Base URL", BASE_URL),
        ("Ключ API", _masked_key()),
        ("Лимит итераций агента", str(MAX_ITERATIONS)),
        ("max_results по умолчанию (search)", str(SEARCH_MAX_RESULTS)),
        ("results.json", RESULTS_FILE),
        ("logs.json", LOGS_FILE),
    ]

    view = ft.Column(
        expand=True,
        spacing=12,
        scroll=ft.ScrollMode.AUTO,
        controls=[
            ft.Text("Состояние MCP-сервера", size=16, weight=ft.FontWeight.BOLD),
            ft.Row([status_txt, restart_btn]),
            ft.Text("Зарегистрированные инструменты (MCP list_tools)", size=16, weight=ft.FontWeight.BOLD),
            tools_col,
            ft.Divider(),
            ft.Text("Конфигурация (только просмотр)", size=16, weight=ft.FontWeight.BOLD),
            ft.DataTable(
                columns=[ft.DataColumn(ft.Text("Параметр")), ft.DataColumn(ft.Text("Значение"))],
                rows=[
                    ft.DataRow(cells=[ft.DataCell(ft.Text(k)), ft.DataCell(ft.Text(v, selectable=True))])
                    for k, v in config_rows
                ],
            ),
            ft.Divider(),
            ft.Text("Хранилище", size=16, weight=ft.FontWeight.BOLD),
            storage_txt,
            ft.Row(
                [
                    ft.OutlinedButton("Очистить results.json", on_click=on_clear(RESULTS_FILE)),
                    ft.OutlinedButton("Очистить logs.json", on_click=on_clear(LOGS_FILE)),
                    ft.OutlinedButton("Обновить", icon=ft.Icons.REFRESH, on_click=lambda e: refresh()),
                ]
            ),
        ],
    )
    refresh()
    return view


# ---------------------------------------------------------------------------
# Страница «Логи»
# ---------------------------------------------------------------------------

def build_logs_view(page: ft.Page) -> ft.Control:
    runs_col = ft.Column(spacing=6, scroll=ft.ScrollMode.AUTO, expand=True)

    def refresh():
        records = list(reversed(_read_json_array(LOGS_FILE)))
        runs_col.controls.clear()
        if not records:
            runs_col.controls.append(ft.Text("Логов пока нет — запустите пайплайн."))
        for rec in records:
            ok = rec.get("status") == "success"
            steps_controls = [
                _step_card(s) for s in rec.get("steps", [])
            ] or [ft.Text("Шаги не зафиксированы")]
            if rec.get("error"):
                steps_controls.append(
                    ft.Text(f"Ошибка: {rec['error']}", color=ft.Colors.RED_800, size=12)
                )
            runs_col.controls.append(
                ft.ExpansionTile(
                    title=ft.Text(
                        f"{rec.get('timestamp', '')[:19].replace('T', ' ')} UTC — {rec.get('query', '')[:80]}",
                        size=13,
                    ),
                    subtitle=ft.Text(
                        f"статус: {rec.get('status')}, шагов: {len(rec.get('steps', []))}, "
                        f"длительность: {rec.get('duration_sec')} c",
                        size=12,
                    ),
                    leading=ft.Icon(
                        ft.Icons.CHECK_CIRCLE if ok else ft.Icons.ERROR,
                        color=ft.Colors.GREEN_700 if ok else ft.Colors.RED_700,
                    ),
                    controls=[ft.Container(padding=8, content=ft.Column(steps_controls, spacing=6))],
                )
            )
        page.update()

    refresh()
    return ft.Column(
        expand=True,
        spacing=8,
        controls=[
            ft.Row(
                [
                    ft.Text("История прогонов (logs.json)", size=16, weight=ft.FontWeight.BOLD),
                    ft.OutlinedButton("Обновить", icon=ft.Icons.REFRESH, on_click=lambda e: refresh()),
                ],
                alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
            ),
            runs_col,
        ],
    )


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main(page: ft.Page) -> None:
    page.title = "MCP Pipeline — DeepSeek"
    page.padding = 16

    content = ft.Container(expand=True, padding=ft.padding.only(left=16))
    views = {
        0: lambda: build_pipeline_view(page),
        1: lambda: build_admin_view(page),
        2: lambda: build_logs_view(page),
    }

    def on_nav(e):
        content.content = views[rail.selected_index]()
        page.update()

    rail = ft.NavigationRail(
        selected_index=0,
        label_type=ft.NavigationRailLabelType.ALL,
        min_width=100,
        on_change=on_nav,
        destinations=[
            ft.NavigationRailDestination(icon=ft.Icons.BOLT, label="Пайплайн"),
            ft.NavigationRailDestination(icon=ft.Icons.ADMIN_PANEL_SETTINGS, label="Админ"),
            ft.NavigationRailDestination(icon=ft.Icons.LIST_ALT, label="Логи"),
        ],
    )

    content.content = views[0]()
    page.add(ft.Row([rail, ft.VerticalDivider(width=1), content], expand=True))


if __name__ == "__main__":
    ft.app(target=main, view=ft.AppView.WEB_BROWSER, host="127.0.0.1", port=8550)
