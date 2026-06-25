"""Live MCP-dispatch re-verification (docs/12 S2).

S1 (T6.7) wired the 43 tool bodies and added ``tests/test_mcp_dispatch.py``,
which drives a 3-step lifecycle (``init_run -> next_phases -> start_phase``)
through the REGISTERED tools. What no suite yet did is drive a FULL 5-phase
pipeline run end-to-end through ``tools/call``: the existing
``tests/test_meta_run_regression.py`` drives the same run through the
``handle_*`` handlers via injected DI contexts, never through the registered
``@mcp.tool`` bodies + the module-level ``ServerState`` singleton.

This module closes that gap permanently (docs/12 S2 scope item #2). It drives
``curation -> concept_extraction -> design_synthesis -> validation ->
implementation_scaffold`` through ``mcp.call_tool`` against the live
``server.state`` singleton and asserts the meta-run regression checklist:
exactly one ``phase_completed`` per phase, ``gate_evaluated`` on each gated
phase, ``artifact_stored`` events present, run-scoped artifact paths under the
per-run dir, and a terminal ``status=completed`` + ``completed_at``.

It also re-verifies completion criterion #5 (KB) at the dispatch boundary:
``pipeline_kb_search`` returns the frozen
``{status,results,results_count,vector_results,vector_status}`` shape with
``vector_status='disabled'`` (``SECOND_BRAIN_INDEX_PATH`` unset, no subprocess
spawn) and never throws.

The 5-phase subset is selected via ``init_run`` ``skip_phases`` (the
dispatch-path analogue of the handler test passing an explicit 5-element
``phases`` list): the four non-driven phases (``document_ingestion``,
``research_discovery``, ``codebase_analysis``, ``debate``) are skipped so the
terminal-status guard (all pending phases have incoming edges) is satisfied and
the run reaches ``status=completed``.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any, cast

import pytest
from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, TextContent

from pipeline_orchestrator import kb_client
from pipeline_orchestrator import server as srv
from pipeline_orchestrator.bm25 import BM25Index, bm25_index_dir
from pipeline_orchestrator.collections_store import ResearchItem
from pipeline_orchestrator.kb_client import KBProviderAdapter, register_adapter
from pipeline_orchestrator.second_brain_adapter import (
    create_second_brain_adapter,
)
from pipeline_orchestrator.tools import register_tools

RUN_NAME = "meta-regression"
RUN_TS = "2026-04-12T10-00-00-000Z"

# The four phases not driven in the 5-phase chain; skipped at init so the
# terminal-status guard is satisfied (skipped counts as completed).
_SKIPPED_PHASES = [
    "document_ingestion",
    "research_discovery",
    "codebase_analysis",
    "debate",
]

_DRIVEN_PHASES = [
    "curation",
    "concept_extraction",
    "design_synthesis",
    "validation",
    "implementation_scaffold",
]

_GATED_PHASES = [
    "curation",
    "concept_extraction",
    "design_synthesis",
    "validation",
]


def _fresh_server() -> FastMCP:
    mcp: FastMCP = FastMCP("pipeline")
    register_tools(mcp)
    return mcp


# One in-process server; the only mutable state is the module-level
# ``srv.state`` singleton, reset by the autouse fixture (same idiom as
# ``tests/test_mcp_dispatch.py``).
_MCP = _fresh_server()


def _reset_state() -> None:
    s = srv.state
    s.active_run = None
    s.active_run_dir = ""
    s.active_debate = None
    s._project_root = None
    s._config = None
    s._schemas = None
    s._quality_gates = None
    s._hooks_config = None


@pytest.fixture(autouse=True)
def _isolated_state() -> Any:
    _reset_state()
    kb_client._adapter_registry.clear()
    os.environ.pop("SECOND_BRAIN_INDEX_PATH", None)
    # Register the real vector:second_brain adapter exactly as server.main()
    # does, so the disabled-leg path is exercised live (no env -> no spawn).
    register_adapter(cast("KBProviderAdapter", create_second_brain_adapter()))
    yield
    _reset_state()
    kb_client._adapter_registry.clear()
    os.environ.pop("SECOND_BRAIN_INDEX_PATH", None)


def _call(name: str, args: dict[str, Any]) -> CallToolResult:
    return cast("CallToolResult", asyncio.run(_MCP.call_tool(name, args)))


def _text(result: CallToolResult) -> str:
    assert isinstance(result, CallToolResult)
    assert len(result.content) == 1
    block = result.content[0]
    assert isinstance(block, TextContent)
    return block.text


def _parse_events(run_dir: str) -> list[dict[str, Any]]:
    path = os.path.join(run_dir, "events.jsonl")
    if not os.path.exists(path):
        return []
    events: list[dict[str, Any]] = []
    with open(path, encoding="utf-8") as fh:
        content = fh.read()
    for line in content.split("\n"):
        if line.strip():
            events.append(json.loads(line))
    return events


# Per-phase artifact payloads — byte-for-byte the gate-passing fixtures from
# ``tests/test_meta_run_regression.py``, validated here against the REAL
# bundled schemas (the dispatch path resolves ``state.schemas()``).
_ARTIFACTS: dict[str, dict[str, Any]] = {
    "curation": {
        "file_name": "curated.json",
        "storage_key": "curated_collections",
        "artifact_type": "curated-collection",
        "phase": "curation",
        "artifact": {
            "collection_id": "curated-meta-regression",
            "created_date": "2026-04-12",
            "status": "curated",
            "sources": [
                {
                    "type": "x_search",
                    "query": "pipeline orchestrator",
                    "items_found": 1,
                }
            ],
            "items": [
                {
                    "id": "item-1",
                    "title": "Test item",
                    "content": "Test content",
                }
            ],
        },
    },
    "concept_extraction": {
        "file_name": "overview.json",
        "storage_key": "overviews",
        "artifact_type": "knowledge-overview",
        "phase": "concept_extraction",
        "artifact": {
            "overview_id": "ov-meta-regression",
            "created_date": "2026-04-12",
            "source_collection": "curated-meta-regression",
            "themes": [
                {
                    "name": "Pipeline Architecture",
                    "description": "Core pipeline patterns",
                    "concept_names": ["phase-dag"],
                }
            ],
            "concepts": [
                {
                    "name": "phase-dag",
                    "description": "Directed acyclic graph of phases",
                    "category": "architecture",
                }
            ],
            "knowledge_gaps": [],
        },
    },
    "design_synthesis": {
        "file_name": "spec.json",
        "storage_key": "specs",
        "artifact_type": "design-spec",
        "phase": "design_synthesis",
        "artifact": {
            "spec_id": "spec-meta-regression",
            "title": "Meta-Regression Design Spec",
            "created_date": "2026-04-12",
            "architecture": {
                "components": [
                    {
                        "name": "Pipeline Orchestrator",
                        "description": "Core MCP server orchestration",
                        "responsibilities": [
                            "phase management",
                            "artifact routing",
                        ],
                    }
                ],
            },
            "implementation": {"phases": []},
        },
    },
    "validation": {
        "file_name": "validation-report.json",
        # 'manifests' has no ArtifactSubtype mapping -> legacy storeArtifact
        # path (NOT under the per-run dir). 'specs' is blocked for
        # validation-report (M4.3), so 'manifests' is the contract-correct key.
        "storage_key": "manifests",
        "artifact_type": "validation-report",
        "phase": "validation",
        "artifact": {
            "report_id": "vr-meta-regression",
            "created_date": "2026-04-12",
            "spec_id": "spec-meta-regression",
            "results": [
                {
                    "check": "spec_completeness",
                    "passed": True,
                    "message": "Spec is complete",
                }
            ],
            "overall_status": "pass",
        },
    },
}

# Artifact types that ARE run-scoped (mapped subtype) and so must land under
# the per-run directory. validation-report (storage_key='manifests') is NOT.
_RUN_SCOPED_TYPES = {"curated-collection", "knowledge-overview", "design-spec"}


class TestMetaRunDispatchFivePhase:
    def test_five_phase_run_through_registered_tools(
        self, tmp_path: Path
    ) -> None:
        project_root = str(tmp_path)

        # ── pipeline_init_run (registered tool) — Feature-C per-run layout ──
        r_init = _call(
            "pipeline_init_run",
            {
                "run_id": "meta-reg-dispatch",
                "project_root": project_root,
                "skip_phases": list(_SKIPPED_PHASES),
                "run_parameters": {
                    "run_name": RUN_NAME,
                    "run_directory_timestamp": RUN_TS,
                },
            },
        )
        assert r_init.isError is False, _text(r_init)
        init_body = json.loads(_text(r_init))
        assert init_body["status"] == "initialized"
        assert init_body["total_phases"] == 5
        assert sorted(init_body["skipped"]) == sorted(_SKIPPED_PHASES)

        run = srv.state.active_run
        assert run is not None
        per_run_dir = run.run_data_dir
        assert per_run_dir, "run_data_dir must be set under Feature-C params"
        assert srv.state.active_run_dir == per_run_dir

        # ── pipeline_next_phases (registered tool) — curation available ──
        r_next = _call("pipeline_next_phases", {})
        assert r_next.isError is False, _text(r_next)
        available = json.loads(_text(r_next))["available_phases"]
        avail_names = {p["name"] for p in available}
        assert "curation" in avail_names

        # ── Drive the 5 phases through start -> (store) -> complete ──
        for phase in _DRIVEN_PHASES:
            r_start = _call("pipeline_start_phase", {"phase": phase})
            assert r_start.isError is False, _text(r_start)
            assert json.loads(_text(r_start))["status"] == "in_progress"

            if phase in _ARTIFACTS:
                r_store = _call("pipeline_store_artifact", _ARTIFACTS[phase])
                assert r_store.isError is False, _text(r_store)
                assert json.loads(_text(r_store))["status"] == "ok"

            r_complete = _call("pipeline_complete_phase", {"phase": phase})
            assert r_complete.isError is False, _text(r_complete)

        # ── Terminal state (criterion: status=completed + completed_at) ──
        final = srv.state.active_run
        assert final is not None
        assert final.status == "completed", (
            f"run must end 'completed', got '{final.status}'"
        )
        assert final.completed_at, "run completed_at must be set"

        # The persisted run-state.json must agree (transport-faithful: a client
        # reads artifacts off disk, not the in-process singleton).
        persisted = json.loads(
            Path(srv.state.active_run_dir, "run-state.json").read_text(
                encoding="utf-8"
            )
        )
        assert persisted["status"] == "completed"
        assert persisted["completed_at"] == final.completed_at

        events = _parse_events(per_run_dir)
        assert len(events) > 0, "events.jsonl must contain events"

        # (a) exactly one phase_completed per driven phase
        for phase in _DRIVEN_PHASES:
            count = len(
                [
                    e
                    for e in events
                    if e.get("event") == "phase_completed"
                    and e.get("phase") == phase
                ]
            )
            assert count == 1, (
                f"exactly one phase_completed for '{phase}', got {count}"
            )

        # (b) gate_evaluated present for each gated phase
        for phase in _GATED_PHASES:
            count = len(
                [
                    e
                    for e in events
                    if e.get("event") == "gate_evaluated"
                    and e.get("phase") == phase
                ]
            )
            assert count > 0, f"missing gate_evaluated for gated '{phase}'"

        # (c) artifact_stored events present (one per stored artifact)
        stored = len(
            [e for e in events if e.get("event") == "artifact_stored"]
        )
        assert stored >= 4, f"expected >=4 artifact_stored, got {stored}"

        # (d) run-scoped artifact paths start with the per-run dir
        run_scoped = [
            a
            for a in final.available_artifacts
            if a.type in _RUN_SCOPED_TYPES
        ]
        assert len(run_scoped) > 0, "must have run-scoped artifacts"
        for ref in run_scoped:
            assert ref.path.startswith(per_run_dir), (
                f"artifact '{ref.path}' must be under per-run '{per_run_dir}'"
            )


class TestKbSearchDispatchFrozenShape:
    """Criterion #5 at the dispatch boundary: ``pipeline_kb_search`` returns the
    frozen shape with the vector leg ``disabled`` (env unset, no subprocess)."""

    def test_kb_search_dispatch_frozen_shape_vector_disabled(
        self, tmp_path: Path
    ) -> None:
        project_root = str(tmp_path)
        # No active run -> base_dir = project_root / config storage base dir.
        srv.state.set_project_root(project_root)
        base_dir = os.path.abspath(
            os.path.join(project_root, srv.state.config_storage_base_dir())
        )

        # Build + persist a real BM25 index where the dispatch ctx resolves it.
        items = [
            ResearchItem(
                id="a1",
                title="A1",
                content="alpha alpha alpha beta",
                tags=[],
                metadata={},
            ),
            ResearchItem(
                id="a2",
                title="A2",
                content="alpha gamma",
                tags=[],
                metadata={},
            ),
        ]
        idx = BM25Index()
        idx.build(items, "col_a")
        idx.persist(bm25_index_dir(base_dir, "col_a"))

        assert "SECOND_BRAIN_INDEX_PATH" not in os.environ
        r = _call(
            "pipeline_kb_search",
            {
                "run_id": "rid",
                "phase": "design_synthesis",
                "query": "alpha",
                "top_k": 3,
            },
        )
        assert r.isError is False, _text(r)
        payload = json.loads(_text(r))

        # Frozen top-level key order (R5 §9.4).
        assert list(payload.keys()) == [
            "status",
            "results",
            "results_count",
            "vector_results",
            "vector_status",
        ]
        assert payload["status"] == "ok"
        assert payload["results_count"] >= 1
        # Vector leg disabled — env unset, no subprocess, no merged results.
        assert payload["vector_status"] == "disabled"
        assert payload["vector_results"] == []
