"""MCP stdio server so an agent can look up companies directly."""
from mcp.server.fastmcp import FastMCP

import companies_db as db

mcp = FastMCP("companies")


@mcp.tool()
def search_companies(name: str, limit: int = 10, active_only: bool = False) -> list[dict]:
    """Find UK companies whose name (or a previous name) contains `name`.
    Returns company number, status, category and SIC classification codes."""
    return db.search(name, min(limit, 50), active_only)


@mcp.tool()
def search_by_postcode(postcode: str, sic: str = "", limit: int = 50, active_only: bool = True) -> dict:
    """List UK companies in a postcode area, district or full postcode (e.g. 'S17', 'S17 3GW').
    Optional `sic` filters by SIC code prefix or description text (e.g. '62' or 'consultancy').
    Returns the total match count plus up to `limit` companies."""
    return db.by_postcode(postcode, sic or None, min(limit, 200), active_only)


@mcp.tool()
def get_company(number: str) -> dict | None:
    """Get full details (SIC classification, status, address) for a company number."""
    return db.get(number)


if __name__ == "__main__":
    mcp.run()
