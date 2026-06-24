"""Port of ``legacy-node/tests/cross-ref-validator.test.ts`` (18 cases).

Byte-exact behavioral parity tests for
``pipeline_orchestrator.cross_ref``. Each Node ``it(...)`` maps 1:1 to a
``test_*`` function here, grouped into classes mirroring the Node ``describe``
blocks (validateCrossReferences ×5, validateSemantics ×5,
validateRunCompleteness ×3, validateRun ×3, "Fix 3.7" ×2).

Fixtures mirror the Node ``beforeEach``/``makeRunState`` and the module-level
sample artifacts. Artifacts are written to the legacy flat per-type directories
via :func:`store_artifact` and registered as :class:`ArtifactRef` entries whose
``path`` basename matches the stored file name (the validator matches by
``basename(ref.path)``). The run-state helpers take a ``state_dir`` positional
the TS versions lack; a per-test ``runs/<run-id>`` temp dir is supplied — the
validators never read ``run-state.json`` off disk, only the in-memory state.

All paths are OS-agnostic: directories come from the ``tmp_path`` fixture and
are joined with ``os.path.join`` — never hardcoded POSIX slashes.
"""

from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any

import pytest

from pipeline_orchestrator.cross_ref import (
    validate_cross_references,
    validate_run,
    validate_run_completeness,
    validate_semantics,
)
from pipeline_orchestrator.models import ArtifactRef, RunState
from pipeline_orchestrator.run_state import (
    add_artifact,
    complete_phase,
    init_run,
    start_phase,
)
from pipeline_orchestrator.storage import StorageConfig, store_artifact

# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def temp_dir(tmp_path: Path) -> str:
    """A fresh base directory for storage + run state (mirrors beforeEach mkdtemp)."""
    return str(tmp_path)


@pytest.fixture
def storage_config(temp_dir: str) -> StorageConfig:
    """Storage config rooted at ``temp_dir`` (mirrors the Node ``storageConfig``)."""
    return StorageConfig(
        base_dir=temp_dir,
        paths={
            "runs": "runs",
            "manifests": "manifests",
            "raw_collections": os.path.join("collections", "raw"),
            "curated_collections": os.path.join("collections", "curated"),
            "overviews": "overviews",
            "specs": "specs",
            "debates": "debates",
            "codebase": "codebase",
        },
    )


def make_run_state(temp_dir: str, **overrides: Any) -> RunState:
    """Build a :class:`RunState` with the Node ``makeRunState`` defaults + overrides."""
    defaults: dict[str, Any] = {
        "run_id": "test-run",
        "pipeline_version": "1.0.0",
        "created_at": "2026-04-03T10:00:00Z",
        "updated_at": "2026-04-03T10:00:00Z",
        "status": "running",
        "phases": {},
        "available_artifacts": [],
        "config_path": temp_dir,
    }
    defaults.update(overrides)
    return RunState(**defaults)


# ── Sample artifacts ──────────────────────────────────────────────────

SAMPLE_RAW_COLLECTION: dict[str, Any] = {
    "collection_id": "test--aabbccdd",
    "created_date": "2026-04-03T10:00:00Z",
    "status": "raw",
    "items": [
        {"id": "x_001", "source_type": "x_search", "content": "Some tweet content"},
        {"id": "x_002", "source_type": "x_search", "content": "Another tweet"},
    ],
}

SAMPLE_CURATED_COLLECTION: dict[str, Any] = {
    "collection_id": "test--aabbccdd",
    "created_date": "2026-04-03T10:00:00Z",
    "curated_date": "2026-04-03T11:00:00Z",
    "status": "curated",
    "items": [
        {
            "id": "x_001",
            "content": "Some tweet content",
            "summary": "Tweet about X",
            "tags": ["trading"],
        },
        {
            "id": "x_002",
            "content": "Another tweet",
            "summary": "Tweet about Y",
            "tags": ["market"],
        },
    ],
}

SAMPLE_OVERVIEW: dict[str, Any] = {
    "overview_id": "ov-12345678",
    "title": "Test Overview",
    "created_date": "2026-04-03T12:00:00Z",
    "status": "complete",
    "sources": [
        {"collection": "test--aabbccdd", "item_id": "x_001", "title": "Source tweet"},
    ],
    "summary": "A test summary",
    "concepts": [
        {
            "name": "Test Concept",
            "category": "strategy",
            "description": "A test concept",
            "key_details": ["detail 1"],
            "source_items": ["x_001"],
            "relationships": [],
        },
    ],
    "themes": [
        {
            "name": "Test Theme",
            "description": "A grouping",
            "concept_names": ["Test Concept"],
        },
    ],
}

