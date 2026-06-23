"""Tool registration seam (spec §3.1, profile-project idiom).

``register_tools(mcp)`` is the single point that wires every ``pipeline_*`` tool
onto the FastMCP instance. In M0 it is a deliberate **no-op stub**: it registers
**zero** tools, so the MCP ``initialize``/``tools/list`` handshake returns an
empty tool list cleanly. The 43 tools land across M3–M6, each adding a
``register_*_tools`` call here (T6.5 is the sole owner of the full wiring).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP


def register_tools(mcp: FastMCP) -> None:
    """Register zero tools (M0 stub — the M6 wiring seam).

    No ``register_*`` calls yet; the parameter is intentionally unused until
    tool modules land.
    """
    _ = mcp
