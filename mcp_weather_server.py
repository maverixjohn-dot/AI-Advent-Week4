"""MCP-сервер вокруг Open-Meteo API: периодический сбор погоды и отчёты.

Инструменты:
  - get_current_weather        — текущая погода в городе
  - get_weather_forecast       — прогноз на N дней
  - start_weather_collection   — каждые N минут собирать погоду города M в JSON
  - stop_weather_collection    — остановить сбор
  - set_report_schedule        — настроить расписание отчёта (интервал или cron)
  - get_new_reports            — забрать сгенерированные по расписанию отчёты
  - get_latest_report          — агрегированный отчёт по данным из JSON

Хранение: JSON-файл (по умолчанию weather_data.json рядом с сервером,
переопределяется DATA_JSON_PATH). Планировщик — APScheduler внутри сервера.
Настройки сбора и расписания хранятся в том же JSON и восстанавливаются
при рестарте.

ВАЖНО: логи — только в stderr. stdout занят JSON-RPC транспортом MCP,
любой print в stdout ломает протокол.

Транспорт: stdio. Open-Meteo не требует API-ключа: https://open-meteo.com/
"""

import json
import os
import sys
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from mcp.server.fastmcp import FastMCP

GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

DATA_JSON_PATH = os.environ.get(
    "DATA_JSON_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "weather_data.json"),
)

# Коды WMO, при которых зонт актуален: морось, дождь, ливень, гроза
RAIN_CODES = {51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82, 95, 96, 99}

# Расшифровка кодов погоды WMO (https://open-meteo.com/en/docs)
WMO_CODES = {
    0: "ясно", 1: "преимущественно ясно", 2: "переменная облачность", 3: "пасмурно",
    45: "туман", 48: "изморозь",
    51: "лёгкая морось", 53: "морось", 55: "сильная морось",
    56: "ледяная морось", 57: "сильная ледяная морось",
    61: "небольшой дождь", 63: "дождь", 65: "сильный дождь",
    66: "ледяной дождь", 67: "сильный ледяной дождь",
    71: "небольшой снег", 73: "снег", 75: "сильный снег", 77: "снежная крупа",
    80: "небольшой ливень", 81: "ливень", 82: "сильный ливень",
    85: "небольшой снегопад", 86: "снегопад",
    95: "гроза", 96: "гроза с градом", 99: "гроза с сильным градом",
}

COLLECT_JOB_ID = "collect"
REPORT_JOB_ID = "report"

scheduler = AsyncIOScheduler()


# ---------------------------------------------------------------------------
# JSON-хранилище
# ---------------------------------------------------------------------------

def _default_data() -> dict:
    return {
        "observations": [],   # замеры погоды
        "reports": [],        # сгенерированные по расписанию отчёты
        "config": {           # настройки, восстанавливаются при рестарте
            "collection": None,      # {"city": ..., "interval_minutes": ...}
            "report_schedule": None, # {"interval_minutes": ...} или {"cron": ...}
        },
    }


def _load() -> dict:
    if not os.path.exists(DATA_JSON_PATH):
        return _default_data()
    with open(DATA_JSON_PATH, encoding="utf-8") as f:
        data = json.load(f)
    base = _default_data()
    base.update(data)
    base["config"] = {**_default_data()["config"], **data.get("config", {})}
    return base


def _save(data: dict) -> None:
    """Атомарная запись: временный файл + rename, чтобы не получить
    обрезанный JSON при падении посреди записи."""
    fd, tmp = tempfile.mkstemp(
        dir=os.path.dirname(DATA_JSON_PATH) or ".", suffix=".tmp"
    )
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, DATA_JSON_PATH)


def _store_observation(entry: dict) -> None:
    data = _load()
    data["observations"].append(entry)
    _save(data)


# ---------------------------------------------------------------------------
# Open-Meteo
# ---------------------------------------------------------------------------

async def _geocode(city: str) -> dict:
    """Геокодирование: название города -> координаты."""
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            GEOCODING_URL,
            params={"name": city, "count": 1, "language": "ru", "format": "json"},
        )
        resp.raise_for_status()
        results = resp.json().get("results") or []
        if not results:
            raise ValueError(f"Город не найден: {city!r}")
        return results[0]


def _weather_text(code: int | None) -> str:
    return WMO_CODES.get(code, f"код погоды {code}")


