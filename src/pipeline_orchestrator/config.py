"""Lazy, process-cached configuration readers (spec §10, obsidian-mcp idiom).

Every reader is a zero-arg ``@functools.cache`` function that consults
``os.environ`` only on first call — **never at import time**. Constructing the
FastMCP server (``server.py``) imports this module but calls none of these
readers, so an MCP ``initialize``/``tools/list`` handshake resolves no config
(spec §3.2 handshake-safety; pinned by ``tests/test_server_startup.py`` case 4).

The full §10.2 env table is covered here. ``pipeline.toml`` resolution (the
``[pipeline]``/``[phases]``/``[edges]`` config) is a separate, also-lazy concern
landing in ``toml_loader.py`` (M1, T1.5); this module is the env-var leg only.

Errors are surfaced as ``ConfigError`` envelopes by the tool layer (M6), never
raised across the MCP boundary.
"""

from __future__ import annotations

import functools
import os
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pipeline_orchestrator.models import PipelineConfig


class ConfigError(Exception):
    """Structured config error carrying an envelope error_code (obsidian-mcp idiom).

    The tool layer renders this as a contract envelope; it is never raised across
    the MCP boundary.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# ---------------------------------------------------------------------------
# §10.2 environment variables — one lazy, cached reader each.
# Defaults match the spec §10.2 table verbatim.
# ---------------------------------------------------------------------------


@functools.cache
def second_brain_index_path() -> str | None:
    """``SECOND_BRAIN_INDEX_PATH`` — master gate + handshake key (unset → off)."""
    return os.environ.get("SECOND_BRAIN_INDEX_PATH")


@functools.cache
def second_brain_python() -> str:
    """``SECOND_BRAIN_PYTHON`` — venv python for the second_brain CLI."""
    return os.environ.get("SECOND_BRAIN_PYTHON", "/opt/second-brain/bin/python")


@functools.cache
def second_brain_embedder_version() -> str | None:
    """``SECOND_BRAIN_EMBEDDER_VERSION`` — optional handshake assertion."""
    return os.environ.get("SECOND_BRAIN_EMBEDDER_VERSION")


@functools.cache
def second_brain_enabled() -> bool:
    """``SECOND_BRAIN_ENABLED`` — advisory passthrough; default true."""
    return os.environ.get("SECOND_BRAIN_ENABLED", "true").lower() != "false"


@functools.cache
def agentic_os_data() -> str:
    """``AGENTIC_OS_DATA`` — vault/index path derivation root; default ``/data``."""
    return os.environ.get("AGENTIC_OS_DATA", "/data")


@functools.cache
def pipeline_search_api() -> str | None:
    """``PIPELINE_SEARCH_API`` — ``brave`` selects Brave; unset → DuckDuckGo."""
    return os.environ.get("PIPELINE_SEARCH_API")


@functools.cache
def brave_api_key() -> str | None:
    """``BRAVE_API_KEY`` — required when ``PIPELINE_SEARCH_API=brave``."""
    return os.environ.get("BRAVE_API_KEY")


@functools.cache
def concepts_output_dir() -> str | None:
    """``CONCEPTS_OUTPUT_DIR`` — legacy overview output dir (else config/default)."""
    return os.environ.get("CONCEPTS_OUTPUT_DIR")


@functools.cache
def synth_output_dir() -> str | None:
    """``SYNTH_OUTPUT_DIR`` — legacy spec output dir (config/default fallback)."""
    return os.environ.get("SYNTH_OUTPUT_DIR")


# ---------------------------------------------------------------------------
# §10.1 bundled pipeline config — lazy + process-cached (same idiom as above).
# The parsed ``pipeline.toml`` is read on first call only, never at import time,
# so the MCP handshake resolves no config (spec §3.2 handshake-safety).
# ---------------------------------------------------------------------------


def bundled_pipeline_toml_path() -> Path:
    """Absolute path to the package-bundled ``pipeline/pipeline.toml``.

    Resolved relative to this module (``Path(__file__)``), so it is OS-agnostic
    and independent of the process CWD.
    """
    return Path(__file__).resolve().parent / "pipeline" / "pipeline.toml"


@functools.cache
def pipeline_config() -> PipelineConfig:
    """Parse + cache the bundled ``pipeline.toml`` (spec §10.1; lazy).

    The import of :mod:`pipeline_orchestrator.toml_loader` is deferred into the
    function body so importing :mod:`config` (e.g. while constructing the FastMCP
    server) neither parses the TOML nor pulls in the loader/models — only the
    first call does. One-directional: ``config`` → ``toml_loader`` → ``models``.
    """
    from pipeline_orchestrator.toml_loader import load_pipeline_config

    return load_pipeline_config(bundled_pipeline_toml_path())


def reset_config_cache() -> None:
    """Clear every cached reader (test seam).

    Tests use this to assert lazy-read semantics and to re-read a patched env.
    """
    for reader in (
        second_brain_index_path,
        second_brain_python,
        second_brain_embedder_version,
        second_brain_enabled,
        agentic_os_data,
        pipeline_search_api,
        brave_api_key,
        concepts_output_dir,
        synth_output_dir,
        pipeline_config,
    ):
        reader.cache_clear()
