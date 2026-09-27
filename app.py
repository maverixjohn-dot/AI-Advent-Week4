"""
Flet UI (web-режим) для мультисерверного MCP-пайплайна.

Страницы:
    0 «Пайплайн»         — запрос к LLM, живой лог шагов (с сервером каждого
                           инструмента), финальный ответ, превью графиков
    1 «Администрирование» — статус и перезапуск каждого MCP-сервера, настройки
                           почты (config.json), графиков, хранилище
    2 «Логи»             — история прогонов из logs.json с полной трассой шагов

Запуск: python app.py  ->  http://127.0.0.1:8550
"""

import base64
import json
import os
import re

import flet as ft

from agent import (
    Agent,
    BASE_URL,
    LOGS_FILE,
    MAX_ITERATIONS,
    MODEL_NAME,
    RESULTS_FILE,
    SEARCH_MAX_RESULTS,
    SERVERS,
)
from config import (
    CONFIG_FILE,
    charts_dir,
    load_config,
    outbox_dir,
    save_config,
    telegram_outbox_dir,
)

_agent_holder: dict = {"agent": None}


def get_agent() -> Agent:
    if _agent_holder["agent"] is None:
        _agent_holder["agent"] = Agent()
    return _agent_holder["agent"]


def _masked(text: str, placeholder_prefix: str) -> str:
    if not text or text.startswith(placeholder_prefix):
        return "не задан"
    return text[:4] + "…" + text[-4:] if len(text) > 10 else "задан"


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
    server = f"  [сервер: {rec['server']}]" if rec.get("server") else ""
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
                    ft.Text(f"{label}{name}{server}", weight=ft.FontWeight.BOLD, size=13),
                    ft.Text(body, size=12, selectable=True),
                ],
            ),
        ),
    )


def _extract_chart_paths(steps: list) -> list:
    text = "\n".join(str(s.get("payload") or "") for s in steps)
    return re.findall(r'"chart_path":\s*"([^"]+)"', text)


