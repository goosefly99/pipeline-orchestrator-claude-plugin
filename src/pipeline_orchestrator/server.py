from __future__ import annotations

import logging
import sys

import structlog
from mcp.server.fastmcp import FastMCP

# Frozen workflow blurb (R1 §0 / spec §3.2) — preserved verbatim from the Node
# server identity (server.ts `new Server({ name: 'pipeline' }, { instructions })`).
# Split across adjacent string literals (auto-concatenated) only to keep source
# lines under the 88-char ruff cap; the assembled value is byte-identical.
INSTRUCTIONS = (
    "Pipeline orchestrator MCP server. "
    "Reads pipeline.toml to understand phases and DAG edges. "
    "Initialize a run, resolve available phases, validate artifacts, "
    "and manage artifact storage. "
    "Includes embedded concept extraction (pipeline_cc_*), "
    "research synthesis (pipeline_synth_*), and feature request tools. "
    "Workflow: pipeline_init_run → pipeline_next_phases → "
    "pipeline_start_phase → (do work) → "
    "pipeline_validate_artifact → pipeline_store_artifact → "
    "pipeline_complete_phase → repeat."
)

# Frozen identity name (spec §2): the in-code `Server` identity is `pipeline`.
mcp: FastMCP = FastMCP("pipeline", instructions=INSTRUCTIONS)

_LOG_HANDLER_TAG = "pipeline_orchestrator_stderr"


def configure_logging() -> None:
    """Configure structlog so all logs go to sys.stderr only.

    stdout is the JSON-RPC framing channel under stdio transport; writing
    anything to stdout corrupts the protocol stream (spec §4.5/§15).
    Idempotent: a tagged StreamHandler(sys.stderr) is installed at most once.
    """
    root = logging.getLogger()
    # Only this module's own tagged handler is de-duplicated; any pre-existing
    # foreign StreamHandler(sys.stderr) is intentionally left in place
    # (stderr is protocol-safe; we do not remove other libraries' handlers).
    already_installed = any(
        getattr(h, "_pipeline_orchestrator_tag", None) == _LOG_HANDLER_TAG
        for h in root.handlers
    )
    if not already_installed:
        handler = logging.StreamHandler(stream=sys.stderr)
        handler._pipeline_orchestrator_tag = _LOG_HANDLER_TAG  # type: ignore[attr-defined]
        handler.setFormatter(
            structlog.stdlib.ProcessorFormatter(
                processor=structlog.processors.JSONRenderer(),
            )
        )
        root.addHandler(handler)
        root.setLevel(logging.INFO)
        structlog.configure(
            processors=[
                structlog.contextvars.merge_contextvars,
                structlog.processors.add_log_level,
                structlog.processors.TimeStamper(fmt="iso", utc=True),
                structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
            ],
            logger_factory=structlog.stdlib.LoggerFactory(),
            wrapper_class=structlog.stdlib.BoundLogger,
            cache_logger_on_first_use=True,
        )


def register_tools(mcp: FastMCP) -> None:
    """Register every pipeline_* tool onto the given FastMCP instance.

    Delegates to the tools subpackage (currently a no-op stub; the 43 tools
    land in M6). This is the single wiring seam.
    """
    from pipeline_orchestrator.tools import (
        register_tools as _register_tools,  # noqa: PLC0415
    )

    _register_tools(mcp)


def main() -> None:
    """Entry point: configure logging, register tools, run over stdio.

    Config (SECOND_BRAIN_*, AGENTIC_OS_DATA, PIPELINE_* etc.) is parsed lazily
    on the first tool call that needs it rather than at import time, so an MCP
    ``initialize``/``tools/list`` handshake succeeds even with no
    ``pipeline.toml`` resolved (the tools themselves return contract-honest
    error envelopes).
    """
    configure_logging()
    register_tools(mcp)
    # Startup line goes to stderr only — never stdout (protocol-safe, spec §3.2).
    sys.stderr.write("pipeline: MCP server started\n")
    mcp.run()