SAMPLE_SPEC: dict[str, Any] = {
    "spec_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
    "title": "Test Spec",
    "created_date": "2026-04-03",
    "updated_date": "2026-04-03",
    "version": "1.0.0",
    "status": "draft",
    "spec_type": "implementation",
    "sources": [
        {
            "collection": "test--aabbccdd",
            "item_id": "x_001",
            "title": "Source",
            "relevance": "Key input",
        },
    ],
    "overview": {
        "description": "Test spec description",
        "objectives": ["obj1"],
        "constraints": ["con1"],
        "assumptions": ["asm1"],
    },
    "architecture": {
        "components": [{"name": "Core"}],
        "data_flow": "A to B",
        "integration_points": ["API X"],
    },
    "implementation": {
        "phases": [
            {"phase": 1, "name": "Phase 1", "tasks": ["task1"], "deliverables": ["d1"]}
        ],
        "tech_stack": ["TypeScript"],
        "complexity": "medium",
    },
    "risks": [
        {"description": "Risk 1", "severity": "medium", "mitigation": "Mitigate it"}
    ],
    "success_criteria": ["criterion 1"],
}

SAMPLE_DEBATE_TRANSCRIPT: dict[str, Any] = {
    "transcript_id": "dt-aabb1122",
    "input_type": "knowledge-overview",
    "input_id": "ov-12345678",
    "input_version": "1.0.0",
    "created_date": "2026-04-03T14:00:00Z",
    "rounds": [
        {
            "round": 1,
            "type": "divergent",
            "agents": [
                {"role": "advocate", "position": "Support it", "confidence": 0.8},
                {"role": "critic", "position": "Challenge it", "confidence": 0.7},
            ],
        },
    ],
    "synthesis": {
        "changes_accepted": ["Change 1"],
        "changes_rejected": [{"proposed": "Bad idea", "reason": "Too risky"}],
    },
}


def _ref(**kwargs: Any) -> ArtifactRef:
    """Build an :class:`ArtifactRef` from Node literal kwargs (fills ``created_at``)."""
    kwargs.setdefault("created_at", "2026-04-03T10:00:00Z")
    return ArtifactRef(**kwargs)


# ── validateCrossReferences ───────────────────────────────────────────


