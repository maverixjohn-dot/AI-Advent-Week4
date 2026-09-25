"""MCP-сервер вокруг Open-Meteo API (погода).

Регистрирует два инструмента:
  - get_current_weather  — текущая погода в городе
  - get_weather_forecast — прогноз на N дней

Транспорт: stdio (сервер запускается приложением как подпроцесс).
Open-Meteo не требует API-ключа: https://open-meteo.com/
"""

import httpx
from mcp.server.fastmcp import FastMCP

GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

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

mcp = FastMCP("weather-server")


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


@mcp.tool()
async def get_current_weather(city: str) -> dict:
    """Текущая погода в указанном городе.

    Args:
        city: Название города (например: "Москва", "Berlin", "Токио").

    Returns:
        Словарь с городом, координатами, температурой, ощущаемой температурой,
        влажностью, скоростью ветра и текстовым описанием погоды.
    """
    place = await _geocode(city)
    lat, lon = place["latitude"], place["longitude"]

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            FORECAST_URL,
            params={
                "latitude": lat,
                "longitude": lon,
                "current": "temperature_2m,relative_humidity_2m,apparent_temperature,"
                           "weather_code,wind_speed_10m",
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
        "conditions": _weather_text(cur.get("weather_code")),
    }


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


if __name__ == "__main__":
    mcp.run(transport="stdio")