async def _fetch_current(city: str) -> dict:
    """Сырые текущие данные по городу (включая осадки и код погоды)."""
    place = await _geocode(city)
    lat, lon = place["latitude"], place["longitude"]

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            FORECAST_URL,
            params={
                "latitude": lat,
                "longitude": lon,
                "current": "temperature_2m,relative_humidity_2m,apparent_temperature,"
                           "precipitation,weather_code,wind_speed_10m",
                "timezone": "auto",
            },
        )
        resp.raise_for_status()
        cur = resp.json()["current"]

    return {
        "city": place["name"],
        "country": place.get("country", ""),
        "latitude": lat,
        "longitude": lon,
        "time": cur["time"],
        "temperature_c": cur["temperature_2m"],
        "apparent_temperature_c": cur["apparent_temperature"],
        "humidity_percent": cur["relative_humidity_2m"],
        "wind_speed_kmh": cur["wind_speed_10m"],
        "precipitation_mm": cur.get("precipitation"),
        "weather_code": cur.get("weather_code"),
        "conditions": _weather_text(cur.get("weather_code")),
    }


# ---------------------------------------------------------------------------
# Агрегация и отчёты
# ---------------------------------------------------------------------------

def _aggregate(city: str, observations: list[dict],
               period_from: str | None, period_to: str) -> dict:
    """Агрегированный отчёт по списку замеров."""
    if not observations:
        return {
            "city": city,
            "period_from": period_from,
            "period_to": period_to,
            "checks": 0,
            "note": "За период замеров нет",
        }

    temps = [o["temperature_c"] for o in observations
             if o.get("temperature_c") is not None]
    precips = [o.get("precipitation_mm") or 0 for o in observations]
    rainy = sum(
        1 for o in observations
        if o.get("weather_code") in RAIN_CODES or (o.get("precipitation_mm") or 0) > 0
    )
    last = observations[-1]
    raining_now = bool(
        last.get("weather_code") in RAIN_CODES
        or (last.get("precipitation_mm") or 0) > 0
    )

    # Частотность состояний: «дождь ×3, ясно ×2»
    freq: dict[str, int] = {}
    for o in observations:
        key = _weather_text(o.get("weather_code"))
        freq[key] = freq.get(key, 0) + 1

    return {
        "city": city,
        "period_from": period_from or observations[0]["ts_utc"],
        "period_to": period_to,
        "checks": len(observations),
        "temp_avg_c": round(sum(temps) / len(temps), 1) if temps else None,
        "temp_min_c": min(temps) if temps else None,
        "temp_max_c": max(temps) if temps else None,
        "total_precipitation_mm": round(sum(precips), 2),
        "rainy_checks": rainy,
        "rain_share_pct": round(100 * rainy / len(observations), 1),
        "conditions_freq": dict(sorted(freq.items(), key=lambda kv: -kv[1])),
        "last_check": {
            "local_time": last.get("time") or last.get("local_time"),
            "temperature_c": last.get("temperature_c"),
            "precipitation_mm": last.get("precipitation_mm"),
            "conditions": _weather_text(last.get("weather_code")),
        },
        "raining_now": raining_now,
        "umbrella": "нужен" if raining_now else "не нужен",
    }


def _generate_report() -> dict | None:
    """Генерация отчёта по расписанию: агрегирует замеры с момента
    предыдущего отчёта, сохраняет в JSON, возвращает отчёт."""
    data = _load()
    collection = data["config"].get("collection")
    if not collection:
        print("[report] сбор не настроен, отчёт пропущен", file=sys.stderr,
              flush=True)
        return None

    city = collection["city"]
    prev_reports = data["reports"]
    period_from = prev_reports[-1]["period_to"] if prev_reports else None

    observations = [
        o for o in data["observations"]
        if o.get("city") == city
        and (period_from is None or o["ts_utc"] > period_from)
    ]
    now_utc = datetime.now(timezone.utc).isoformat(timespec="seconds")
    report = _aggregate(city, observations, period_from, now_utc)
    report["generated_at"] = now_utc
    report["delivered"] = False

    data["reports"].append(report)
    _save(data)
    print(f"[report] отчёт сгенерирован: {city}, замеров {report['checks']}",
          file=sys.stderr, flush=True)
    return report


# ---------------------------------------------------------------------------
# Периодические задачи
# ---------------------------------------------------------------------------

