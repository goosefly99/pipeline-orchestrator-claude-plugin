"""Tool registration seam (spec §3.1, profile-project idiom).

``register_tools(mcp)`` is the single point that wires every ``pipeline_*`` tool
onto the FastMCP instance (spec §3.2/§3.3 — the Node ``tool-schemas.ts`` per-tool
``_meta``+annotations are declared inline on each tool module's
``register_*_tools``; this ``__init__`` is the single wiring point that calls them
all). It fans out to the seven per-domain registrars, attaching the full **43**
tools so the MCP ``initialize``/``tools/list`` handshake enumerates them with the
correct ``_meta.max_result_chars`` and ``ToolAnnotations``:

* ``register_cc_tools`` — 7 ``pipeline_cc_*``
* ``register_synth_tools`` — 6 ``pipeline_synth_*``
* ``register_debate_tools`` — 4 debate tools
* ``register_lifecycle_tools`` — 11 lifecycle tools
* ``register_artifact_tools`` — 6 artifact tools
* ``register_misc_tools`` — 5 misc tools
* ``register_kb_tools`` — 4 ``pipeline_kb_*``

The wiring only makes the tools enumerate with their metadata; functional MCP
dispatch (the DI seam binding the real handler contexts) is exercised elsewhere.
``server.py`` calls this lazily from inside its own function (spec §3.2), so the
module-level imports below introduce no circular-import risk at package import.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pipeline_orchestrator.tools.artifact_tools import register_artifact_tools
from pipeline_orchestrator.tools.cc_tools import register_cc_tools
from pipeline_orchestrator.tools.debate_tools import register_debate_tools
from pipeline_orchestrator.tools.kb_tools import register_kb_tools
from pipeline_orchestrator.tools.lifecycle_tools import register_lifecycle_tools
from pipeline_orchestrator.tools.misc_tools import register_misc_tools
from pipeline_orchestrator.tools.synth_tools import register_synth_tools

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP


def register_tools(mcp: FastMCP) -> None:
    """Wire the full 43 ``pipeline_*`` tools onto ``mcp`` (spec §3.2/§3.3).

    Fans out to the seven per-domain registrars (7 + 6 + 4 + 11 + 6 + 5 + 4 =
    43). Registration is metadata-only and env-free, so it is safe to call from
    the MCP handshake path.
    """
    register_cc_tools(mcp)
    register_synth_tools(mcp)
    register_debate_tools(mcp)
    register_lifecycle_tools(mcp)
    register_artifact_tools(mcp)
    register_misc_tools(mcp)
    register_kb_tools(mcp)
