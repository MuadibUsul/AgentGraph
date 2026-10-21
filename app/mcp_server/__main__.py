"""Entry point: python -m app.mcp_server (stdio transport)."""

from app.mcp_server.server import run_stdio

if __name__ == "__main__":
    run_stdio()