async def _collect(city: str) -> None:
    """Один цикл сбора: снять показания и сохранить в JSON."""
    try:
        data = await _fetch_current(city)
        entry = {
            "city": data["city"],
            "ts_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "time": data["time"],
            "temperature_c": data["temperature_c"],
            "apparent_temperature_c": data["apparent_temperature_c"],
            "humidity_percent": data["humidity_percent"],
            "wind_speed_kmh": data["wind_speed_kmh"],
            "precipitation_mm": data["precipitation_mm"],
            "weather_code": data["weather_code"],
            "conditions": data["conditions"],
        }
        _store_observation(entry)
        print(f"[collect] {data['city']}: {data['conditions']}, "
              f"{data['temperature_c']} °C, {data['precipitation_mm']} мм",
              file=sys.stderr, flush=True)
    except Exception as exc:
        # Сбой одного цикла не должен убивать планировщик
        print(f"[collect] {city}: ошибка цикла: {exc}", file=sys.stderr,
              flush=True)


def _schedule_collection(city: str, interval_minutes: float) -> None:
    scheduler.add_job(
        _collect,
        IntervalTrigger(minutes=interval_minutes),
        args=[city],
        id=COLLECT_JOB_ID,
        replace_existing=True,
        next_run_time=datetime.now(),  # первый замер — сразу, не ждём интервал
    )


def _schedule_report(interval_minutes: float | None, cron: str | None) -> None:
    trigger = (
        CronTrigger.from_crontab(cron) if cron
        else IntervalTrigger(minutes=interval_minutes)
    )
    scheduler.add_job(
        _generate_report,
        trigger,
        id=REPORT_JOB_ID,
        replace_existing=True,
    )


def _restore_from_config() -> None:
    """Восстановление сбора и расписания отчётов из JSON при старте."""
    cfg = _load()["config"]
    collection = cfg.get("collection")
    if collection:
        _schedule_collection(collection["city"], collection["interval_minutes"])
        print(f"[config] восстановлен сбор: {collection['city']} каждые "
              f"{collection['interval_minutes']} мин", file=sys.stderr, flush=True)
    report = cfg.get("report_schedule")
    if report:
        _schedule_report(report.get("interval_minutes"), report.get("cron"))
        print(f"[config] восстановлено расписание отчёта: {report}",
              file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# MCP-сервер
# ---------------------------------------------------------------------------

@asynccontextmanager
async def _lifespan(server: FastMCP) -> AsyncIterator[None]:
    _restore_from_config()
    scheduler.start()
    yield
    scheduler.shutdown(wait=False)


mcp = FastMCP("weather-server", lifespan=_lifespan)


@mcp.tool()
async def get_current_weather(city: str) -> dict:
    """Текущая погода в указанном городе.

    Args:
        city: Название города (например: "Москва", "Berlin", "Токио").

    Returns:
        Словарь с городом, координатами, температурой, ощущаемой температурой,
        влажностью, осадками, скоростью ветра и описанием погоды.
    """
    return await _fetch_current(city)


@mcp.tool()
async def get_weather_forecast(city: str, days: int = 3) -> dict:
    """Прогноз погоды в указанном городе на несколько дней.

    Args:
        city: Название города (например: "Санкт-Петербург", "Paris").
        days: Число дней прогноза, от 1 до 16 (по умолчанию 3).

    Returns:
        Словарь с городом и списком дней: дата, мин/макс температура,
        сумма осадков и описание погоды.
    """
    if not 1 <= days <= 16:
        raise ValueError("days должен быть в диапазоне 1..16")

    place = await _geocode(city)
    lat, lon = place["latitude"], place["longitude"]

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            FORECAST_URL,
            params={
                "latitude": lat,
                "longitude": lon,
                "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum,weather_code",
                "forecast_days": days,
                "timezone": "auto",
            },
        )
        resp.raise_for_status()
        daily = resp.json()["daily"]

    forecast = [
        {
            "date": date,
            "temp_min_c": tmin,
            "temp_max_c": tmax,
            "precipitation_mm": prec,
            "conditions": _weather_text(code),
        }
        for date, tmin, tmax, prec, code in zip(
            daily["time"],
            daily["temperature_2m_min"],
            daily["temperature_2m_max"],
            daily["precipitation_sum"],
            daily["weather_code"],
        )
    ]

    return {
        "city": place["name"],
        "country": place.get("country", ""),
        "latitude": lat,
        "longitude": lon,
        "forecast": forecast,
    }