class TestValidateCrossReferences:
    def test_returns_no_issues_when_all_references_resolve(
        self, temp_dir: str, storage_config: StorageConfig
    ) -> None:
        store_artifact(
            storage_config,
            "curated_collections",
            "test--aabbccdd.json",
            SAMPLE_CURATED_COLLECTION,
        )
        store_artifact(storage_config, "overviews", "ov-12345678.json", SAMPLE_OVERVIEW)
        store_artifact(storage_config, "specs", "test-spec.json", SAMPLE_SPEC)
        store_artifact(
            storage_config, "debates", "dt-aabb1122.json", SAMPLE_DEBATE_TRANSCRIPT
        )

        state = make_run_state(
            temp_dir,
            available_artifacts=[
                _ref(
                    type="curated-collection",
                    path="test--aabbccdd.json",
                    phase="curation",
                    created_at="2026-04-03T10:00:00Z",
                ),
                _ref(
                    type="knowledge-overview",
                    path="ov-12345678.json",
                    phase="concept_extraction",
                    created_at="2026-04-03T12:00:00Z",
                ),
                _ref(
                    type="design-spec",
                    path="test-spec.json",
                    phase="design_synthesis",
                    created_at="2026-04-03T13:00:00Z",
                ),
                _ref(
                    type="debate-transcript",
                    path="dt-aabb1122.json",
                    phase="debate",
                    created_at="2026-04-03T14:00:00Z",
                ),
            ],
        )

        issues = validate_cross_references(state, storage_config)
        assert issues == []

    def test_reports_broken_collection_reference_in_overview_sources(
        self, temp_dir: str, storage_config: StorageConfig
    ) -> None:
        bad_overview = copy.deepcopy(SAMPLE_OVERVIEW)
        bad_overview["sources"] = [
            {
                "collection": "nonexistent--collection",
                "item_id": "x_001",
                "title": "Source",
            },
        ]
        store_artifact(storage_config, "overviews", "ov-12345678.json", bad_overview)

        state = make_run_state(
            temp_dir,
            available_artifacts=[
                _ref(
                    type="knowledge-overview",
                    path="ov-12345678.json",
                    phase="concept_extraction",
                    created_at="2026-04-03T12:00:00Z",
                ),
            ],
        )

        issues = validate_cross_references(state, storage_config)
        assert len(issues) > 0
        assert any(
            i.source_artifact == "knowledge-overview:ov-12345678" for i in issues
        )
        assert any(i.referenced_id == "nonexistent--collection" for i in issues)

    def test_reports_broken_input_id_reference_in_debate(
        self, temp_dir: str, storage_config: StorageConfig
    ) -> None:
        # Debate references overview_id that does not exist in run
        store_artifact(
            storage_config, "debates", "dt-aabb1122.json", SAMPLE_DEBATE_TRANSCRIPT
        )

        state = make_run_state(
            temp_dir,
            available_artifacts=[
                _ref(
                    type="debate-transcript",
                    path="dt-aabb1122.json",
                    phase="debate",
                    created_at="2026-04-03T14:00:00Z",
                ),
                # No knowledge-overview artifact -- input_id 'ov-12345678' dangles
            ],
        )

        issues = validate_cross_references(state, storage_config)
        assert len(issues) > 0
        assert any(
            i.source_artifact == "debate-transcript:dt-aabb1122"
            and i.referenced_id == "ov-12345678"
            for i in issues
        )

    def test_reports_broken_codebase_requirements_id_in_debate(
        self, temp_dir: str, storage_config: StorageConfig
    ) -> None:
        debate_with_cr = copy.deepcopy(SAMPLE_DEBATE_TRANSCRIPT)
        debate_with_cr["codebase_requirements_id"] = "cr-nonexist"
        store_artifact(storage_config, "debates", "dt-aabb1122.json", debate_with_cr)

        state = make_run_state(
            temp_dir,
            available_artifacts=[
                _ref(
                    type="knowledge-overview",
                    path="ov-12345678.json",
                    phase="concept_extraction",
                    created_at="2026-04-03T12:00:00Z",
                ),
                _ref(
                    type="debate-transcript",
                    path="dt-aabb1122.json",
                    phase="debate",
                    created_at="2026-04-03T14:00:00Z",
                ),
            ],
        )

        # Store the overview so input_id resolves, but no codebase-requirements
        store_artifact(storage_config, "overviews", "ov-12345678.json", SAMPLE_OVERVIEW)

        issues = validate_cross_references(state, storage_config)
        assert any(
            i.field == "codebase_requirements_id" and i.referenced_id == "cr-nonexist"
            for i in issues
        )

    def test_reports_broken_collection_reference_in_spec_sources(
        self, temp_dir: str, storage_config: StorageConfig
    ) -> None:
        bad_spec = copy.deepcopy(SAMPLE_SPEC)
        bad_spec["sources"] = [
            {
                "collection": "missing--collection",
                "item_id": "x_999",
                "title": "Ghost",
                "relevance": "n/a",
            },
        ]
        store_artifact(storage_config, "specs", "bad-spec.json", bad_spec)

        state = make_run_state(
            temp_dir,
            available_artifacts=[
                _ref(
                    type="design-spec",
                    path="bad-spec.json",
                    phase="design_synthesis",
                    created_at="2026-04-03T13:00:00Z",
                ),
            ],
        )

        issues = validate_cross_references(state, storage_config)
        assert any(i.referenced_id == "missing--collection" for i in issues)


# ── validateSemantics ─────────────────────────────────────────────────