def _image_from_file(path: str) -> ft.Control:
    try:
        with open(path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
        return ft.Column(
            spacing=4,
            controls=[
                ft.Text(f"График: {os.path.basename(path)}", size=12, weight=ft.FontWeight.BOLD),
                ft.Image(src_base64=b64, fit=ft.ImageFit.CONTAIN, height=350),
            ],
        )
    except OSError:
        return ft.Text(f"Не удалось прочитать график: {path}", color=ft.Colors.RED_700)


def build_pipeline_view(page: ft.Page) -> ft.Control:
    query_tf = ft.TextField(
        label="Запрос к LLM",
        hint_text="Например: найди новости о DeepSeek, сохрани выжимку, построй график по логам и отправь на почту",
        expand=True,
    )
    run_btn = ft.ElevatedButton("Запустить", icon=ft.Icons.PLAY_ARROW)
    progress = ft.ProgressRing(visible=False, width=18, height=18, stroke_width=2)
    status_txt = ft.Text("", size=12, color=ft.Colors.GREY_700)
    steps_lv = ft.ListView(expand=True, spacing=8, auto_scroll=True)
    charts_col = ft.Column(spacing=10)
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
        charts_col.controls.clear()
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
                    status_txt.value = "Готово."
                    for path in _extract_chart_paths(result["steps"]):
                        charts_col.controls.append(_image_from_file(path))
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
    query_tf.on_submit = on_run

    return ft.Column(
        expand=True,
        spacing=12,
        controls=[
            ft.Row([query_tf, run_btn, progress]),
            status_txt,
            answer_box,
            charts_col,
            ft.Text("Шаги флоу", weight=ft.FontWeight.BOLD),
            steps_lv,
        ],
    )


# ---------------------------------------------------------------------------
# Страница «Администрирование»
# ---------------------------------------------------------------------------

def build_admin_view(page: ft.Page) -> ft.Control:
    servers_col = ft.Column(spacing=8)
    storage_txt = ft.Text("", size=13)

    # ---------------- секция серверов ----------------

    def render_servers(st: dict):
        servers_col.controls.clear()
        for srv in st.get("servers", []):
            ok = srv["connected"]
            tool_tiles = [
                ft.ExpansionTile(
                    title=ft.Text(t["name"], size=13, weight=ft.FontWeight.BOLD),
                    subtitle=ft.Text((t["description"] or "").split("\n")[0], size=12),
                    controls=[
                        ft.Container(
                            padding=10,
                            content=ft.Text(
                                json.dumps(t["schema"], ensure_ascii=False, indent=2),
                                size=11, selectable=True, font_family="monospace",
                            ),
                        )
                    ],
                )
                for t in srv["tools"]
            ]
            restart_btn = ft.OutlinedButton(
                "Перезапустить",
                icon=ft.Icons.RESTART_ALT,
                on_click=lambda e, n=srv["name"]: on_restart_server(n),
            )
            servers_col.controls.append(
                ft.Card(
                    content=ft.Container(
                        padding=12,
                        content=ft.Column(
                            spacing=6,
                            controls=[
                                ft.Row(
                                    [
                                        ft.Icon(
                                            ft.Icons.CHECK_CIRCLE if ok else ft.Icons.ERROR,
                                            color=ft.Colors.GREEN_700 if ok else ft.Colors.RED_700,
                                        ),
                                        ft.Text(srv["name"], size=15, weight=ft.FontWeight.BOLD),
                                        ft.Text(srv["description"], size=12, color=ft.Colors.GREY_700),
                                        ft.Text(f"инструментов: {len(srv['tools'])}", size=12),
                                        restart_btn,
                                    ],
                                    alignment=ft.MainAxisAlignment.START,
                                ),
                                ft.Text(
                                    f"Ошибка: {srv['error']}", color=ft.Colors.RED_700, size=12
                                ) if srv.get("error") else ft.Container(height=0),
                                *tool_tiles,
                            ],
                        ),
                    )
                )
            )

    def refresh():
        def worker():
            try:
                st = get_agent().status()
            except Exception as exc:  # noqa: BLE001
                st = {"servers": [], "error": str(exc)}
            render_servers(st)
            storage_txt.value = (
                _storage_info(RESULTS_FILE) + "\n" + _storage_info(LOGS_FILE)
            )
            page.update()

        page.run_thread(worker)

    def on_restart_server(name):
        def worker():
            try:
                get_agent().restart(name)
            except Exception:  # noqa: BLE001
                pass
            refresh()

        page.run_thread(worker)

    # ---------------- секция почты (config.json) ----------------

    cfg = load_config()
    em = cfg["email"]
    ch = cfg["charts"]
    tg = cfg["telegram"]

    smtp_host_tf = ft.TextField(label="SMTP host", value=em["smtp_host"], width=220)
    smtp_port_tf = ft.TextField(label="SMTP port", value=str(em["smtp_port"]), width=110)
    use_ssl_cb = ft.Checkbox(label="SSL", value=bool(em["use_ssl"]))
    smtp_user_tf = ft.TextField(label="SMTP user (логин)", value=em["smtp_user"], width=260)
    from_tf = ft.TextField(label="From (отправитель)", value=em["from_addr"], width=260)
    default_to_tf = ft.TextField(label="Получатель по умолчанию", value=em["default_to"], width=260)
    dry_run_cb = ft.Checkbox(
        label="dry-run (письма в outbox/, без реальной отправки)",
        value=bool(em["dry_run"]),
    )
    charts_dir_tf = ft.TextField(label="Папка графиков", value=ch["output_dir"], width=220)
    save_cfg_status = ft.Text("", size=12, color=ft.Colors.GREEN_700)

    smtp_pwd = os.environ.get("SMTP_PASSWORD", "")
    pwd_status = ft.Text(
        f"SMTP_PASSWORD: {_masked(smtp_pwd, 'ВСТАВЬТЕ')} "
        f"(задаётся в mcp_email.py через os.environ.setdefault)",
        size=12, color=ft.Colors.GREY_700,
    )

    # ---------------- секция Telegram (config.json) ----------------

    tg_to_tf = ft.TextField(
        label="Получатель по умолчанию (username без @ или chat_id)",
        value=tg["default_to"], width=320,
    )
    tg_dry_run_cb = ft.Checkbox(
        label="dry-run (сообщения в outbox_telegram/, Bot API не вызывается)",
        value=bool(tg["dry_run"]),
    )
    tg_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    tg_token_status = ft.Text(
        f"TELEGRAM_BOT_TOKEN: {_masked(tg_token, 'ВСТАВЬТЕ')} "
        f"(задаётся в mcp_telegram.py через os.environ.setdefault)",
        size=12, color=ft.Colors.GREY_700,
    )
    tg_check_result = ft.Text("", size=12, selectable=True)

    def on_tg_check(e):
        tg_check_result.value = "Проверка…"
        page.update()

        def worker():
            try:
                res = get_agent().call_tool("check_connection", {})
                tg_check_result.value = res
            except Exception as exc:  # noqa: BLE001
                tg_check_result.value = f"Ошибка: {exc}"
            page.update()

        page.run_thread(worker)

    def on_save_config(e):
        new_cfg = load_config()
        new_cfg["email"] = {
            "smtp_host": smtp_host_tf.value.strip(),
            "smtp_port": int(smtp_port_tf.value or 465),
            "use_ssl": bool(use_ssl_cb.value),
            "smtp_user": smtp_user_tf.value.strip(),
            "from_addr": from_tf.value.strip(),
            "default_to": default_to_tf.value.strip(),
            "dry_run": bool(dry_run_cb.value),
        }
        new_cfg["charts"]["output_dir"] = charts_dir_tf.value.strip() or "charts"
        new_cfg["telegram"] = {
            "default_to": tg_to_tf.value.strip().lstrip("@"),
            "dry_run": bool(tg_dry_run_cb.value),
            "min_interval_sec": new_cfg["telegram"].get("min_interval_sec", 1.1),
        }
        try:
            save_config(new_cfg)
            save_cfg_status.value = f"Сохранено в {CONFIG_FILE}"
            save_cfg_status.color = ft.Colors.GREEN_700
        except Exception as exc:  # noqa: BLE001
            save_cfg_status.value = f"Ошибка сохранения: {exc}"
            save_cfg_status.color = ft.Colors.RED_700
        page.update()

    def on_clear(path, is_dir=False):
        def handler(e):
            try:
                if is_dir:
                    for fn in os.listdir(path):
                        os.remove(os.path.join(path, fn))
                else:
                    with open(path, "w", encoding="utf-8") as f:
                        json.dump([], f)
            except OSError:
                pass
            refresh()

        return handler

    config_rows = [
        ("Модель", MODEL_NAME),
        ("Base URL", BASE_URL),
        ("Ключ DeepSeek", _masked(os.environ.get("DEEPSEEK_API_KEY", ""), "sk-ВСТАВЬТЕ")),
        ("Лимит итераций агента", str(MAX_ITERATIONS)),
        ("MCP-серверов", str(len(SERVERS))),
        ("max_results по умолчанию (search)", str(SEARCH_MAX_RESULTS)),
        ("config.json", CONFIG_FILE),
        ("outbox (dry-run письма)", outbox_dir()),
        ("outbox_telegram (dry-run)", telegram_outbox_dir()),
    ]

    view = ft.Column(
        expand=True,
        spacing=12,
        scroll=ft.ScrollMode.AUTO,
        controls=[
            ft.Text("MCP-серверы", size=16, weight=ft.FontWeight.BOLD),
            servers_col,
            ft.Row([
                ft.OutlinedButton("Перезапустить все", icon=ft.Icons.RESTART_ALT,
                                  on_click=lambda e: on_restart_server(None)),
                ft.OutlinedButton("Обновить статус", icon=ft.Icons.REFRESH,
                                  on_click=lambda e: refresh()),
            ]),
            ft.Divider(),
            ft.Text("Почта (сохраняется в config.json)", size=16, weight=ft.FontWeight.BOLD),
            ft.Row([smtp_host_tf, smtp_port_tf, use_ssl_cb]),
            ft.Row([smtp_user_tf, from_tf, default_to_tf]),
            ft.Row([dry_run_cb]),
            pwd_status,
            ft.Divider(),
            ft.Text("Telegram (сохраняется в config.json)", size=16, weight=ft.FontWeight.BOLD),
            ft.Row([tg_to_tf]),
            ft.Row([tg_dry_run_cb]),
            tg_token_status,
            ft.Row([
                ft.OutlinedButton("Проверить связь", icon=ft.Icons.NETWORK_CHECK,
                                  on_click=on_tg_check),
            ]),
            tg_check_result,
            ft.Text(
                "Setup-чеклист боевого режима: 1) бот создан через @BotFather, токен вписан; "
                "2) у получателя задан username; 3) получатель нажал /start боту "
                "(бот не может писать первым — ограничение Bot API).",
                size=12, color=ft.Colors.GREY_700,
            ),
            ft.Divider(),
            ft.Text("Графики", size=16, weight=ft.FontWeight.BOLD),
            ft.Row([charts_dir_tf]),
            ft.Row([
                ft.ElevatedButton("Сохранить настройки", icon=ft.Icons.SAVE,
                                  on_click=on_save_config),
                save_cfg_status,
            ]),
            ft.Divider(),
            ft.Text("Конфигурация (только просмотр)", size=16, weight=ft.FontWeight.BOLD),
            ft.DataTable(
                columns=[ft.DataColumn(ft.Text("Параметр")), ft.DataColumn(ft.Text("Значение"))],
                rows=[
                    ft.DataRow(cells=[ft.DataCell(ft.Text(k)),
                                      ft.DataCell(ft.Text(str(v), selectable=True, size=12))])
                    for k, v in config_rows
                ],
            ),
            ft.Divider(),
            ft.Text("Хранилище", size=16, weight=ft.FontWeight.BOLD),
            storage_txt,
            ft.Row([
                ft.OutlinedButton("Очистить results.json", on_click=on_clear(RESULTS_FILE)),
                ft.OutlinedButton("Очистить logs.json", on_click=on_clear(LOGS_FILE)),
                ft.OutlinedButton("Очистить графики", on_click=on_clear(charts_dir(), True)),
            ]),
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
            runs_col.controls.append(ft.Text("Логов пока нет — запустите флоу."))
        for rec in records:
            ok = rec.get("status") == "success"
            steps_controls = [_step_card(s) for s in rec.get("steps", [])] or [
                ft.Text("Шаги не зафиксированы")
            ]
            if rec.get("error"):
                steps_controls.append(
                    ft.Text(f"Ошибка: {rec['error']}", color=ft.Colors.RED_800, size=12)
                )
            servers_used = sorted({s.get("server") for s in rec.get("steps", []) if s.get("server")})
            runs_col.controls.append(
                ft.ExpansionTile(
                    title=ft.Text(
                        f"{rec.get('timestamp', '')[:19].replace('T', ' ')} UTC — {rec.get('query', '')[:80]}",
                        size=13,
                    ),
                    subtitle=ft.Text(
                        f"статус: {rec.get('status')}, шагов: {len(rec.get('steps', []))}, "
                        f"{rec.get('duration_sec')} c, серверы: {', '.join(servers_used) or '—'}",
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
