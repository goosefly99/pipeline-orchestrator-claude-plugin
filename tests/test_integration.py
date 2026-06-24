"""Port of ``legacy-node/tests/integration.test.ts`` (10 cases, 4 describe groups).

Byte-faithful behavioral parity for the end-to-end pipeline flow. Each Node
``it(...)`` maps 1:1 to a ``test_*`` method here, grouped into classes mirroring
the four Node ``describe`` blocks:

* ``integration: full pipeline run flow`` ×1
* ``integration: codebase analysis against real projects`` ×2
* ``integration: cross-reference validation on a multi-phase run`` ×2
* ``integration: register-artifact flow`` ×5

The Node ``PIPELINE_DIR = resolve(__dirname, '../pipeline')`` (i.e.
``legacy-node/pipeline``) maps to the Python ``PIPELINE_DIR`` /``SCHEMAS_DIR``
under ``src/pipeline_orchestrator/`` (resolved OS-agnostically from
``REPO_ROOT`` via the ``Path(__file__)`` idiom used by ``test_validator.py``).

The Node "pipeline-mcp itself" codebase-analysis target — ``resolve(__dirname,
'..')`` (the original Node tree) — maps to ``LEGACY_NODE`` here: it still carries
the unmodified ``package.json`` named ``pipeline-mcp`` with ``smol-toml`` and
``ajv`` runtime deps. Pointing at the Python repo root would detect
``pyproject.toml`` named ``pipeline-orchestrator`` and break the assertions.

Fixtures mirror the Node ``mkdtempSync``/``rmSync`` ``beforeEach``/``afterEach``
via the ``tmp_path`` fixture. All paths are OS-agnostic (``os.path.join`` over
``tmp_path``) — never hardcoded slashes.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest

from pipeline_orchestrator.codebase_analyzer import analyze_codebase
from pipeline_orchestrator.cross_ref import validate_run
from pipeline_orchestrator.dag import resolve_next_phases
from pipeline_orchestrator.models import ArtifactRef
from pipeline_orchestrator.run_state import (
    add_artifact,
    complete_phase,
    init_run,
    load_run_state,
    start_phase,
)
from pipeline_orchestrator.storage import (
    StorageConfig,
    load_artifact,
    store_artifact,
)
from pipeline_orchestrator.toml_loader import load_pipeline_config
from pipeline_orchestrator.validator import load_schemas, validate_artifact

REPO_ROOT = Path(__file__).resolve().parent.parent
PIPELINE_DIR = REPO_ROOT / "src" / "pipeline_orchestrator" / "pipeline"
SCHEMAS_DIR = REPO_ROOT / "src" / "pipeline_orchestrator" / "schemas"
LEGACY_NODE = REPO_ROOT / "legacy-node"


def _now_iso() -> str:
    """Node ``new Date().toISOString()`` analogue (millisecond precision, ``Z``)."""
    now = datetime.now(UTC)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


# ── integration: full pipeline run flow ───────────────────────────────


class TestFullPipelineRunFlow:
    def test_init_resolve_start_validate_store_complete_next(
        self, tmp_path: Path
    ) -> None:
        temp_dir = str(tmp_path)

        # 1. Load pipeline config and schemas
        config = load_pipeline_config(str(PIPELINE_DIR / "pipeline.toml"))
        schemas = load_schemas(SCHEMAS_DIR)

        assert config.pipeline.id == "research-to-implementation"
        assert len(schemas) == 12

        # 2. Init run
        run_dir = os.path.join(temp_dir, "runs", "test-run")
        phase_names = list(config.phases.keys())
        state = init_run("test-run", config.pipeline.version, phase_names, run_dir)

        assert state.status == "initialized"
        assert len(state.phases) == 9

        # 3. Resolve next phases — entry-point phases are available immediately.
        artifact_types = [a.type for a in state.available_artifacts]
        next_phases = resolve_next_phases(config, [], artifact_types)
        # All entry-point phases should be available (they bypass input satisfaction)
        entry_point_phases = [
            name for name, p in config.phases.items() if p.entry_point
        ]
        for ep in entry_point_phases:
            assert ep in next_phases, (
                f'entry-point phase "{ep}" should be available at init'
            )
        assert "research_discovery" in next_phases

        # 5. Start discovery phase
        state = start_phase(state, "research_discovery", run_dir)
        assert state.phases["research_discovery"].status == "in_progress"

        # 6. Create and validate a raw collection
        raw_collection = {
            "collection_id": "test--12345678",
            "created_date": "2026-04-03T14:30:00Z",
            "status": "raw",
            "source_runs": [
                {
                    "source_type": "x_search",
                    "query": "prediction market",
                    "executed_at": "2026-04-03T14:30:05Z",
                    "items_found": 2,
                    "items_stored": 2,
                },
            ],
            "items": [
                {
                    "id": "x_123",
                    "source_type": "x_search",
                    "content": "Test tweet about trading strategies",
                },
            ],
        }

        validation = validate_artifact(
            schemas, "raw-collection.json", raw_collection
        )
        assert validation.valid is True, (
            f"Validation failed: {', '.join(validation.errors)}"
        )

        # 7. Store the artifact
        sc = StorageConfig(
            base_dir=temp_dir,
            paths={
                "raw_collections": "collections/raw",
                "specs": "specs",
                "overviews": "overviews",
            },
        )
        stored_path = store_artifact(
            sc, "raw_collections", "test--12345678.json", raw_collection
        )
        assert "collections" in stored_path

        # 8. Register artifact in run state
        state = add_artifact(
            state,
            ArtifactRef(
                type="raw-collection",
                path=stored_path,
                phase="research_discovery",
                created_at=_now_iso(),
            ),
            run_dir,
        )

        # 9. Complete discovery
        state = complete_phase(state, "research_discovery", run_dir)
        assert state.phases["research_discovery"].status == "completed"

        # 10. Resolve next — curation should now be available
        completed_phases = ["research_discovery"]
        all_artifacts = ["research-manifest", "raw-collection"]
        next_phases = resolve_next_phases(config, completed_phases, all_artifacts)
        assert "curation" in next_phases, (
            f"Expected curation in {json.dumps(next_phases)}"
        )

        # 11. Load the stored artifact back
        loaded = load_artifact(sc, "raw_collections", "test--12345678.json")
        assert isinstance(loaded, dict)
        assert loaded["collection_id"] == "test--12345678"


# ── integration: codebase analysis against real projects ──────────────


class TestCodebaseAnalysisAgainstRealProjects:
    def test_analyzes_pipeline_mcp_itself_valid_schema_deps_smol_toml_ajv(
        self,
    ) -> None:
        pipeline_mcp_path = str(LEGACY_NODE)
        schemas = load_schemas(SCHEMAS_DIR)

        result = analyze_codebase(pipeline_mcp_path)

        assert str(result["requirements_id"]).startswith("cr-"), (
            f"requirements_id should start with cr-: {result['requirements_id']}"
        )
        package = cast("dict[str, Any]", result["package"])
        assert package["name"] == "pipeline-mcp"
        dependencies = cast("dict[str, Any]", result["dependencies"])
        runtime = cast("list[str]", dependencies["runtime"])
        assert any(d.startswith("smol-toml@") for d in runtime), (
            f"expected smol-toml in: {json.dumps(runtime)}"
        )
        assert any(d.startswith("ajv@") for d in runtime), (
            f"expected ajv in: {json.dumps(runtime)}"
        )

        validation = validate_artifact(
            schemas, "codebase-requirements.json", result
        )
        assert validation.valid is True, (
            "Schema validation failed for pipeline-mcp: "
            f"{'; '.join(validation.errors)}"
        )

    def test_full_pipeline_run_init_analyze_validate_store_load_round_trip(
        self, tmp_path: Path
    ) -> None:
        temp_dir = str(tmp_path)
        pipeline_mcp_path = str(LEGACY_NODE)
        config = load_pipeline_config(str(PIPELINE_DIR / "pipeline.toml"))
        schemas = load_schemas(SCHEMAS_DIR)

        # Init a run
        run_dir = os.path.join(temp_dir, "runs", "codebase-run")
        phase_names = list(config.phases.keys())
        state = init_run(
            "codebase-run", config.pipeline.version, phase_names, run_dir
        )
        assert state.status == "initialized"

        # Analyze the codebase
        result = analyze_codebase(pipeline_mcp_path)
        assert str(result["requirements_id"]).startswith("cr-")

        # Validate directly — no remapping needed
        validation = validate_artifact(
            schemas, "codebase-requirements.json", result
        )
        assert validation.valid is True, (
            f"Round-trip schema validation failed: {'; '.join(validation.errors)}"
        )

        # Store the artifact
        sc = StorageConfig(base_dir=temp_dir, paths={"codebase": "codebase"})
        file_name = f"{result['requirements_id']}.json"
        stored_path = store_artifact(sc, "codebase", file_name, result)
        assert stored_path.endswith(file_name), (
            f"expected stored path to end with {file_name}"
        )

        # Load it back and verify round-trip fidelity
        reloaded = load_artifact(sc, "codebase", file_name)
        assert isinstance(reloaded, dict)
        assert reloaded["requirements_id"] == result["requirements_id"]
        assert reloaded["codebase_path"] == pipeline_mcp_path
        pkg = reloaded["package"]
        assert isinstance(pkg, dict)
        assert pkg["name"] == "pipeline-mcp"
        # No tsconfig.json at root — analyzer detects 'javascript' or 'typescript'.
        assert pkg["language"] in ("javascript", "typescript"), (
            f"expected js/ts language, got: {pkg['language']}"
        )


# ── integration: cross-reference validation on a multi-phase run ──────


class TestCrossReferenceValidationMultiPhase:
    def test_validates_run_with_curated_overview_spec_and_debate(
        self, tmp_path: Path
    ) -> None:
        temp_dir = str(tmp_path)
        config = load_pipeline_config(str(PIPELINE_DIR / "pipeline.toml"))
        schemas = load_schemas(SCHEMAS_DIR)

        # Setup storage config rooted at tempDir
        assert config.storage is not None
        sc = StorageConfig(base_dir=temp_dir, paths=config.storage.paths)

        # 1. Store a curated collection
        curated_collection = {
            "collection_id": "integ--11223344",
            "created_date": "2026-04-03T10:00:00Z",
            "curated_date": "2026-04-03T11:00:00Z",
            "status": "curated",
            "items": [
                {
                    "id": "item_001",
                    "content": "Research on prediction markets",
                    "summary": "PM research",
                    "tags": ["trading"],
                },
                {
                    "id": "item_002",
                    "content": "Kalshi API documentation",
                    "summary": "API docs",
                    "tags": ["api"],
                },
            ],
        }
        store_artifact(
            sc, "curated_collections", "integ--11223344.json", curated_collection
        )

        # 2. Store a knowledge overview referencing the collection
        overview = {
            "overview_id": "ov-integ001",
            "title": "Prediction Market Overview",
            "created_date": "2026-04-03T12:00:00Z",
            "status": "complete",
            "sources": [
                {
                    "collection": "integ--11223344",
                    "item_id": "item_001",
                    "title": "PM research",
                },
                {
                    "collection": "integ--11223344",
                    "item_id": "item_002",
                    "title": "API docs",
                },
            ],
            "summary": "Overview of prediction market landscape",
            "concepts": [
                {
                    "name": "Market Making",
                    "category": "strategy",
                    "description": "Providing liquidity by quoting bid/ask",
                    "key_details": ["Spread capture", "Inventory risk"],
                    "source_items": ["item_001"],
                    "relationships": ["related to API Integration"],
                },
                {
                    "name": "API Integration",
                    "category": "architecture",
                    "description": "REST API patterns for Kalshi",
                    "key_details": ["WebSocket feeds", "Order placement"],
                    "source_items": ["item_002"],
                    "relationships": ["enables Market Making"],
                },
            ],
            "themes": [
                {
                    "name": "Trading Infrastructure",
                    "description": "Core trading components",
                    "concept_names": ["Market Making", "API Integration"],
                },
            ],
        }
        store_artifact(sc, "overviews", "ov-integ001.json", overview)

        # Validate overview against schema
        ov_result = validate_artifact(schemas, "knowledge-overview.json", overview)
        assert ov_result.valid is True, (
            f"Overview validation failed: {', '.join(ov_result.errors)}"
        )

        # 3. Store a design spec referencing the same collection
        spec = {
            "spec_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            "title": "Prediction Market Trading Bot",
            "created_date": "2026-04-03",
            "updated_date": "2026-04-03",
            "version": "1.0.0",
            "status": "draft",
            "spec_type": "implementation",
            "sources": [
                {
                    "collection": "integ--11223344",
                    "item_id": "item_001",
                    "title": "PM research",
                    "relevance": "Core strategy",
                },
            ],
            "overview": {
                "description": "An automated trading bot for prediction markets",
                "objectives": ["Automate market making on Kalshi"],
                "constraints": ["US residency required"],
                "assumptions": ["Kalshi API remains stable"],
            },
            "architecture": {
                "components": [
                    {"name": "OrderManager", "description": "Manages order lifecycle"}
                ],
                "data_flow": "Market data -> Signal -> Order",
                "integration_points": ["Kalshi REST API", "Kalshi WebSocket"],
            },
            "implementation": {
                "phases": [
                    {
                        "phase": 1,
                        "name": "Core",
                        "tasks": ["API client"],
                        "deliverables": ["kalshi-client module"],
                    }
                ],
                "tech_stack": ["Python", "asyncio"],
                "complexity": "high",
            },
            "risks": [
                {
                    "description": "API rate limits",
                    "severity": "medium",
                    "mitigation": "Backoff with jitter",
                }
            ],
            "success_criteria": ["Profitable over 30-day window"],
        }
        store_artifact(sc, "specs", "integ-spec.json", spec)

        spec_result = validate_artifact(schemas, "design-spec.json", spec)
        assert spec_result.valid is True, (
            f"Spec validation failed: {', '.join(spec_result.errors)}"
        )

        # 4. Store a debate transcript referencing the overview
        debate = {
            "transcript_id": "dt-integ001",
            "input_type": "knowledge-overview",
            "input_id": "ov-integ001",
            "input_version": "1.0.0",
            "created_date": "2026-04-03T14:00:00Z",
            "rounds": [
                {
                    "round": 1,
                    "type": "divergent",
                    "agents": [
                        {
                            "role": "advocate",
                            "position": "Market making is viable",
                            "confidence": 0.85,
                        },
                        {
                            "role": "critic",
                            "position": "Liquidity risk is underestimated",
                            "confidence": 0.7,
                        },
                        {
                            "role": "risk_specialist",
                            "position": "Need tighter stop-losses",
                            "confidence": 0.75,
                        },
                        {
                            "role": "domain_specialist",
                            "position": "Kalshi market structure supports this",
                            "confidence": 0.8,
                        },
                    ],
                },
            ],
            "synthesis": {
                "changes_accepted": [
                    "Add inventory limit parameter",
                    "Specify stop-loss thresholds",
                ],
                "changes_rejected": [
                    {
                        "proposed": "Remove market making entirely",
                        "reason": "Core strategy, well-supported by evidence",
                    }
                ],
            },
        }
        store_artifact(sc, "debates", "dt-integ001.json", debate)

        debate_result = validate_artifact(schemas, "debate-transcript.json", debate)
        assert debate_result.valid is True, (
            f"Debate validation failed: {', '.join(debate_result.errors)}"
        )

        # 5. Build run state with all artifacts
        run_dir = os.path.join(temp_dir, "runs", "integ-run")
        state = init_run(
            "integ-run",
            config.pipeline.version,
            ["curation", "concept_extraction", "design_synthesis", "debate"],
            run_dir,
        )

        artifacts: list[dict[str, str]] = [
            {
                "type": "curated-collection",
                "path": "integ--11223344.json",
                "phase": "curation",
            },
            {
                "type": "knowledge-overview",
                "path": "ov-integ001.json",
                "phase": "concept_extraction",
            },
            {
                "type": "design-spec",
                "path": "integ-spec.json",
                "phase": "design_synthesis",
            },
            {
                "type": "debate-transcript",
                "path": "dt-integ001.json",
                "phase": "debate",
            },
        ]
        for art in artifacts:
            state = add_artifact(
                state,
                ArtifactRef(
                    type=art["type"],
                    path=art["path"],
                    phase=art["phase"],
                    created_at="2026-04-03T10:00:00Z",
                ),
                run_dir,
            )
        for phase in [
            "curation",
            "concept_extraction",
            "design_synthesis",
            "debate",
        ]:
            state = start_phase(state, phase, run_dir)
            state = complete_phase(state, phase, run_dir)

        # 6. Run full validation
        phase_output_map = {
            "curation": ["curated-collection"],
            "concept_extraction": ["knowledge-overview"],
            "design_synthesis": ["design-spec"],
            "debate": ["debate-transcript"],
        }

        report = validate_run(state, sc, phase_output_map)

        assert report.valid is True, (
            f"Expected clean report but got: {report.summary}"
        )
        assert len(report.cross_ref_issues) == 0
        assert len(report.semantic_issues) == 0
        assert len(report.completeness_issues) == 0
        assert report.artifacts_checked == 4

    def test_detects_issues_when_artifacts_have_broken_cross_references(
        self, tmp_path: Path
    ) -> None:
        temp_dir = str(tmp_path)
        config = load_pipeline_config(str(PIPELINE_DIR / "pipeline.toml"))

        assert config.storage is not None
        sc = StorageConfig(base_dir=temp_dir, paths=config.storage.paths)

        # Store a debate that references a nonexistent overview
        broken_debate = {
            "transcript_id": "dt-broken01",
            "input_type": "knowledge-overview",
            "input_id": "ov-doesnotexist",
            "input_version": "1.0.0",
            "created_date": "2026-04-03T14:00:00Z",
            "rounds": [
                {
                    "round": 1,
                    "type": "divergent",
                    "agents": [
                        {"role": "advocate", "position": "Yes", "confidence": 0.9},
                        {"role": "critic", "position": "No", "confidence": 0.8},
                    ],
                },
            ],
            "synthesis": {"changes_accepted": [], "changes_rejected": []},
        }
        store_artifact(sc, "debates", "dt-broken01.json", broken_debate)

        # Store an overview that references a nonexistent collection
        broken_overview = {
            "overview_id": "ov-broken01",
            "title": "Broken Overview",
            "created_date": "2026-04-03T12:00:00Z",
            "status": "complete",
            "sources": [
                {
                    "collection": "ghost--collection",
                    "item_id": "ghost_1",
                    "title": "Ghost source",
                }
            ],
            "summary": "This overview has broken refs",
            "concepts": [],
            "themes": [],
        }
        store_artifact(sc, "overviews", "ov-broken01.json", broken_overview)

        run_dir = os.path.join(temp_dir, "runs", "broken-run")
        state = init_run(
            "broken-run",
            config.pipeline.version,
            ["concept_extraction", "debate"],
            run_dir,
        )

        state = add_artifact(
            state,
            ArtifactRef(
                type="knowledge-overview",
                path="ov-broken01.json",
                phase="concept_extraction",
                created_at="2026-04-03T12:00:00Z",
            ),
            run_dir,
        )
        state = add_artifact(
            state,
            ArtifactRef(
                type="debate-transcript",
                path="dt-broken01.json",
                phase="debate",
                created_at="2026-04-03T14:00:00Z",
            ),
            run_dir,
        )

        for phase in ["concept_extraction", "debate"]:
            state = start_phase(state, phase, run_dir)
            state = complete_phase(state, phase, run_dir)

        report = validate_run(
            state,
            sc,
            {
                "concept_extraction": ["knowledge-overview"],
                "debate": ["debate-transcript"],
            },
        )

        assert report.valid is False
        # Cross-ref: debate input_id -> ov-doesnotexist,
        # overview collection -> ghost--collection
        assert len(report.cross_ref_issues) >= 2, (
            f"Expected at least 2 cross-ref issues, got "
            f"{len(report.cross_ref_issues)}"
        )
        # Semantic: overview has no concepts
        assert len(report.semantic_issues) >= 1, (
            f"Expected at least 1 semantic issue, got "
            f"{len(report.semantic_issues)}"
        )


# ── integration: register-artifact flow ───────────────────────────────


class TestRegisterArtifactFlow:
    def test_registers_existing_file_in_run_state_and_records_ref(
        self, tmp_path: Path
    ) -> None:
        temp_dir = str(tmp_path)
        config = load_pipeline_config(str(PIPELINE_DIR / "pipeline.toml"))
        run_dir = os.path.join(temp_dir, "runs", "register-test")
        state = init_run(
            "register-test",
            config.pipeline.version,
            list(config.phases.keys()),
            run_dir,
        )

        # Create a real file on disk
        artifact_dir = os.path.join(temp_dir, "codebase")
        os.makedirs(artifact_dir, exist_ok=True)
        artifact_path = os.path.join(artifact_dir, "cr-abcd1234.json")
        with open(artifact_path, "w", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {"requirements_id": "cr-abcd1234", "codebase_path": "/test"}
                )
            )
        assert os.path.exists(artifact_path)

        # Register it
        state = add_artifact(
            state,
            ArtifactRef(
                type="codebase-requirements",
                path=artifact_path,
                phase="codebase_analysis",
                created_at=_now_iso(),
            ),
            run_dir,
        )

        assert len(state.available_artifacts) == 1
        assert state.available_artifacts[0].type == "codebase-requirements"
        assert state.available_artifacts[0].path == artifact_path
        assert state.available_artifacts[0].phase == "codebase_analysis"
        assert (
            artifact_path in state.phases["codebase_analysis"].output_artifacts
        )

    def test_nonexistent_file_path_would_cause_handler_to_reject(
        self, tmp_path: Path
    ) -> None:
        temp_dir = str(tmp_path)
        bogus_path = os.path.join(temp_dir, "no-such-artifact.json")
        assert os.path.exists(bogus_path) is False, (
            "precondition: file should not exist"
        )

        # The handler does: if not exists(resolved): raise Error(...)
        # We test the same guard condition.
        def _guard() -> None:
            if not os.path.exists(bogus_path):
                raise ValueError(f"File not found: {bogus_path}")

        with pytest.raises(ValueError, match="File not found"):
            _guard()

    def test_validation_with_valid_artifact_passes_schema_check(
        self, tmp_path: Path
    ) -> None:
        temp_dir = str(tmp_path)
        schemas = load_schemas(SCHEMAS_DIR)

        # Create a minimal valid raw-collection artifact
        artifact: dict[str, Any] = {
            "collection_id": "register-valid-test--00000001",
            "created_date": _now_iso(),
            "status": "raw",
            "source_runs": [],
            "items": [],
        }
        artifact_path = os.path.join(temp_dir, "register-valid.json")
        with open(artifact_path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(artifact))

        # Same validation the handler does when validate=true
        result = validate_artifact(schemas, "raw-collection.json", artifact)
        assert result.valid is True, (
            f"Expected valid artifact: {'; '.join(result.errors)}"
        )

    def test_validation_with_invalid_artifact_reports_schema_errors(
        self, tmp_path: Path
    ) -> None:
        schemas = load_schemas(SCHEMAS_DIR)

        # Missing required 'collection_id' field
        bad_artifact = {"status": "raw", "items": []}
        result = validate_artifact(schemas, "raw-collection.json", bad_artifact)
        assert result.valid is False
        assert len(result.errors) > 0
        assert any(
            "collection_id" in e or "required" in e for e in result.errors
        )

    def test_registered_artifacts_appear_in_run_state_and_persist_to_disk(
        self, tmp_path: Path
    ) -> None:
        temp_dir = str(tmp_path)
        config = load_pipeline_config(str(PIPELINE_DIR / "pipeline.toml"))
        run_dir = os.path.join(temp_dir, "runs", "persist-test")
        state = init_run(
            "persist-test",
            config.pipeline.version,
            list(config.phases.keys()),
            run_dir,
        )

        artifact_path = os.path.join(temp_dir, "test-spec.json")
        with open(artifact_path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"spec_id": "spec-test"}))

        state = add_artifact(
            state,
            ArtifactRef(
                type="design-spec",
                path=artifact_path,
                phase="design_synthesis",
                created_at=_now_iso(),
            ),
            run_dir,
        )

        # Reload from disk to verify persistence
        reloaded = load_run_state(run_dir)
        assert reloaded is not None
        assert len(reloaded.available_artifacts) == 1
        assert reloaded.available_artifacts[0].type == "design-spec"
        assert reloaded.available_artifacts[0].path == artifact_path