class TestValidateSemantics:
    def test_returns_no_issues_for_well_formed_artifacts(
        self, temp_dir: str, storage_config: StorageConfig
    ) -> None:
        store_artifact(storage_config, "overviews", "ov-12345678.json", SAMPLE_OVERVIEW)
        store_artifact(storage_config, "specs", "test-spec.json", SAMPLE_SPEC)
        store_artifact(
            storage_config, "debates", "dt-aabb1122.json", SAMPLE_DEBATE_TRANSCRIPT
        )

        state = make_run_state(
            temp_dir,
            available_artifacts=[
                _ref(
                    type="knowledge-overview",
                    path="ov-12345678.json",
                    phase="concept_extraction",
                    created_at="2026-04-03T12:00:00Z",
                ),
                _ref(
                    type="design-spec",
                    path="test-spec.json",
                    phase="design_synthesis",
                    created_at="2026-04-03T13:00:00Z",
                ),
                _ref(
                    type="debate-transcript",
                    path="dt-aabb1122.json",
                    phase="debate",
                    created_at="2026-04-03T14:00:00Z",
                ),
            ],
        )

        issues = validate_semantics(state, storage_config)
        assert issues == []

    def test_flags_debate_with_fewer_than_2_round1_arguments(
        self, temp_dir: str, storage_config: StorageConfig
    ) -> None:
        thin_debate = copy.deepcopy(SAMPLE_DEBATE_TRANSCRIPT)
        thin_debate["transcript_id"] = "dt-thin"
        thin_debate["rounds"] = [
            {
                "round": 1,
                "type": "divergent",
                "agents": [
                    {
                        "role": "advocate",
                        "position": "Only one voice",
                        "confidence": 0.8,
                    },
                ],
            },
        ]
        store_artifact(storage_config, "debates", "dt-thin.json", thin_debate)

        state = make_run_state(
            temp_dir,
            available_artifacts=[
                _ref(
                    type="debate-transcript",
                    path="dt-thin.json",
                    phase="debate",
                    created_at="2026-04-03T14:00:00Z",
                ),
            ],
        )

        issues = validate_semantics(state, storage_config)
        assert any(
            i.artifact == "debate-transcript:dt-thin"
            and i.rule == "debate_min_round1_agents"
            for i in issues
        )

    def test_flags_design_spec_with_no_sources(
        self, temp_dir: str, storage_config: StorageConfig
    ) -> None:
        empty_sources_spec = copy.deepcopy(SAMPLE_SPEC)
        empty_sources_spec["sources"] = []
        store_artifact(
            storage_config, "specs", "empty-sources.json", empty_sources_spec
        )

        state = make_run_state(
            temp_dir,
            available_artifacts=[
                _ref(
                    type="design-spec",
                    path="empty-sources.json",
                    phase="design_synthesis",
                    created_at="2026-04-03T13:00:00Z",
                ),
            ],
        )

        issues = validate_semantics(state, storage_config)
        assert any(i.rule == "spec_has_sources" for i in issues)

    def test_flags_knowledge_overview_with_no_concepts(
        self, temp_dir: str, storage_config: StorageConfig
    ) -> None:
        empty_concepts = copy.deepcopy(SAMPLE_OVERVIEW)
        empty_concepts["concepts"] = []
        store_artifact(
            storage_config, "overviews", "empty-concepts.json", empty_concepts
        )

        state = make_run_state(
            temp_dir,
            available_artifacts=[
                _ref(
                    type="knowledge-overview",
                    path="empty-concepts.json",
                    phase="concept_extraction",
                    created_at="2026-04-03T12:00:00Z",
                ),
            ],
        )

        issues = validate_semantics(state, storage_config)
        assert any(i.rule == "overview_has_concepts" for i in issues)

    def test_flags_overview_theme_referencing_nonexistent_concept_name(
        self, temp_dir: str, storage_config: StorageConfig
    ) -> None:
        bad_themes = copy.deepcopy(SAMPLE_OVERVIEW)
        bad_themes["themes"] = [
            {
                "name": "Orphan Theme",
                "description": "References nothing real",
                "concept_names": ["Nonexistent Concept"],
            },
        ]
        store_artifact(storage_config, "overviews", "bad-themes.json", bad_themes)

        state = make_run_state(
            temp_dir,
            available_artifacts=[
                _ref(
                    type="knowledge-overview",
                    path="bad-themes.json",
                    phase="concept_extraction",
                    created_at="2026-04-03T12:00:00Z",
                ),
            ],
        )

        issues = validate_semantics(state, storage_config)
        assert any(i.rule == "overview_theme_concepts_exist" for i in issues)


# ── validateRunCompleteness ───────────────────────────────────────────


