"""Pure-Python FastMCP startup parity (roadmap T0.1, reinterpreted budget).

This file REPLACES the behavioral intent of the old Node-launcher
``test_start_py.py`` (which tested ``start.py``'s node_modules sentinels / zod
shim / execv dry-run). The Node tree is relocated in T0.2; those assertions go
with it. Here the 4-case budget is reinterpreted onto the NEW pure-Python
FastMCP server:

1. the server module imports and the FastMCP app constructs/boots without error;
2. ``register_tools`` wires the full 43 tools onto the FastMCP instance;
3. ``main`` is importable/callable (import-level; we do not block on stdio);
4. config is lazy — importing the package + constructing the server reads NO env
   and resolves NO ``pipeline.toml`` (handshake-safe, spec §3.2).

It deliberately does not import ``start`` and does not collide with
``test_start_py.py``.
"""

from __future__ import annotations

import asyncio
import importlib
from collections.abc import Callable

import pytest


def test_server_module_imports_and_app_constructs() -> None:
    """Case 1: importing server.py constructs a bootable FastMCP app."""
    from mcp.server.fastmcp import FastMCP

    from pipeline_orchestrator import server

    assert isinstance(server.mcp, FastMCP)
    # The frozen identity name (spec §2) and the frozen workflow blurb (R1 §0).
    assert server.mcp.name == "pipeline"
    assert "pipeline_init_run → pipeline_next_phases → pipeline_start_phase" in (
        server.INSTRUCTIONS
    )
    assert server.mcp.instructions == server.INSTRUCTIONS
    # configure_logging is idempotent and must not raise.
    server.configure_logging()
    server.configure_logging()


def test_register_tools_wires_full_43_tools() -> None:
    """Case 2: register_tools wires the full 43 tools (the M6 wiring seam, T6.5)."""
    from mcp.server.fastmcp import FastMCP

    from pipeline_orchestrator import server

    fresh = FastMCP("pipeline")
    assert len(asyncio.run(fresh.list_tools())) == 0
    server.register_tools(fresh)
    assert len(asyncio.run(fresh.list_tools())) == 43

    # And the package's own module-level instance exposes 43 tools after wiring.
    server.register_tools(server.mcp)
    assert len(asyncio.run(server.mcp.list_tools())) == 43


def test_main_is_importable_callable() -> None:
    """Case 3: main is importable and callable (we do not run the stdio loop)."""
    import importlib as _importlib

    from pipeline_orchestrator import server

    assert isinstance(server.main, Callable)  # type: ignore[arg-type]
    # The __main__ entrypoint imports the same callable object from server.
    entrypoint = _importlib.import_module("pipeline_orchestrator.__main__")
    assert getattr(entrypoint, "main") is server.main  # noqa: B009


def test_config_is_lazy_no_env_read_at_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Case 4: importing the package + constructing the server reads NO env.

    Each §10.2 reader is a zero-arg ``functools.cache`` function: its body (the
    only place that touches ``os.environ``) runs on the FIRST call and never
    before. We clear all caches, re-import the package, and construct + wire the
    server; no reader's cache may have recorded a miss (i.e. none was invoked).
    Then the first explicit accessor call reads env exactly once and caches it.
    """
    import pipeline_orchestrator.config as config

    importlib.reload(config)
    config.reset_config_cache()

    readers = (
        config.second_brain_index_path,
        config.second_brain_python,
        config.second_brain_embedder_version,
        config.second_brain_enabled,
        config.agentic_os_data,
        config.pipeline_search_api,
        config.brave_api_key,
        config.concepts_output_dir,
        config.synth_output_dir,
    )
    # No reader has been invoked yet (cleared above).
    assert all(r.cache_info().misses == 0 for r in readers)

    # Re-import the package + construct/wire the server. None of this may read env
    # (no reader body runs → every cache stays at zero misses / empty).
    server = importlib.reload(importlib.import_module("pipeline_orchestrator.server"))
    server.register_tools(server.mcp)
    for r in readers:
        info = r.cache_info()
        assert info.misses == 0 and info.currsize == 0, (
            f"{r.__name__} read env at import/handshake time: {info}"
        )

    # Lazy-on-first-call: the FIRST explicit accessor call runs the body once
    # (one cache miss), and the result is then cached (a second call is a hit).
    monkeypatch.setenv("SECOND_BRAIN_INDEX_PATH", "/data/vault/index")
    assert config.second_brain_index_path() == "/data/vault/index"
    assert config.second_brain_index_path.cache_info().misses == 1
    assert config.second_brain_index_path() == "/data/vault/index"
    info = config.second_brain_index_path.cache_info()
    assert info.misses == 1 and info.hits == 1
