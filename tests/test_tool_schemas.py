"""Port of ``legacy-node/tests/tool-schemas.test.ts`` (10 cases).

Static-integrity parity for the registered tool set. The Node ``toolSchemas``
array (the exported ``tool-schemas.ts`` literal) is replaced by the FastMCP
``tools/list`` output: each test builds a **fresh** ``FastMCP("pipeline")``,
calls :func:`~pipeline_orchestrator.tools.register_tools`, and enumerates the
tools via ``list_tools()``. A fresh instance (NOT ``server.mcp``) avoids
cross-test double-registration on the shared module-level singleton.

Field mapping (TS ``Tool`` → :class:`mcp.types.Tool`):

* ``tool.name`` → ``tool.name``
* ``tool.description`` → ``tool.description``
* ``tool.inputSchema`` (the JSON-Schema object) → ``tool.inputSchema`` (a dict)

Each Node ``it(...)`` maps 1:1 to a ``test_*`` method here, grouped in the class
mirroring the Node ``describe('toolSchemas static integrity', ...)`` block.
"""

from __future__ import annotations

import asyncio

from mcp.server.fastmcp import FastMCP
from mcp.types import Tool

from pipeline_orchestrator.tools import register_tools


def _tool_schemas() -> list[Tool]:
    """Build the FastMCP ``toolSchemas`` equivalent from a fresh instance.

    A fresh ``FastMCP("pipeline")`` per call keeps each test isolated from the
    shared ``server.mcp`` singleton (no duplicate-registration warnings, no
    cross-test bleed).
    """
    mcp = FastMCP("pipeline")
    register_tools(mcp)
    return asyncio.run(mcp.list_tools())


class TestToolSchemasStaticIntegrity:
    """Port of the Node ``describe('toolSchemas static integrity', ...)`` block."""

    def test_has_at_least_25_tool_definitions(self) -> None:
        schemas = _tool_schemas()
        assert len(schemas) >= 25, f"expected >= 25 tools, got {len(schemas)}"

    def test_all_tool_names_are_unique(self) -> None:
        names = [t.name for t in _tool_schemas()]
        unique = set(names)
        dupes = [n for i, n in enumerate(names) if names.index(n) != i]
        assert len(unique) == len(names), f"duplicate tool names: {', '.join(dupes)}"

    def test_all_tool_names_use_the_pipeline_prefix(self) -> None:
        for tool in _tool_schemas():
            assert tool.name.startswith("pipeline_"), (
                f'tool "{tool.name}" does not start with pipeline_'
            )

    def test_no_tool_has_an_empty_description(self) -> None:
        for tool in _tool_schemas():
            assert isinstance(tool.description, str) and len(tool.description) > 0, (
                f'tool "{tool.name}" has empty or missing description'
            )

    def test_every_input_schema_type_is_object(self) -> None:
        for tool in _tool_schemas():
            assert tool.inputSchema["type"] == "object", (
                f'tool "{tool.name}" inputSchema.type is '
                f'"{tool.inputSchema["type"]}", expected "object"'
            )

    def test_every_input_schema_has_a_properties_object(self) -> None:
        for tool in _tool_schemas():
            props = tool.inputSchema["properties"]
            assert isinstance(props, dict), (
                f'tool "{tool.name}" inputSchema.properties is not an object'
            )

    def test_required_arrays_only_reference_properties_that_exist(self) -> None:
        for tool in _tool_schemas():
            required = tool.inputSchema.get("required") or []
            prop_names = list(tool.inputSchema["properties"].keys())
            for req in required:
                assert req in prop_names, (
                    f'tool "{tool.name}" requires "{req}" but it is not in '
                    f"properties [{', '.join(prop_names)}]"
                )

    def test_pipeline_tools_include_the_core_orchestration_set(self) -> None:
        pipeline_names = [
            t.name for t in _tool_schemas() if t.name.startswith("pipeline_")
        ]
        expected = [
            "pipeline_get_config",
            "pipeline_init_run",
            "pipeline_next_phases",
            "pipeline_start_phase",
            "pipeline_complete_phase",
            "pipeline_fail_phase",
            "pipeline_validate_artifact",
            "pipeline_store_artifact",
            "pipeline_load_artifact",
            "pipeline_run_status",
        ]
        for name in expected:
            assert name in pipeline_names, f"missing core pipeline tool: {name}"

    def test_pipeline_cc_tools_include_the_collector_set(self) -> None:
        cc_names = [
            t.name for t in _tool_schemas() if t.name.startswith("pipeline_cc_")
        ]
        expected = [
            "pipeline_cc_load_collection",
            "pipeline_cc_query",
            "pipeline_cc_get_items",
            "pipeline_cc_collect_concepts",
            "pipeline_cc_save_overview",
            "pipeline_cc_list_overviews",
            "pipeline_cc_get_overview",
        ]
        for name in expected:
            assert name in cc_names, f"missing pipeline_cc tool: {name}"

    def test_pipeline_synth_tools_include_the_synthesis_set(self) -> None:
        synth_names = [
            t.name for t in _tool_schemas() if t.name.startswith("pipeline_synth_")
        ]
        expected = [
            "pipeline_synth_load_collection",
            "pipeline_synth_query",
            "pipeline_synth_get_items",
            "pipeline_synth_create_spec",
            "pipeline_synth_save_spec",
            "pipeline_synth_list_specs",
        ]
        for name in expected:
            assert name in synth_names, f"missing pipeline_synth tool: {name}"