class TestValidateRunCompleteness:
    def test_returns_no_issues_when_completed_phases_have_outputs(
        self, temp_dir: str
    ) -> None:
        run_dir = os.path.join(temp_dir, "runs", "complete-run")
        state = init_run(
            "complete-run", "1.0.0", ["curation", "concept_extraction"], run_dir
        )
        state = start_phase(state, "curation", run_dir)
        state = add_artifact(
            state,
            ArtifactRef(
                type="curated-collection",
                path="test.json",
                phase="curation",
                created_at="2026-04-03T10:00:00Z",
            ),
            run_dir,
        )
        state = complete_phase(state, "curation", run_dir)

        phase_output_map = {
            "curation": ["curated-collection"],
            "concept_extraction": ["knowledge-overview"],
        }

        issues = validate_run_completeness(state, phase_output_map)
        assert issues == []

    def test_flags_completed_phase_missing_expected_outputs(
        self, temp_dir: str
    ) -> None:
        run_dir = os.path.join(temp_dir, "runs", "incomplete-run")
        state = init_run(
            "incomplete-run", "1.0.0", ["curation", "concept_extraction"], run_dir
        )

        # Complete curation without storing any artifact
        state = start_phase(state, "curation", run_dir)
        state = complete_phase(state, "curation", run_dir)

        phase_output_map = {
            "curation": ["curated-collection"],
            "concept_extraction": ["knowledge-overview"],
        }

        issues = validate_run_completeness(state, phase_output_map)
        assert len(issues) > 0
        assert any(
            i.phase == "curation" and i.missing_type == "curated-collection"
            for i in issues
        )

    def test_ignores_skipped_and_pending_phases(self, temp_dir: str) -> None:
        run_dir = os.path.join(temp_dir, "runs", "partial-run")
        state = init_run(
            "partial-run",
            "1.0.0",
            ["curation", "concept_extraction", "design_synthesis"],
            run_dir,
        )

        # Only complete curation with its artifact
        state = start_phase(state, "curation", run_dir)
        state = add_artifact(
            state,
            ArtifactRef(
                type="curated-collection",
                path="test.json",
                phase="curation",
                created_at="2026-04-03T10:00:00Z",
            ),
            run_dir,
        )
        state = complete_phase(state, "curation", run_dir)

        # concept_extraction is still pending, design_synthesis is still pending

        phase_output_map = {
            "curation": ["curated-collection"],
            "concept_extraction": ["knowledge-overview"],
            "design_synthesis": ["design-spec"],
        }

        issues = validate_run_completeness(state, phase_output_map)
        assert issues == []


# ── validateRun (full orchestrator) ──────────────────────────────────


