from mcp.server.mcpserver import MCPServer

mcp = MCPServer("demo")

@mcp.tool()
def add(a: int, b: int) -> int:
    """Складывает два числа."""
    return a + b

@mcp.tool()
def multiply(a: int, b: int) -> int:
    """Перемножает два числа."""
    return a * b

@mcp.tool()
def echo(text: str) -> str:
    """Возвращает текст обратно."""
    return text

if __name__ == "__main__":
    mcp.run()