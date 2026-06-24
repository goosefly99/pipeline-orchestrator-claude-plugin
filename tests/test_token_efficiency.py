"""Port of ``legacy-node/tests/token-efficiency.test.ts`` (19 cases).

Behavioral parity for the two pure builders owned by this task — ``generateHandoff``
(handoff.ts → :mod:`pipeline_orchestrator.handoff`) and ``generatePhaseBrief``
(phase-brief.ts → :mod:`pipeline_orchestrator.phase_brief`). Each Node ``it(...)``
maps 1:1 to a ``test_*`` method here, grouped into classes mirroring the Node
``describe`` blocks.

Scoping (T6.1): the Node test imports from FOUR sources; two are owned by this
task and two by later tasks (the established deferral pattern):

- ``generateHandoff`` (7 cases) → **ACTIVE** (port 1:1).
- ``generatePhaseBrief`` (5 cases) → **ACTIVE** (port 1:1).
- ``enforceResponseSize`` (4 cases) → **SKIP** (lands in T6.3, artifact-handlers.ts).
- ``inline parameter alias`` (1) + ``Phase 5 tool schemas`` (2) → **ACTIVE** (T6.5).
  The full 43-tool wiring landed in T6.5, so these three assert against the
  registered FastMCP tool list (built from a fresh ``FastMCP("pipeline")`` so the
  shared ``server.mcp`` singleton is never double-registered).

The ``enforceResponseSize`` case bodies are ported so they are ready to unskip;
they are marked ``@pytest.mark.skip`` and reference not-yet-ported symbols only
inside the skipped methods (never at import time).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from pipeline_orchestrator.handoff import generate_handoff
from pipeline_orchestrator.models import (
    ArtifactRef,
    EdgeDefinition,
    PhaseDefinition,
    PipelineConfig,
    PipelineMeta,
    QualityCheck,
    QualityGate,
    StorageConfig,
)
from pipeline_orchestrator.phase_brief import generate_phase_brief
from pipeline_orchestrator.run_state import (
    add_artifact,
    complete_phase,
    init_run,
    start_phase,
)


def _now_iso() -> str:
    """ISO timestamp (the Node ``new Date().toISOString()`` analogue)."""
    return datetime.now(UTC).isoformat()


# ── Test config factory (mirrors the Node makeConfig) ────────────────


def make_config() -> PipelineConfig:
    """Mirror of the Node ``makeConfig()`` fixture."""
    return PipelineConfig(
        pipeline=PipelineMeta(
            id="test-pipeline", version="1.0.0", description="Test pipeline"
        ),
        phases={
            "discovery": PhaseDefinition(
                id=0,
                description="Discover and ingest documents",
                inputs=[],
                outputs=["raw-collection"],
                tools=["pipeline_ingest_documents"],
                entry_point=True,
                model_tier="sonnet",
            ),
            "curation": PhaseDefinition(
                id=1,
                description="Curate raw collection",
                inputs=["raw-collection"],
                outputs=["curated-collection"],
                tools=["pipeline_store_artifact"],
                entry_point=False,
                model_tier="haiku",
            ),
            "synthesis": PhaseDefinition(
                id=2,
                description="Synthesize design spec",
                inputs=["curated-collection"],
                outputs=["design-spec"],
                tools=["pipeline_synth_create_spec"],
                entry_point=False,
                model_tier="opus",
            ),
        },
        edges=[
            EdgeDefinition(from_="discovery", to="curation"),
            EdgeDefinition(from_="curation", to="synthesis"),
        ],
        storage=StorageConfig(
            base_dir="test",
            paths={
                "collections_raw": "collections/raw",
                "collections_curated": "collections/curated",
                "specs": "specs",
            },
        ),
    )


def make_gates() -> list[QualityGate]:
    """Mirror of the Node ``makeGates()`` fixture."""
    return [
        QualityGate(
            phase="curation",
            on_failure="block",
            checks=[
                QualityCheck(
                    check_type="field_present",
                    description="Sources must be present",
                    params={"field": "sources"},
                ),
                QualityCheck(
                    check_type="min_items",
                    description="At least 1 item",
                    params={"field": "items", "min": 1},
                ),
            ],
        )
    ]


# ── generateHandoff (7 cases, ACTIVE) ────────────────────────────────


class TestGenerateHandoff:
    """Port of the Node ``describe('generateHandoff', ...)`` block."""

    def test_generates_a_valid_handoff_payload(self, tmp_path: object) -> None:
        temp_dir = str(tmp_path)
        state = init_run(
            "handoff-test", "1.0.0", ["discovery", "curation", "synthesis"], temp_dir
        )
        state = start_phase(state, "discovery", temp_dir)
        state = complete_phase(state, "discovery", temp_dir)
        state = add_artifact(
            state,
            ArtifactRef(
                type="raw-collection",
                path="collections/raw/test.json",
                phase="discovery",
                created_at=_now_iso(),
            ),
            temp_dir,
        )

        config = make_config()
        gates = make_gates()
        handoff = generate_handoff(state, config, gates, ["curation"])

        assert handoff["run_id"] == "handoff-test"
        assert handoff["pipeline"]["id"] == "test-pipeline"
        assert len(handoff["phases"]) == 3
        assert handoff["completed_phases"] == ["discovery"]
        assert handoff["next_available_phases"] == ["curation"]
        assert "curation" in handoff["suggested_instruction"]

    def test_handoff_payload_is_under_5k_tokens(self, tmp_path: object) -> None:
        import json

        temp_dir = str(tmp_path)
        state = init_run(
            "size-test", "1.0.0", ["discovery", "curation", "synthesis"], temp_dir
        )
        state = start_phase(state, "discovery", temp_dir)
        state = complete_phase(state, "discovery", temp_dir)

        config = make_config()
        handoff = generate_handoff(state, config, make_gates(), ["curation"])
        serialized = json.dumps(handoff, indent=2)

        # 5K tokens is approximately 20K characters.
        assert len(serialized) < 20000, (
            f"Handoff payload is {len(serialized)} chars — exceeds 20K char target"
        )

    def test_includes_model_tier_for_phases_that_have_it(
        self, tmp_path: object
    ) -> None:
        temp_dir = str(tmp_path)
        state = init_run(
            "tier-test", "1.0.0", ["discovery", "curation", "synthesis"], temp_dir
        )
        config = make_config()
        handoff = generate_handoff(state, config, [], ["discovery"])

        discovery_phase = next(
            (p for p in handoff["phases"] if p["name"] == "discovery"), None
        )
        assert discovery_phase is not None
        assert discovery_phase["model_tier"] == "sonnet"

    def test_includes_quality_gate_summaries(self, tmp_path: object) -> None:
        temp_dir = str(tmp_path)
        state = init_run("gate-test", "1.0.0", ["discovery", "curation"], temp_dir)
        config = make_config()
        gates = make_gates()
        handoff = generate_handoff(state, config, gates, ["discovery"])

        assert len(handoff["quality_gates"]) == 1
        assert handoff["quality_gates"][0]["phase"] == "curation"
        assert handoff["quality_gates"][0]["on_failure"] == "block"
        assert handoff["quality_gates"][0]["check_count"] == 2

    def test_suggests_continuing_in_progress_phase(self, tmp_path: object) -> None:
        temp_dir = str(tmp_path)
        state = init_run("ip-test", "1.0.0", ["discovery"], temp_dir)
        state = start_phase(state, "discovery", temp_dir)

        config = make_config()
        handoff = generate_handoff(state, config, [], [])

        assert "Continue" in handoff["suggested_instruction"]
        assert "discovery" in handoff["suggested_instruction"]

    def test_suggests_validation_when_all_phases_complete(
        self, tmp_path: object
    ) -> None:
        temp_dir = str(tmp_path)
        state = init_run("done-test", "1.0.0", ["discovery"], temp_dir)
        state = start_phase(state, "discovery", temp_dir)
        state = complete_phase(state, "discovery", temp_dir)

        config = make_config()
        handoff = generate_handoff(state, config, [], [])

        assert "validate" in handoff["suggested_instruction"]

    def test_includes_artifact_paths_per_phase(self, tmp_path: object) -> None:
        temp_dir = str(tmp_path)
        state = init_run("art-test", "1.0.0", ["discovery"], temp_dir)
        state = add_artifact(
            state,
            ArtifactRef(
                type="raw-collection",
                path="collections/raw/r1.json",
                phase="discovery",
                created_at=_now_iso(),
            ),
            temp_dir,
        )
        state = add_artifact(
            state,
            ArtifactRef(
                type="raw-collection",
                path="collections/raw/r2.json",
                phase="discovery",
                created_at=_now_iso(),
            ),
            temp_dir,
        )

        config = make_config()
        handoff = generate_handoff(state, config, [], [])

        discovery_phase = next(
            (p for p in handoff["phases"] if p["name"] == "discovery"), None
        )
        assert discovery_phase is not None
        assert len(discovery_phase["artifact_paths"]) == 2


# ── generatePhaseBrief (5 cases, ACTIVE) ─────────────────────────────


class TestGeneratePhaseBrief:
    """Port of the Node ``describe('generatePhaseBrief', ...)`` block."""

    def test_generates_a_valid_phase_brief(self, tmp_path: object) -> None:
        temp_dir = str(tmp_path)
        state = init_run("brief-test", "1.0.0", ["discovery", "curation"], temp_dir)
        state = start_phase(state, "discovery", temp_dir)
        state = complete_phase(state, "discovery", temp_dir)
        state = add_artifact(
            state,
            ArtifactRef(
                type="raw-collection",
                path="collections/raw/test.json",
                phase="discovery",
                created_at=_now_iso(),
            ),
            temp_dir,
        )

        config = make_config()
        gates = make_gates()
        brief = generate_phase_brief("curation", state, config, gates)

        assert brief["phase_name"] == "curation"
        assert len(brief["description"]) > 0
        assert brief["model_tier"] == "haiku"
        assert len(brief["input_artifacts"]) > 0
        assert brief["output_requirements"]["expected_types"] == [
            "curated-collection"
        ]
        assert brief.get("quality_gate") is not None
        assert brief["quality_gate"]["on_failure"] == "block"
        assert "curation" in brief["instruction"]

    def test_throws_for_unknown_phase(self, tmp_path: object) -> None:
        temp_dir = str(tmp_path)
        state = init_run("err-test", "1.0.0", ["discovery"], temp_dir)
        config = make_config()

        with pytest.raises(ValueError, match="not found"):
            generate_phase_brief("nonexistent", state, config, [])

    def test_includes_all_required_fields(self, tmp_path: object) -> None:
        temp_dir = str(tmp_path)
        state = init_run("fields-test", "1.0.0", ["discovery"], temp_dir)
        config = make_config()
        brief = generate_phase_brief("discovery", state, config, [])

        assert "phase_name" in brief
        assert "description" in brief
        assert "input_artifacts" in brief
        assert "output_requirements" in brief
        assert "storage_paths" in brief
        assert "tools" in brief
        assert "instruction" in brief

    def test_resolves_input_artifacts_from_upstream_phases(
        self, tmp_path: object
    ) -> None:
        temp_dir = str(tmp_path)
        state = init_run(
            "inputs-test", "1.0.0", ["discovery", "curation"], temp_dir
        )
        state = add_artifact(
            state,
            ArtifactRef(
                type="raw-collection",
                path="collections/raw/test.json",
                phase="discovery",
                created_at=_now_iso(),
            ),
            temp_dir,
        )

        config = make_config()
        brief = generate_phase_brief("curation", state, config, [])

        # curation takes raw-collection as input; discovery produces it.
        assert len(brief["input_artifacts"]) > 0
        assert any(a["type"] == "raw-collection" for a in brief["input_artifacts"])

    def test_omits_quality_gate_when_no_gate_is_configured(
        self, tmp_path: object
    ) -> None:
        temp_dir = str(tmp_path)
        state = init_run("no-gate-test", "1.0.0", ["discovery"], temp_dir)
        config = make_config()
        brief = generate_phase_brief("discovery", state, config, [])

        # The TS asserts `brief.quality_gate === undefined`; in Python the key is
        # OMITTED entirely (undefined-drop parity), so `.get(...)` is None.
        assert brief.get("quality_gate") is None
        assert "quality_gate" not in brief


# ── enforceResponseSize (4 cases, SKIP — lands in T6.3) ──────────────


class TestEnforceResponseSize:
    """Port of the Node ``describe('enforceResponseSize', ...)`` block.

    Unskipped in T6.3: ``enforce_response_size`` now lives in
    ``pipeline_orchestrator.tools.artifact_tools``. Each method imports the symbol
    lazily (never at module import time) so the suite stays import-clean.
    """

    def test_returns_response_unchanged_when_under_limit(self) -> None:
        import importlib
        import json

        enforce_response_size = importlib.import_module(
            "pipeline_orchestrator.tools.artifact_tools"
        ).enforce_response_size

        response = json.dumps({"data": "small"})
        result = enforce_response_size(response, 1000)
        assert result == response

    def test_truncates_and_adds_continuation_ref_when_over_limit(self) -> None:
        import importlib
        import json

        enforce_response_size = importlib.import_module(
            "pipeline_orchestrator.tools.artifact_tools"
        ).enforce_response_size

        large_response = json.dumps({"data": "x" * 1000})
        result = enforce_response_size(
            large_response, 100, {"storage_key": "test", "file_name": "test.json"}
        )
        parsed = json.loads(result)

        assert parsed["truncated"] is True
        assert parsed["continuation_ref"]
        assert parsed["continuation_ref"]["storage_key"] == "test"
        assert parsed["continuation_ref"]["file_name"] == "test.json"

    def test_truncates_with_ellipsis_when_no_continuation_ref(self) -> None:
        import importlib
        import json

        enforce_response_size = importlib.import_module(
            "pipeline_orchestrator.tools.artifact_tools"
        ).enforce_response_size

        large_response = json.dumps({"data": "x" * 1000})
        result = enforce_response_size(large_response, 100)

        assert "truncated" in result
        assert len(result) <= 200  # truncated + some overflow for the message

    def test_reports_full_length_in_truncated_response(self) -> None:
        import importlib
        import json

        enforce_response_size = importlib.import_module(
            "pipeline_orchestrator.tools.artifact_tools"
        ).enforce_response_size

        large_response = json.dumps({"data": "x" * 1000})
        result = enforce_response_size(
            large_response, 100, {"storage_key": "k", "file_name": "f"}
        )
        parsed = json.loads(result)

        assert parsed["full_length"] == len(large_response)
        assert parsed["preview_length"] == 100


# ── inline parameter alias (1 case, SKIP — lands in T6.5) ────────────


class TestInlineParameterAlias:
    """Port of the Node ``describe('inline parameter alias', ...)`` block.

    ACTIVE (T6.5): the full 43-tool wiring landed. FastMCP derives ``inputSchema``
    from the registered tool's typed signature; this asserts the ``inline`` /
    ``full`` params exist on the registered ``pipeline_load_artifact`` tool. Built
    from a fresh ``FastMCP("pipeline")`` so the shared ``server.mcp`` singleton is
    never double-registered.
    """

    def test_tool_schema_includes_inline_parameter(self) -> None:
        import asyncio

        from mcp.server.fastmcp import FastMCP

        from pipeline_orchestrator.tools import register_tools

        mcp = FastMCP("pipeline")
        register_tools(mcp)
        tools = asyncio.run(mcp.list_tools())
        load_tool = next(
            (t for t in tools if t.name == "pipeline_load_artifact"), None
        )
        assert load_tool is not None
        props = load_tool.inputSchema["properties"]
        assert "inline" in props
        assert "full" in props


# ── Phase 5 tool schemas (2 cases, SKIP — lands in T6.5) ─────────────


class TestPhase5ToolSchemas:
    """Port of the Node ``describe('Phase 5 tool schemas', ...)`` block.

    ACTIVE (T6.5): tool schemas + annotations landed with the full 43-tool
    wiring. Asserts the ``pipeline_phase_handoff`` / ``pipeline_phase_brief``
    tools register with the expected ``readOnlyHint`` / ``idempotentHint``
    annotations and required args. Built from a fresh ``FastMCP("pipeline")`` so
    the shared ``server.mcp`` singleton is never double-registered.
    """

    def test_pipeline_phase_handoff_is_registered_with_read_only_hint(self) -> None:
        import asyncio

        from mcp.server.fastmcp import FastMCP

        from pipeline_orchestrator.tools import register_tools

        mcp = FastMCP("pipeline")
        register_tools(mcp)
        tools = asyncio.run(mcp.list_tools())
        handoff_tool = next(
            (t for t in tools if t.name == "pipeline_phase_handoff"), None
        )
        assert handoff_tool is not None, (
            "pipeline_phase_handoff tool must be registered"
        )
        assert handoff_tool.annotations is not None
        assert handoff_tool.annotations.readOnlyHint is True
        assert handoff_tool.annotations.idempotentHint is True

    def test_pipeline_phase_brief_is_registered_with_read_only_hint(self) -> None:
        import asyncio

        from mcp.server.fastmcp import FastMCP

        from pipeline_orchestrator.tools import register_tools

        mcp = FastMCP("pipeline")
        register_tools(mcp)
        tools = asyncio.run(mcp.list_tools())
        brief_tool = next(
            (t for t in tools if t.name == "pipeline_phase_brief"), None
        )
        assert brief_tool is not None, "pipeline_phase_brief tool must be registered"
        assert brief_tool.annotations is not None
        assert brief_tool.annotations.readOnlyHint is True
        assert "phase" in (brief_tool.inputSchema.get("required") or [])
