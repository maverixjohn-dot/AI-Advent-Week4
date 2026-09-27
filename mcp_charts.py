"""
MCP-сервер «charts» — построение графиков.

Инструменты:
    plot_logs_stats — статистика прогонов по данным logs.json
                      (данные читает сам инструмент, LLM передаёт только параметры);
    plot_series     — график по произвольным данным, извлечённым LLM из результата.

Оба инструмента возвращают путь к PNG — этот путь можно передать
в send_email (сервер «email») как attachment_path.

Запуск: python mcp_charts.py  (транспорт stdio; поднимается клиентом из agent.py)
"""

import json
import os
from collections import Counter
from datetime import datetime

import matplotlib

matplotlib.use("Agg")  # без GUI, серверный режим
import matplotlib.pyplot as plt  # noqa: E402

from mcp.server.fastmcp import FastMCP  # noqa: E402

from config import charts_dir  # noqa: E402

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOGS_FILE = os.path.join(BASE_DIR, "logs.json")

mcp = FastMCP("charts")


def _read_logs() -> list:
    if not os.path.exists(LOGS_FILE):
        return []
    try:
        with open(LOGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _save_fig(prefix: str) -> str:
    filename = f"{prefix}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
    path = os.path.join(charts_dir(), filename)
    plt.tight_layout()
    plt.savefig(path, dpi=120)
    plt.close()
    return path


@mcp.tool()
def plot_logs_stats(metric: str = "durations", limit: int = 20) -> dict:
    """Строит график статистики прогонов пайплайна по данным logs.json.

    metric:
      "durations"  — длительность прогонов, сек (столбцы по последним limit прогонам);
      "tool_usage" — сколько раз вызывался каждый инструмент (за всю историю);
      "status"     — соотношение успешных и ошибочных прогонов.
    Возвращает путь к PNG-файлу с графиком.
    """
    logs = _read_logs()
    if not logs:
        return {"error": "logs.json пуст или отсутствует — сначала выполните хотя бы один прогон пайплайна"}

    limit = max(1, min(int(limit), 100))

    if metric == "durations":
        runs = logs[-limit:]
        labels = [r.get("timestamp", "")[11:19] for r in runs]
        values = [float(r.get("duration_sec") or 0) for r in runs]
        plt.figure(figsize=(10, 5))
        plt.bar(range(len(values)), values, color="#3b82f6")
        plt.xticks(range(len(values)), labels, rotation=45, ha="right", fontsize=8)
        plt.ylabel("сек")
        plt.title(f"Длительность последних {len(runs)} прогонов")
    elif metric == "tool_usage":
        counter = Counter()
        for run in logs:
            for step in run.get("steps", []):
                if step.get("kind") == "tool_call":
                    counter[step.get("name", "?")] += 1
        if not counter:
            return {"error": "в логах нет вызовов инструментов"}
        plt.figure(figsize=(8, 5))
        names, counts = zip(*counter.most_common())
        plt.bar(names, counts, color="#10b981")
        plt.ylabel("вызовов")
        plt.title("Частота вызовов инструментов (вся история)")
    elif metric == "status":
        counter = Counter(r.get("status", "unknown") for r in logs)
        plt.figure(figsize=(6, 6))
        plt.pie(
            counter.values(),
            labels=counter.keys(),
            autopct="%1.0f%%",
            colors=["#10b981", "#ef4444", "#9ca3af"][: len(counter)],
        )
        plt.title("Статусы прогонов")
    else:
        return {"error": f"неизвестная метрика '{metric}', допустимы: durations, tool_usage, status"}

    path = _save_fig(f"logs_{metric}")
    return {"chart_path": path, "metric": metric, "runs_analyzed": len(logs)}


@mcp.tool()
def plot_series(
    title: str,
    labels: list[str],
    values: list[float],
    chart_type: str = "bar",
) -> dict:
    """Строит график по произвольному числовому ряду.

    Используется, когда LLM сама извлекла данные из результата поиска/обработки.
    labels — подписи точек (строки), values — значения (числа), длины должны совпадать.
    chart_type: "bar" или "line". Возвращает путь к PNG-файлу с графиком.
    """
    if not labels or not values:
        return {"error": "labels и values не должны быть пустыми"}
    if len(labels) != len(values):
        return {"error": f"длины labels ({len(labels)}) и values ({len(values)}) не совпадают"}
    if len(labels) > 50:
        return {"error": "слишком много точек (максимум 50)"}
    try:
        values = [float(v) for v in values]
    except (TypeError, ValueError):
        return {"error": "values должны быть числами"}

    plt.figure(figsize=(10, 5))
    if chart_type == "line":
        plt.plot(range(len(values)), values, marker="o", color="#3b82f6")
    else:
        plt.bar(range(len(values)), values, color="#3b82f6")
    plt.xticks(range(len(values)), [str(l) for l in labels], rotation=45, ha="right", fontsize=8)
    plt.title(str(title))
    plt.grid(axis="y", alpha=0.3)

    path = _save_fig("series")
    return {"chart_path": path, "points": len(values), "chart_type": chart_type}


if __name__ == "__main__":
    mcp.run(transport="stdio")
