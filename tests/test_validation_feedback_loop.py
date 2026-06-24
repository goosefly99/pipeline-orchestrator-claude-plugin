"""Port of ``legacy-node/tests/validation-feedback-loop.test.ts`` (3 cases).

Byte-faithful behavioral parity for the validation feedback-loop DAG routing.
Each Node ``it(...)`` maps 1:1 to a ``test_*`` method here, all under the single
Node ``describe('validation feedback loop — DAG routing')`` block:

* ``research_discovery available / implementation_scaffold not when validation
  produces only research-manifest`` ×1
* ``implementation_scaffold IS available when validation produces a design-spec
  (happy path)`` ×1
* ``artifact registry reflects research-manifest registration after validation
  completes`` ×1

The Node ``makeConfig`` synthetic-config helper is reproduced by constructing a
:class:`PipelineConfig` directly (mirroring the ``make_config`` construction
shape in ``tests/test_lifecycle_handlers.py``). The TS ``EdgeDefinition``
literal ``{ from:'validation', to:'research_discovery' }`` becomes
``EdgeDefinition(from_="validation", to="research_discovery")`` — the field is
``from_`` (``from`` is a Python reserved word).

Fixtures mirror the Node ``mkdtempSync``/``rmSync`` ``beforeEach``/``afterEach``
via the ``tmp_path`` fixture. All paths are OS-agnostic — never hardcoded
slashes.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from pipeline_orchestrator.dag import resolve_next_phases
from pipeline_orchestrator.models import (
    ArtifactRef,
    DebateConfig,
    DebateOutputConfig,
    EdgeDefinition,
    KBDefaults,
    PhaseDefinition,
    PipelineConfig,
    PipelineMeta,
    StorageConfig,
)
from pipeline_orchestrator.run_state import (
    add_artifact,
    complete_phase,
    init_run,
    start_phase,
)


def _now_iso() -> str:
    """Node ``new Date().toISOString()`` analogue (millisecond precision, ``Z``)."""
    now = datetime.now(UTC)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def make_config(
    phases: dict[str, PhaseDefinition],
    edges: list[EdgeDefinition] | None = None,
) -> PipelineConfig:
    """Mirror the Node ``makeConfig`` synthetic-config builder.

    Each caller-supplied :class:`PhaseDefinition` already carries the relevant
    ``entry_point`` / ``inputs`` / ``input_mode`` overrides (the Node helper's
    ``?? []`` / ``?? false`` defaults are the dataclass field defaults here).
    """
    if edges is None:
        edges = []
    return PipelineConfig(
        pipeline=PipelineMeta(id="test", version="1.0.0", description=""),
        phases=phases,
        edges=edges,
        debate=DebateConfig(
            agents={},
            rounds={},
            output=DebateOutputConfig(
                includes_transcript=True,
                includes_refined_artifact=True,
                artifact_version_bump="minor",
            ),
        ),
        knowledge_bases=KBDefaults(
            available_in=[],
            max_queries_per_phase=20,
            query_mode="proactive",
        ),
        schemas={},
        storage=StorageConfig(base_dir="test", paths={}),
    )


class TestValidationFeedbackLoopDagRouting:
    def test_research_discovery_available_impl_scaffold_not_when_only_manifest(
        self, tmp_path: Path
    ) -> None:
        temp_dir = str(tmp_path)
        # Build a minimal config representing the relevant DAG slice.
        # implementation_scaffold has entry_point=False so the
        # required_optional input check runs — can_run = available includes
        # 'design-spec'. research_discovery has entry_point=True so it is gated
        # only by edges (predecessor completion), not by input satisfaction.
        config = make_config(
            {
                "validation": PhaseDefinition(
                    id="validation",
                    entry_point=True,
                    inputs=["design-spec"],
                    input_mode="required_optional",
                    outputs=[
                        "validation-report",
                        "design-spec",
                        "research-manifest",
                    ],
                ),
                "research_discovery": PhaseDefinition(
                    id="research_discovery",
                    entry_point=True,
                    inputs=["research-manifest"],
                ),
                "implementation_scaffold": PhaseDefinition(
                    id="implementation_scaffold",
                    entry_point=False,
                    inputs=["design-spec"],
                    input_mode="required_optional",
                ),
            },
            [
                EdgeDefinition(
                    from_="validation", to="research_discovery"
                ),  # feedback edge
                EdgeDefinition(
                    from_="validation", to="implementation_scaffold"
                ),
            ],
        )

        # Simulate: validation started and completed, emitting only a
        # research-manifest.
        state = init_run(
            "test",
            "1.0.0",
            ["validation", "research_discovery", "implementation_scaffold"],
            temp_dir,
        )
        state = start_phase(state, "validation", temp_dir)

        # Register research-manifest only (NOT design-spec) — knowledge-gaps path
        state = add_artifact(
            state,
            ArtifactRef(
                type="research-manifest",
                path=f"{temp_dir}/research-manifest-001.json",
                phase="validation",
                created_at=_now_iso(),
                version=1,
            ),
            temp_dir,
        )

        state = complete_phase(state, "validation", temp_dir)

        # Resolve next phases given: validation completed, only
        # research-manifest available.
        completed_phases = ["validation"]
        artifact_types = ["research-manifest"]  # no design-spec

        next_phases = resolve_next_phases(config, completed_phases, artifact_types)

        # research_discovery IS available (entry_point=True, predecessor
        # validation completed).
        assert "research_discovery" in next_phases, (
            f"Expected research_discovery in next phases, got: "
            f"[{', '.join(next_phases)}]"
        )

        # implementation_scaffold is NOT available (requires design-spec, none
        # registered).
        assert "implementation_scaffold" not in next_phases, (
            "implementation_scaffold should NOT be in next phases when no "
            f"design-spec is registered, got: [{', '.join(next_phases)}]"
        )

    def test_impl_scaffold_available_when_validation_produces_design_spec(
        self, tmp_path: Path
    ) -> None:
        config = make_config(
            {
                "validation": PhaseDefinition(
                    id="validation",
                    entry_point=True,
                    inputs=["design-spec"],
                    input_mode="required_optional",
                ),
                "implementation_scaffold": PhaseDefinition(
                    id="implementation_scaffold",
                    entry_point=False,
                    inputs=["design-spec"],
                    input_mode="required_optional",
                ),
            },
            [EdgeDefinition(from_="validation", to="implementation_scaffold")],
        )

        # Validation produced a design-spec (happy path — spec validated/updated)
        completed_phases = ["validation"]
        artifact_types = ["design-spec", "validation-report"]

        next_phases = resolve_next_phases(config, completed_phases, artifact_types)

        assert "implementation_scaffold" in next_phases, (
            "Expected implementation_scaffold in next phases when design-spec "
            f"is available, got: [{', '.join(next_phases)}]"
        )

    def test_artifact_registry_reflects_research_manifest_after_validation(
        self, tmp_path: Path
    ) -> None:
        temp_dir = str(tmp_path)
        config = make_config(
            {"validation": PhaseDefinition(id="validation", entry_point=True)},
            [],
        )
        assert config is not None  # config constructed (mirrors Node makeConfig call)

        state = init_run("test", "1.0.0", ["validation"], temp_dir)
        state = start_phase(state, "validation", temp_dir)
        state = add_artifact(
            state,
            ArtifactRef(
                type="research-manifest",
                path=f"{temp_dir}/rm-001.json",
                phase="validation",
                created_at=_now_iso(),
                version=1,
            ),
            temp_dir,
        )
        state = complete_phase(state, "validation", temp_dir)

        registered = next(
            (a for a in state.available_artifacts if a.type == "research-manifest"),
            None,
        )
        assert registered is not None, (
            "research-manifest must be registered in available_artifacts"
        )
        assert registered.phase == "validation"
