from fastmcp import FastMCP

mcp = FastMCP("Demo backend")


@mcp.tool()
def echo(message: str) -> str:
    """Return a message unchanged."""
    return message


if __name__ == "__main__":
    mcp.run(transport="streamable-http", host="0.0.0.0", port=8123)