class TestValidateRun:
    def test_returns_clean_report_when_everything_is_consistent(
        self, temp_dir: str, storage_config: StorageConfig
    ) -> None:
        store_artifact(
            storage_config,
            "curated_collections",
            "test--aabbccdd.json",
            SAMPLE_CURATED_COLLECTION,
        )
        store_artifact(storage_config, "overviews", "ov-12345678.json", SAMPLE_OVERVIEW)
        store_artifact(storage_config, "specs", "test-spec.json", SAMPLE_SPEC)
        store_artifact(
            storage_config, "debates", "dt-aabb1122.json", SAMPLE_DEBATE_TRANSCRIPT
        )

        run_dir = os.path.join(temp_dir, "runs", "full-run")
        state = init_run(
            "full-run",
            "1.0.0",
            ["curation", "concept_extraction", "design_synthesis", "debate"],
            run_dir,
        )

        # Register artifacts in run state
        for art_type, art_path, art_phase in [
            ("curated-collection", "test--aabbccdd.json", "curation"),
            ("knowledge-overview", "ov-12345678.json", "concept_extraction"),
            ("design-spec", "test-spec.json", "design_synthesis"),
            ("debate-transcript", "dt-aabb1122.json", "debate"),
        ]:
            state = add_artifact(
                state,
                ArtifactRef(
                    type=art_type,
                    path=art_path,
                    phase=art_phase,
                    created_at="2026-04-03T10:00:00Z",
                ),
                run_dir,
            )

        # Complete all phases
        for phase in ["curation", "concept_extraction", "design_synthesis", "debate"]:
            state = start_phase(state, phase, run_dir)
            state = complete_phase(state, phase, run_dir)

        phase_output_map = {
            "curation": ["curated-collection"],
            "concept_extraction": ["knowledge-overview"],
            "design_synthesis": ["design-spec"],
            "debate": ["debate-transcript"],
        }

        report = validate_run(state, storage_config, phase_output_map)

        assert report.valid is True
        assert len(report.cross_ref_issues) == 0
        assert len(report.semantic_issues) == 0
        assert len(report.completeness_issues) == 0

    def test_returns_invalid_report_when_there_are_cross_ref_issues(
        self, temp_dir: str, storage_config: StorageConfig
    ) -> None:
        # Debate references nonexistent overview
        store_artifact(
            storage_config, "debates", "dt-aabb1122.json", SAMPLE_DEBATE_TRANSCRIPT
        )

        run_dir = os.path.join(temp_dir, "runs", "bad-run")
        state = init_run("bad-run", "1.0.0", ["debate"], run_dir)
        state = add_artifact(
            state,
            ArtifactRef(
                type="debate-transcript",
                path="dt-aabb1122.json",
                phase="debate",
                created_at="2026-04-03T14:00:00Z",
            ),
            run_dir,
        )
        state = start_phase(state, "debate", run_dir)
        state = complete_phase(state, "debate", run_dir)

        report = validate_run(state, storage_config, {"debate": ["debate-transcript"]})

        assert report.valid is False
        assert len(report.cross_ref_issues) > 0

    def test_aggregates_issues_from_all_three_validators(
        self, temp_dir: str, storage_config: StorageConfig
    ) -> None:
        # Thin debate (semantic) + missing overview (cross-ref)
        # + no curation artifact (completeness)
        thin_debate = copy.deepcopy(SAMPLE_DEBATE_TRANSCRIPT)
        thin_debate["rounds"] = [
            {
                "round": 1,
                "type": "divergent",
                "agents": [{"role": "advocate", "position": "Solo", "confidence": 0.5}],
            },
        ]
        store_artifact(storage_config, "debates", "dt-aabb1122.json", thin_debate)

        run_dir = os.path.join(temp_dir, "runs", "multi-issue-run")
        state = init_run("multi-issue-run", "1.0.0", ["curation", "debate"], run_dir)

        state = add_artifact(
            state,
            ArtifactRef(
                type="debate-transcript",
                path="dt-aabb1122.json",
                phase="debate",
                created_at="2026-04-03T14:00:00Z",
            ),
            run_dir,
        )

        # Complete both phases but curation has no artifact
        state = start_phase(state, "curation", run_dir)
        state = complete_phase(state, "curation", run_dir)
        state = start_phase(state, "debate", run_dir)
        state = complete_phase(state, "debate", run_dir)

        report = validate_run(
            state,
            storage_config,
            {
                "curation": ["curated-collection"],
                "debate": ["debate-transcript"],
            },
        )

        assert report.valid is False
        # Should have cross-ref (dangling input_id), semantic (thin debate),
        # and completeness (no curation artifact)
        assert len(report.cross_ref_issues) > 0, "Expected cross-ref issues"
        assert len(report.semantic_issues) > 0, "Expected semantic issues"
        assert len(report.completeness_issues) > 0, "Expected completeness issues"
        assert len(report.summary) > 0, "Expected a summary string"


# ── Fix 3.7 — validation phase completeness with validation-report ────


class TestFix37ValidationPhaseCompleteness:
    def test_passes_completeness_when_validation_report_registered(
        self, temp_dir: str, storage_config: StorageConfig
    ) -> None:
        run_dir = os.path.join(temp_dir, "runs", "val-report-ok-run")
        state = init_run("val-report-ok-run", "1.0.0", ["validation"], run_dir)
        state = start_phase(state, "validation", run_dir)
        state = add_artifact(
            state,
            ArtifactRef(
                type="validation-report",
                path="validation-report-ok.json",
                phase="validation",
                created_at="2026-04-10T10:00:00Z",
            ),
            run_dir,
        )
        state = complete_phase(state, "validation", run_dir)

        report = validate_run(
            state, storage_config, {"validation": ["validation-report"]}
        )

        assert len(report.completeness_issues) == 0
        assert not any(
            i.phase == "validation" for i in report.completeness_issues
        ), "Expected no completeness issue mentioning the validation phase"

    def test_flags_completeness_when_validation_phase_lacks_validation_report(
        self, temp_dir: str, storage_config: StorageConfig
    ) -> None:
        run_dir = os.path.join(temp_dir, "runs", "val-report-missing-run")
        state = init_run("val-report-missing-run", "1.0.0", ["validation"], run_dir)
        state = start_phase(state, "validation", run_dir)
        state = add_artifact(
            state,
            ArtifactRef(
                type="design-spec",
                path="updated-spec.json",
                phase="validation",
                created_at="2026-04-10T10:00:00Z",
            ),
            run_dir,
        )
        state = complete_phase(state, "validation", run_dir)

        report = validate_run(
            state, storage_config, {"validation": ["validation-report"]}
        )

        assert len(report.completeness_issues) == 1
        assert report.completeness_issues[0].phase == "validation"
        assert report.completeness_issues[0].missing_type == "validation-report"