@mcp.tool()
async def start_weather_collection(city: str, interval_minutes: float = 15) -> dict:
    """Запустить периодический сбор погоды города в JSON-файл.

    Каждые interval_minutes минут сервер опрашивает Open-Meteo и дописывает
    замер в JSON. Первый замер выполняется сразу. Настройка сохраняется
    и восстанавливается после рестарта сервера. Одновременно идёт сбор
    только по одному городу — повторный вызов заменяет предыдущий.

    Args:
        city: Название города M (например: "Санкт-Петербург").
        interval_minutes: Интервал сбора N в минутах, от 0.05 до 1440
            (по умолчанию 15).

    Returns:
        Подтверждение с городом, интервалом и результатом первого замера.
    """
    if not 0.05 <= interval_minutes <= 1440:
        raise ValueError("interval_minutes должен быть в диапазоне 0.05..1440")

    data = await _fetch_current(city)  # валидация города до регистрации задачи
    canonical = data["city"]

    store = _load()
    store["config"]["collection"] = {
        "city": canonical, "interval_minutes": interval_minutes,
    }
    _save(store)

    _schedule_collection(canonical, interval_minutes)
    await _collect(canonical)  # первый замер сразу

    return {
        "collecting": canonical,
        "interval_minutes": interval_minutes,
        "first_check": data,
    }


@mcp.tool()
async def stop_weather_collection() -> dict:
    """Остановить периодический сбор погоды.

    Returns:
        Подтверждение остановки.
    """
    job = scheduler.get_job(COLLECT_JOB_ID)
    if job:
        job.remove()

    store = _load()
    had_config = store["config"].get("collection") is not None
    store["config"]["collection"] = None
    _save(store)

    if job is None and not had_config:
        raise ValueError("Сбор не был запущен")
    return {"stopped": True}


@mcp.tool()
async def set_report_schedule(
    interval_minutes: float | None = None,
    cron: str | None = None,
) -> dict:
    """Настроить расписание агрегированного отчёта.

    Укажите ровно один из параметров. По расписанию сервер агрегирует
    замеры, накопленные с момента предыдущего отчёта, и сохраняет отчёт
    в JSON (забрать: get_new_reports / get_latest_report).
    Настройка сохраняется и восстанавливается после рестарта сервера.

    Args:
        interval_minutes: Интервал между отчётами в минутах,
            от 0.05 до 10080 (например: 60 — каждый час).
        cron: Cron-выражение из 5 полей (например: "0 9 * * *" —
            каждый день в 09:00, "0 */6 * * *" — каждые 6 часов).

    Returns:
        Подтверждение с действующим расписанием.
    """
    if (interval_minutes is None) == (cron is None):
        raise ValueError("Укажите ровно один параметр: interval_minutes ИЛИ cron")
    if interval_minutes is not None and not 0.05 <= interval_minutes <= 10080:
        raise ValueError("interval_minutes должен быть в диапазоне 0.05..10080")

    _schedule_report(interval_minutes, cron)  # упадёт здесь, если cron кривой

    schedule = (
        {"cron": cron} if cron else {"interval_minutes": interval_minutes}
    )
    store = _load()
    store["config"]["report_schedule"] = schedule
    _save(store)
    return {"report_schedule": schedule}


@mcp.tool()
async def get_config() -> dict:
    """Текущая конфигурация: параметры сбора и расписание отчётов.

    Returns:
        Словарь {"collection": {...} | None, "report_schedule": {...} | None}.
    """
    return _load()["config"]


@mcp.tool()
async def get_new_reports() -> dict:
    """Забрать отчёты, сгенерированные по расписанию и ещё не выданные.

    Returns:
        Список новых отчётов; они помечаются выданными, повторный вызов
        вернёт только следующие.
    """
    store = _load()
    new = [r for r in store["reports"] if not r.get("delivered")]
    for r in store["reports"]:
        r["delivered"] = True
    _save(store)
    return {"new_reports": new, "count": len(new)}


@mcp.tool()
async def get_latest_report() -> dict:
    """Последний агрегированный отчёт из JSON.

    Если отчёты по расписанию ещё не генерировались — отчёт строится
    на лету по всем накопленным замерам.

    Returns:
        Агрегированный отчёт: число замеров, средняя/мин/макс температура,
        сумма осадков, доля дождя, частотность состояний, текущее состояние
        и рекомендация по зонту.
    """
    store = _load()
    if store["reports"]:
        return store["reports"][-1]

    collection = store["config"].get("collection")
    if not collection:
        return {"note": "Сбор не настроен — вызовите start_weather_collection"}
    return _aggregate(
        collection["city"],
        [o for o in store["observations"] if o.get("city") == collection["city"]],
        period_from=None,
        period_to=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )


if __name__ == "__main__":
    mcp.run(transport="stdio")
