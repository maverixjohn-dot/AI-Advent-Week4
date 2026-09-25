"""Прямой тест MCP-сервера без LLM: регистрация, схемы, вызов, ошибка."""

import asyncio
import json

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main():
    params = StdioServerParameters(command="python3", args=["mcp_weather_server.py"])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = await session.list_tools()
            for t in tools.tools:
                print("TOOL:", t.name)
                print("  schema:", json.dumps(t.inputSchema, ensure_ascii=False))

            r = await session.call_tool("get_current_weather", {"city": "Москва"})
            print("RESULT:", r.content[0].text)

            r = await session.call_tool(
                "get_weather_forecast", {"city": "Сочи", "days": 2}
            )
            print("RESULT:", r.content[0].text)

            r = await session.call_tool(
                "get_current_weather", {"city": "Несуществующийгород"}
            )
            print("ERROR CASE isError:", r.isError, "|", r.content[0].text)


if __name__ == "__main__":
    asyncio.run(main())
