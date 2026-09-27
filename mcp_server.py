"""
MCP-сервер учебного пайплайна.

Инструменты:
    search       — получает данные (веб-поиск DuckDuckGo, ключ не нужен)
    summarize    — обрабатывает текст (алгоритмическая выжимка, без LLM)
    save_to_file — сохраняет результат (дописывает запись в results.json)

Запуск: python mcp_server.py  (транспорт stdio; обычно поднимается клиентом из agent.py)
"""

import json
import os
import re
import tempfile
from collections import Counter
from datetime import datetime, timezone
from typing import Any

from mcp.server.fastmcp import FastMCP

try:  # ddgs — актуальное имя пакета, duckduckgo_search — легаси-фолбэк
    from ddgs import DDGS
except ImportError:  # pragma: no cover
    from duckduckgo_search import DDGS

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RESULTS_FILE = os.path.join(BASE_DIR, "results.json")

mcp = FastMCP("learning-pipeline")

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

_STOPWORDS = set(
    "и в во не что он на я с со как а то все она так его но да ты к у же вы за бы по "
    "только ее мне было вот от меня еще нет о из ему теперь когда даже ну вдруг ли "
    "если уже или ни быть был него до вас нибудь опять уж вам ведь там потом себя "
    "ничего ей может они тут где есть надо ней для мы тебя их чем была сам чтоб без "
    "будто чего раз тоже себе под будет ж тогда кто этот того потому этого какой "
    "совсем ним здесь этом один почти мой тем чтобы нее сейчас были куда зачем всех "
    "никогда можно при наконец два об другой хоть после над больше тот через эти нас "
    "про всего них какая много разве три эту моя впрочем хорошо свою этой перед "
    "иногда лучше чуть том нельзя такой им более всегда конечно всю между это "
    "the a an and or but of to in on for with is are was were be been it this that "
    "at by from as at its his her their our your".split()
)


def _read_json_array(path: str) -> list:
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _atomic_write_json(path: str, data: Any) -> None:
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _words(text: str) -> list[str]:
    return re.findall(r"[A-Za-zА-Яа-яЁё0-9]+", text.lower())


def _split_sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?…])\s+|\n+", text.strip())
    return [p.strip() for p in parts if len(p.strip()) > 20]


# ---------------------------------------------------------------------------
# tools
# ---------------------------------------------------------------------------

@mcp.tool()
def search(query: str, max_results: int = 5) -> dict:
    """Ищет информацию в вебе через DuckDuckGo.

    Возвращает список результатов: заголовок, URL и сниппет.
    max_results ограничен диапазоном 1..10.
    """
    max_results = max(1, min(int(max_results), 10))
    with DDGS() as ddgs:
        raw = list(ddgs.text(query, max_results=max_results))
    results = [
        {
            "title": r.get("title", ""),
            "url": r.get("href", ""),
            "snippet": r.get("body", ""),
        }
        for r in raw
    ]
    return {"query": query, "count": len(results), "results": results}


@mcp.tool()
def summarize(text: str, max_sentences: int = 3) -> dict:
    """Алгоритмическая суммаризация текста без обращения к LLM.

    Частотный scoring предложений: выбираются max_sentences предложений
    с наибольшей плотностью значимых слов, порядок сохраняется.
    """
    max_sentences = max(1, min(int(max_sentences), 10))
    sentences = _split_sentences(text)
    if not sentences:
        return {
            "summary": text.strip()[:500],
            "sentences_total": 0,
            "sentences_used": 1 if text.strip() else 0,
            "method": "truncation",
        }

    freq = Counter(
        w for w in _words(text) if w not in _STOPWORDS and len(w) > 2
    )
    scored = []
    for idx, sentence in enumerate(sentences):
        words = [w for w in _words(sentence) if w not in _STOPWORDS and len(w) > 2]
        score = sum(freq[w] for w in words) / max(1, len(words))
        scored.append((idx, score, sentence))

    top = sorted(scored, key=lambda x: x[1], reverse=True)[:max_sentences]
    top.sort(key=lambda x: x[0])  # восстанавливаем исходный порядок

    return {
        "summary": " ".join(s for _, _, s in top),
        "sentences_total": len(sentences),
        "sentences_used": len(top),
        "method": "frequency-scoring",
    }


@mcp.tool()
def save_to_file(data: dict) -> dict:
    """Сохраняет запись в results.json (дописывает объект в JSON-массив).

    К записи добавляется timestamp (UTC). Возвращает путь к файлу
    и текущее число записей.
    """
    if not isinstance(data, dict):
        raise ValueError("data must be a JSON object")
    records = _read_json_array(RESULTS_FILE)
    records.append({"timestamp": datetime.now(timezone.utc).isoformat(), **data})
    _atomic_write_json(RESULTS_FILE, records)
    return {"saved_to": RESULTS_FILE, "records_total": len(records)}


if __name__ == "__main__":
    mcp.run(transport="stdio")
