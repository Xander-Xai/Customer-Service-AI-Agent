"""Standalone MCP stdio server for integration tests.

Run by ``test_mcp_stdio.py`` via the official ``mcp`` stdio client.
Only read-only tools (no write operations).
"""

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("catalog")


@mcp.tool()
def get_product_info(keyword: str) -> dict:
    """Get cosmetic product info by keyword."""
    return {"keyword": keyword, "name": "精华液", "price": 199.0, "category": "skincare"}


@mcp.tool()
def query_order_status(order_id: str) -> dict:
    """Query order status by order id."""
    return {"order_id": order_id, "status": "shipped", "carrier": "SF"}


if __name__ == "__main__":
    mcp.run()
