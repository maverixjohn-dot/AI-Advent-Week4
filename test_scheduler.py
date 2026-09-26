"""Тесты периодического сбора погоды и отчётов по расписанию.

Часть 1 (юнит, без сети): агрегация, генерация отчёта, дедупликация выдачи,
восстановление настроек из JSON при рестарте.
Часть 2 (интеграция через MCP): сбор с интервалом 3 секунды, отчёт с
интервалом 6 секунд, проверка JSON-файла, get_new_reports, остановка.

Запуск: python3 test_scheduler.py
"""

import asyncio
import json
import os
import sys
import tempfile

# JSON-хранилище во временном файле, чтобы не трогать боевое
_tmp = tempfile.mkdtemp()
os.environ["DATA_JSON_PATH"] = os.path.join(_tmp, "test_data.json")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import mcp_weather_server as srv

CITY = "Тестгород"


def fake_obs(raining: bool, temp: float, ts: str) -> dict:
    return {
        "city": CITY, "ts_utc": ts, "time": ts,
        "temperature_c": temp, "apparent_temperature_c": temp,
        "humidity_percent": 80, "wind_speed_kmh": 3.0,
        "precipitation_mm": 1.5 if raining else 0.0,
        "weather_code": 61 if raining else 2,
        "conditions": "дождь" if raining else "переменная облачность",
    }


def _ts(minutes_ago: float) -> str:
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat(
        timespec="seconds"
    )


def part1_unit():
    # наполняем JSON замерами (метки времени — в прошлом от «сейчас»,
    # чтобы попасть в период первого отчёта)
    srv._store_observation(fake_obs(False, 10.0, _ts(12)))
    srv._store_observation(fake_obs(True, 12.0, _ts(9)))
    srv._store_observation(fake_obs(True, 14.0, _ts(6)))
    srv._store_observation(fake_obs(False, 15.0, _ts(3)))

    # настройка сбора в конфиг (иначе _generate_report пропустит)
    store = srv._load()
    store["config"]["collection"] = {"city": CITY, "interval_minutes": 3}
    srv._save(store)

    report = srv._generate_report()
    assert report["checks"] == 4, report
    assert report["temp_avg_c"] == 12.8, report["temp_avg_c"]
    assert report["temp_min_c"] == 10.0 and report["temp_max_c"] == 15.0
    assert report["total_precipitation_mm"] == 3.0
    assert report["rainy_checks"] == 2 and report["rain_share_pct"] == 50.0
    assert report["umbrella"] == "не нужен"  # последний замер сухой
    assert report["delivered"] is False
    print("part1: агрегация отчёта OK:",
          {k: report[k] for k in ("checks", "temp_avg_c",
                                  "total_precipitation_mm", "rain_share_pct")})

    # следующий отчёт — только по НОВЫМ замерам (period_from = конец предыдущего).
    # Метки с точностью до секунды — ждём, чтобы ts нового замера стал строго
    # больше period_to предыдущего отчёта.
    import time
    time.sleep(1.1)
    srv._store_observation(fake_obs(True, 9.0, _ts(0)))
    report2 = srv._generate_report()
    assert report2["checks"] == 1, report2
    assert report2["umbrella"] == "нужен"  # последний замер — дождь
    print("part1: инкрементальный отчёт OK (только новые замеры, зонт=нужен)")

    # JSON реально на диске и валиден
    with open(os.environ["DATA_JSON_PATH"], encoding="utf-8") as f:
        on_disk = json.load(f)
    assert len(on_disk["observations"]) == 5
    assert len(on_disk["reports"]) == 2
    print("part1: JSON-файл на диске OK (5 замеров, 2 отчёта)")


async def part2_restore():
    # имитация рестарта: сбор и расписание в конфиге -> задачи в планировщике
    store = srv._load()
    store["config"]["collection"] = {"city": CITY, "interval_minutes": 10}
    store["config"]["report_schedule"] = {"cron": "0 9 * * *"}
    srv._save(store)

    srv.scheduler.start()
    srv._restore_from_config()
    assert srv.scheduler.get_job(srv.COLLECT_JOB_ID) is not None
    assert srv.scheduler.get_job(srv.REPORT_JOB_ID) is not None
    srv.scheduler.shutdown(wait=False)
    print("part2: восстановление сбора и cron-расписания из JSON OK")


async def part3_mcp_integration():
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    env = dict(os.environ)  # наследуем DATA_JSON_PATH
    params = StdioServerParameters(
        command="python3", args=["mcp_weather_server.py"], env=env
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = {t.name for t in (await session.list_tools()).tools}
            expected = {"get_current_weather", "get_weather_forecast",
                        "start_weather_collection", "stop_weather_collection",
                        "set_report_schedule", "get_new_reports",
                        "get_latest_report"}
            assert expected <= tools, tools
            print("part3: инструменты:", sorted(tools))

            # валидация: нельзя задать оба параметра расписания
            r = await session.call_tool(
                "set_report_schedule",
                {"interval_minutes": 60, "cron": "0 9 * * *"},
            )
            assert r.isError, "оба параметра расписания должны давать ошибку"
            print("part3: валидация «interval ИЛИ cron» OK")

            r = await session.call_tool(
                "start_weather_collection",
                {"city": "Москва", "interval_minutes": 0.05},  # 3 секунды
            )
            start = json.loads(r.content[0].text)
            print("part3: сбор запущен ->", start["collecting"],
                  start["interval_minutes"], "мин")

            r = await session.call_tool(
                "set_report_schedule", {"interval_minutes": 0.1}  # 6 секунд
            )
            print("part3: расписание отчёта ->", r.content[0].text.strip())

            # ждём: >=2 замеров и >=1 отчёта по расписанию
            await asyncio.sleep(10)

            r = await session.call_tool("get_new_reports", {})
            new = json.loads(r.content[0].text)
            assert new["count"] >= 1, new
            rep = new["new_reports"][0]
            assert rep["checks"] >= 2, rep
            print("part3: отчёт по расписанию получен ->",
                  {k: rep[k] for k in ("checks", "temp_avg_c",
                                       "rain_share_pct", "umbrella")})

            # повторный вызов — только следующие отчёты
            r = await session.call_tool("get_new_reports", {})
            again = json.loads(r.content[0].text)
            print("part3: повторный get_new_reports ->", again["count"],
                  "(0 или число отчётов, родившихся за эти секунды)")

            r = await session.call_tool("get_latest_report", {})
            latest = json.loads(r.content[0].text)
            assert "checks" in latest
            print("part3: get_latest_report OK")

            r = await session.call_tool("stop_weather_collection", {})
            assert not r.isError
            print("part3: сбор остановлен")

    print("part3: интеграционный прогон через MCP OK")


if __name__ == "__main__":
    part1_unit()
    asyncio.run(part2_restore())
    asyncio.run(part3_mcp_integration())
    print("ВСЕ ТЕСТЫ ПРОШЛИ")
