import asyncio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

async def main():
    params = StdioServerParameters(command="python", args=["server.py"])

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = await session.list_tools()
            for t in tools.tools:
                print(f"{t.name}: {t.description}")
                print(f"  schema: {t.input_schema}")  # было t.inputSchema

                result = await session.call_tool("multiply", {"a": 6, "b": 7})
                print(result.content[0].text)  # 42

asyncio.run(main())