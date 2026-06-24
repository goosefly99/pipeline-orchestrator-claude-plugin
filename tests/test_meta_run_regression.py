"""Port of ``legacy-node/tests/meta-run-regression.test.ts`` (3 cases).

M5.1 Meta-Run Regression Suite. Drives a full 5-phase pipeline run in-process
through the REAL lifecycle + artifact handlers (via context injection, no mocked
MCP tools) and asserts the 6 regression-checklist items, plus the two
``agent_directive`` pre_start-hook cases. Each Node ``it(...)`` maps 1:1 to a
``test_*`` method here, grouped into classes mirroring the two Node ``describe``
blocks:

* ``meta-run regression — 5-phase run checklist (M5.1)`` ×1
* ``meta-run regression — agent_directive with pre_start hook (M5.1 item f)`` ×2

The Node ``makeContexts`` helper (real run-state + storage wired into a
``LifecycleContext`` + ``ArtifactContext`` sharing one ``activeRun``/
``activeRunDir`` closure cell) is reproduced with the real engine functions and
single-element ``list`` cells so the callables rebind — the same idiom used in
``tests/test_lifecycle_handlers.py`` / ``tests/test_artifact_handlers.py``. The
``persistArtifact`` routing closure (``storage_key_to_subtype`` → run-scoped
``persist_artifact`` when mapped + ``run_data_dir`` non-empty, else legacy
``store_artifact``) is ported faithfully.

The ``agent_directive`` hook cases stub ``get_hooks_config`` / ``run_hooks``
exactly as the Node test does: one ``pre_start`` hook returning a dict with an
``agent_directive`` (a plain dict, the Python analogue of the TS
``AgentDirective`` literal — see ``tests/test_pre_start_agent_directive.py``),
vs empty lists for the no-hook case.

Fixtures mirror the Node ``mkdtempSync``/``rmSync`` ``beforeEach``/``afterEach``
via the ``tmp_path`` fixture. All paths are OS-agnostic (``os.path.join``).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from pipeline_orchestrator.dag import (
    get_phase_input_satisfaction,
    resolve_next_phases,
)
from pipeline_orchestrator.models import RunState
from pipeline_orchestrator.quality_gates import run_gate_checks
from pipeline_orchestrator.run_state import (
    add_artifact,
    complete_phase,
    compute_recommended_action,
    compute_run_warnings,
    fail_phase,
    init_run,
    load_run_state,
    recover_run,
    remove_phase_artifacts,
    retry_phase,
    skip_phase,
    start_phase,
)
from pipeline_orchestrator.storage import (
    StorageConfig,
    build_artifact_summary,
    list_artifacts,
    load_artifact,
    persist_artifact,
    storage_key_to_subtype,
    store_artifact,
)
from pipeline_orchestrator.toml_loader import (
    load_pipeline_config,
    load_quality_gates,
)
from pipeline_orchestrator.tools.artifact_tools import (
    ArtifactContext,
    handle_store_artifact,
)
from pipeline_orchestrator.tools.lifecycle_tools import (
    LifecycleContext,
    handle_complete_phase,
    handle_start_phase,
)
from pipeline_orchestrator.validator import ValidationResult

REPO_ROOT = Path(__file__).resolve().parent.parent
PIPELINE_DIR = REPO_ROOT / "src" / "pipeline_orchestrator" / "pipeline"

RUN_NAME = "meta-regression"
RUN_TS = "2026-04-12T10-00-00-000Z"


# ── Helpers ────────────────────────────────────────────────────────────


def _make_storage_config(base_dir: str) -> StorageConfig:
    """Mirror the Node ``makeStorageConfig``."""
    return StorageConfig(
        base_dir=base_dir,
        paths={
            "specs": "specs",
            "overviews": "overviews",
            "debates": "debates",
            "manifests": "manifests",
            "raw_collections": "collections/raw",
            "curated_collections": "collections/curated",
            "scaffold": "scaffold",
        },
    )


class _Contexts:
    """Holder mirroring the Node ``makeContexts`` return tuple.

    ``lc`` / ``ac`` share the ``_active_run`` / ``_active_run_dir`` single-element
    list cells so callables rebind them (the Python analogue of the Node closure
    ``let activeRun`` / ``let activeRunDir``).
    """

    def __init__(self, temp_dir: str) -> None:
        active_run: list[RunState | None] = [None]
        active_run_dir: list[str] = [""]
        self._active_run = active_run
        self._active_run_dir = active_run_dir

        config = load_pipeline_config(str(PIPELINE_DIR / "pipeline.toml"))
        gates = load_quality_gates(str(PIPELINE_DIR / "quality-gates.toml"))

        def _set_active_run(s: RunState) -> None:
            active_run[0] = s

        def _set_active_run_dir(d: str) -> None:
            active_run_dir[0] = d

        self.lc = LifecycleContext(
            get_config=lambda: config,
            set_project_root=lambda _root: None,
            get_storage_config=lambda *_a, **_k: _make_storage_config(temp_dir),
            get_active_run=lambda: active_run[0],
            set_active_run=_set_active_run,
            get_active_run_dir=lambda: active_run_dir[0],
            set_active_run_dir=_set_active_run_dir,
            init_run=lambda r_id, pv, phases, state_dir, rp=None: init_run(
                r_id, pv, phases, state_dir, rp
            ),
            skip_phase=lambda s, p, d: skip_phase(s, p, d),
            add_artifact=lambda s, ref, d: add_artifact(s, ref, d),
            start_phase=lambda s, p, d: start_phase(s, p, d),
            complete_phase=lambda s, p, d: complete_phase(s, p, d),
            fail_phase=lambda s, p, e, d: fail_phase(s, p, e, d),
            retry_phase=lambda s, p, d: retry_phase(s, p, d),
            resolve_next_phases=lambda cfg, completed, art_types, skips: (
                resolve_next_phases(cfg, completed, art_types, skips)
            ),
            get_phase_input_satisfaction=lambda phase, art_types: (
                get_phase_input_satisfaction(phase, art_types)
            ),
            load_run_state=lambda state_dir: load_run_state(state_dir),
            recover_run=lambda run_dir: recover_run(run_dir),
            remove_phase_artifacts=lambda s, p, d: remove_phase_artifacts(
                s, p, d
            ),
            get_quality_gates=lambda: gates,
            run_gate_checks=lambda gate, refs: run_gate_checks(gate, refs),
            get_hooks_config=lambda: [],
            run_hooks=lambda *_a: [],
            run_pre_pipeline_init_hooks=lambda *_a: _empty_pre_init(),
            get_project_root=lambda: temp_dir,
            compute_recommended_action=lambda state, nxt: (
                compute_recommended_action(state, nxt)
            ),
            compute_run_warnings=lambda state, *a, **k: compute_run_warnings(
                state, *a, **k
            ),
        )

        def _require_run() -> RunState:
            if active_run[0] is None:
                raise RuntimeError("No active run")
            return active_run[0]

        def _persist(
            storage_key: str,
            file_name: str,
            artifact: object,
            force: object = None,
        ) -> str:
            run_data_dir = (
                active_run[0].run_data_dir if active_run[0] is not None else None
            )
            subtype = storage_key_to_subtype(storage_key)
            if (
                subtype is not None
                and isinstance(run_data_dir, str)
                and len(run_data_dir) > 0
            ):
                return persist_artifact(
                    run_data_dir,
                    subtype,
                    file_name,
                    artifact,
                    force if isinstance(force, bool) else None,
                )
            # Fall through to legacy path for unmapped keys (e.g. 'manifests')
            return store_artifact(
                _make_storage_config(temp_dir),
                storage_key,
                file_name,
                artifact,
                force if isinstance(force, bool) else None,
            )

        self.ac = ArtifactContext(
            get_schemas=lambda: {},
            validate_artifact=lambda _s, _f, _a: ValidationResult(
                valid=True, errors=[]
            ),
            store_artifact=lambda sc, key, name, artifact, force=None: (
                store_artifact(sc, key, name, artifact, force)
            ),
            load_artifact=lambda sc, key, name: load_artifact(sc, key, name),
            list_artifacts=lambda sc, key: list_artifacts(sc, key),
            build_artifact_summary=lambda artifact, key, name: (
                build_artifact_summary(artifact, key, name)
            ),
            get_storage_config=lambda *_a, **_k: _make_storage_config(temp_dir),
            base_dir_from_run_dir=lambda _d: temp_dir,
            get_project_root=lambda: temp_dir,
            get_config_storage_base_dir=lambda: "artifacts",
            get_active_run=lambda: active_run[0],
            set_active_run=_set_active_run,
            get_active_run_dir=lambda: active_run_dir[0],
            require_run=_require_run,
            add_artifact=lambda s, ref, d: add_artifact(s, ref, d),
            persist_artifact=_persist,
        )

    def get_state(self) -> RunState | None:
        return self._active_run[0]

    def get_run_dir(self) -> str:
        return self._active_run_dir[0]


def _empty_pre_init() -> Any:
    """Build an empty :class:`PreInitHookResult` (parameters/userPrompts empty)."""
    from pipeline_orchestrator.hooks import PreInitHookResult

    return PreInitHookResult(parameters={}, userPrompts=[])


def _parse_events(run_dir: str) -> list[dict[str, Any]]:
    """Parse events.jsonl in ``run_dir`` (Node ``parseEvents``)."""
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


# ── Main regression suite ──────────────────────────────────────────────


class TestMetaRunRegressionFivePhaseChecklist:
    def test_drives_five_phases_with_all_six_checklist_assertions(
        self, tmp_path: Path
    ) -> None:
        temp_dir = str(tmp_path)
        ctxs = _Contexts(temp_dir)
        lc, ac = ctxs.lc, ctxs.ac

        config = load_pipeline_config(str(PIPELINE_DIR / "pipeline.toml"))
        # Only include the 5 phases being driven so pending entry-point phases
        # with no incoming edges (document_ingestion, codebase_analysis) don't
        # block the computeRunTerminalStatus allPendingHaveIncomingEdges guard.
        phase_names = [
            "curation",
            "concept_extraction",
            "design_synthesis",
            "validation",
            "implementation_scaffold",
        ]

        # ── Initialize run with per-run parameters (activates Feature C) ──
        run_dir = os.path.join(temp_dir, "runs", "meta-regression-seed")
        seed_state = init_run(
            "meta-reg-seed",
            config.pipeline.version,
            phase_names,
            run_dir,
            {
                "run_name": RUN_NAME,
                "run_directory_timestamp": RUN_TS,
            },
        )
        assert seed_state.run_data_dir, (
            "run_data_dir must be populated when run_name and "
            "run_directory_timestamp are provided"
        )

        per_run_dir = seed_state.run_data_dir
        assert per_run_dir is not None
        lc.set_active_run(seed_state)
        lc.set_active_run_dir(per_run_dir)

        # ── Phase 1: curation ──────────────────────────────────────────
        handle_start_phase({"phase": "curation"}, lc)

        # Store curated-collection: gate checks sources (field_present) + items
        # (min_items: 1)
        handle_store_artifact(
            {
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
            ac,
        )

        handle_complete_phase({"phase": "curation"}, lc)
        state = ctxs.get_state()
        assert state is not None
        assert state.phases["curation"].status == "completed", (
            "curation must be completed"
        )

        # ── Phase 2: concept_extraction ────────────────────────────────
        handle_start_phase({"phase": "concept_extraction"}, lc)

        # Store knowledge-overview: gate checks concepts (field_present, warn)
        handle_store_artifact(
            {
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
            ac,
        )

        handle_complete_phase({"phase": "concept_extraction"}, lc)
        state = ctxs.get_state()
        assert state is not None
        assert state.phases["concept_extraction"].status == "completed", (
            "concept_extraction must be completed"
        )

        # ── Phase 3: design_synthesis ──────────────────────────────────
        handle_start_phase({"phase": "design_synthesis"}, lc)

        # Store design-spec: gate checks architecture.components (field_present,
        # block)
        handle_store_artifact(
            {
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
            ac,
        )

        handle_complete_phase({"phase": "design_synthesis"}, lc)
        state = ctxs.get_state()
        assert state is not None
        assert state.phases["design_synthesis"].status == "completed", (
            "design_synthesis must be completed"
        )

        # ── Phase 4: validation ─────────────────────────────────────────
        handle_start_phase({"phase": "validation"}, lc)

        # Store validation-report: gate checks results (field_present, block).
        # Use storage_key='manifests' — 'specs' is blocked for validation-report
        # (M4.3).
        handle_store_artifact(
            {
                "file_name": "validation-report.json",
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
            ac,
        )

        handle_complete_phase({"phase": "validation"}, lc)
        state = ctxs.get_state()
        assert state is not None
        assert state.phases["validation"].status == "completed", (
            "validation must be completed"
        )

        # ── Phase 5: implementation_scaffold (terminal node) ───────────
        handle_start_phase({"phase": "implementation_scaffold"}, lc)
        # No gate, no artifacts to store — complete directly.
        handle_complete_phase({"phase": "implementation_scaffold"}, lc)
        state = ctxs.get_state()
        assert state is not None
        assert state.phases["implementation_scaffold"].status == "completed", (
            "implementation_scaffold must be completed"
        )

        final_state = ctxs.get_state()
        assert final_state is not None

        # ── Assertion (e): run-state.status == 'completed' + completed_at ──
        assert final_state.status == "completed", (
            "run status must be 'completed' after terminal phase, got "
            f"'{final_state.status}'"
        )
        assert final_state.completed_at, (
            "run completed_at must be set after terminal phase"
        )

        events = _parse_events(per_run_dir)
        assert len(events) > 0, "events.jsonl must contain events after run"

        # ── Assertion (a): exactly one phase_completed per phase ───────
        driven_phases = [
            "curation",
            "concept_extraction",
            "design_synthesis",
            "validation",
            "implementation_scaffold",
        ]
        for phase in driven_phases:
            count = len(
                [
                    e
                    for e in events
                    if e.get("event") == "phase_completed"
                    and e.get("phase") == phase
                ]
            )
            assert count == 1, (
                f'must have exactly one phase_completed event for "{phase}", '
                f"got {count}"
            )

        # ── Assertion (b): gate_evaluated present for gated phases ──────
        gated_phases = [
            "curation",
            "concept_extraction",
            "design_synthesis",
            "validation",
        ]
        for phase in gated_phases:
            count = len(
                [
                    e
                    for e in events
                    if e.get("event") == "gate_evaluated"
                    and e.get("phase") == phase
                ]
            )
            assert count > 0, (
                "must have at least one gate_evaluated event for gated phase "
                f'"{phase}"'
            )

        # ── Assertion (c): artifact_stored events present ──────────────
        artifact_stored_count = len(
            [e for e in events if e.get("event") == "artifact_stored"]
        )
        assert artifact_stored_count >= 4, (
            "must have at least 4 artifact_stored events (one per artifact), "
            f"got {artifact_stored_count}"
        )

        # ── Assertion (d): run-scoped artifact paths start with per-run dir ─
        # validation-report has no ArtifactSubtype mapping and falls back to the
        # legacy storeArtifact path — this is expected. All other artifact types
        # (curated-collection, knowledge-overview, design-spec) ARE mapped and
        # must land under the per-run directory tree.
        run_scoped_types = {
            "curated-collection",
            "knowledge-overview",
            "design-spec",
        }
        run_scoped_artifacts = [
            a
            for a in final_state.available_artifacts
            if a.type in run_scoped_types
        ]
        assert len(run_scoped_artifacts) > 0, "must have run-scoped artifacts"
        for ref in run_scoped_artifacts:
            assert ref.path.startswith(per_run_dir), (
                f'artifact path "{ref.path}" must start with per-run dir '
                f'"{per_run_dir}"'
            )


# ── Agent directive assertion (checklist item f) ────────────────────────


def _make_directive_lc(
    temp_dir: str,
    config: Any,
    active_run: list[RunState | None],
    active_run_dir: list[str],
    *,
    with_directive: bool,
    mock_directive: dict[str, Any] | None = None,
) -> LifecycleContext:
    """Build a LifecycleContext stubbing get_hooks_config / run_hooks.

    With ``with_directive=True`` it mirrors the Node "one pre_start hook that
    always emits an agent_directive"; otherwise empty hook lists (no directive).
    All other engine seams are wired to the real run-state / dag / gate
    functions, except quality gates are stubbed to ``[]`` (matching the Node
    ``getQualityGates: () => []`` in both directive cases).
    """

    def _set_active_run(s: RunState) -> None:
        active_run[0] = s

    def _set_active_run_dir(d: str) -> None:
        active_run_dir[0] = d

    if with_directive:
        assert mock_directive is not None
        from pipeline_orchestrator.models import HookConfig

        def _get_hooks_config() -> list[Any]:
            return [
                HookConfig(
                    trigger="pre_start",
                    phase_filter="*",
                    command="stub-hook",
                )
            ]

        def _run_hooks(*_a: Any) -> list[dict[str, Any]]:
            return [
                {
                    "command": "stub-hook",
                    "trigger": "pre_start",
                    "phase": "curation",
                    "success": True,
                    "agent_directive": mock_directive,
                }
            ]
    else:

        def _get_hooks_config() -> list[Any]:
            return []

        def _run_hooks(*_a: Any) -> list[dict[str, Any]]:
            return []

    return LifecycleContext(
        get_config=lambda: config,
        set_project_root=lambda _root: None,
        get_storage_config=lambda *_a, **_k: _make_storage_config(temp_dir),
        get_active_run=lambda: active_run[0],
        set_active_run=_set_active_run,
        get_active_run_dir=lambda: active_run_dir[0],
        set_active_run_dir=_set_active_run_dir,
        init_run=lambda r_id, pv, phases, state_dir, rp=None: init_run(
            r_id, pv, phases, state_dir, rp
        ),
        skip_phase=lambda s, p, d: skip_phase(s, p, d),
        add_artifact=lambda s, ref, d: add_artifact(s, ref, d),
        start_phase=lambda s, p, d: start_phase(s, p, d),
        complete_phase=lambda s, p, d: complete_phase(s, p, d),
        fail_phase=lambda s, p, e, d: fail_phase(s, p, e, d),
        retry_phase=lambda s, p, d: retry_phase(s, p, d),
        resolve_next_phases=lambda cfg, completed, art_types, skips: (
            resolve_next_phases(cfg, completed, art_types, skips)
        ),
        get_phase_input_satisfaction=lambda phase, art_types: (
            get_phase_input_satisfaction(phase, art_types)
        ),
        load_run_state=lambda state_dir: load_run_state(state_dir),
        recover_run=lambda run_dir: recover_run(run_dir),
        remove_phase_artifacts=lambda s, p, d: remove_phase_artifacts(s, p, d),
        get_quality_gates=lambda: [],
        run_gate_checks=lambda gate, refs: run_gate_checks(gate, refs),
        get_hooks_config=_get_hooks_config,
        run_hooks=_run_hooks,
        run_pre_pipeline_init_hooks=lambda *_a: _empty_pre_init(),
        get_project_root=lambda: temp_dir,
        compute_recommended_action=lambda state, nxt: (
            compute_recommended_action(state, nxt)
        ),
        compute_run_warnings=lambda state, *a, **k: compute_run_warnings(
            state, *a, **k
        ),
    )


class TestMetaRunRegressionAgentDirective:
    def test_start_phase_response_contains_agent_directive_when_hook_returns_one(
        self, tmp_path: Path
    ) -> None:
        temp_dir = str(tmp_path)
        config = load_pipeline_config(str(PIPELINE_DIR / "pipeline.toml"))
        phase_names = list(config.phases.keys())

        active_run: list[RunState | None] = [None]
        active_run_dir: list[str] = [""]

        # The Python analogue of the TS ``AgentDirective`` object literal.
        mock_directive: dict[str, Any] = {
            "subagent_type": "general-purpose",
            "model": "claude-sonnet-4-6",
            "description": "Execute curation phase",
            "prompt": (
                "Please complete the curation phase. Call "
                "pipeline_complete_phase when done."
            ),
        }

        lc = _make_directive_lc(
            temp_dir,
            config,
            active_run,
            active_run_dir,
            with_directive=True,
            mock_directive=mock_directive,
        )

        # Initialize run with per-run layout
        run_dir = os.path.join(temp_dir, "runs", "meta-regression-hook")
        seed_state = init_run(
            "meta-reg-hook",
            config.pipeline.version,
            phase_names,
            run_dir,
            {
                "run_name": RUN_NAME,
                "run_directory_timestamp": RUN_TS,
            },
        )
        active_run[0] = seed_state
        active_run_dir[0] = (
            seed_state.run_data_dir
            if seed_state.run_data_dir is not None
            else run_dir
        )

        # Start curation — hook fires and directive is forwarded in response
        result = handle_start_phase({"phase": "curation"}, lc)
        body = json.loads(result.json)

        assert body.get("agent_directive"), (
            "pipeline_start_phase response must include agent_directive when "
            "hook provides one"
        )
        directive = body["agent_directive"]
        assert directive["subagent_type"] == "general-purpose"
        assert directive["model"] == "claude-sonnet-4-6"
        assert len(directive["prompt"]) > 0, (
            "agent_directive.prompt must be non-empty"
        )

    def test_start_phase_response_omits_agent_directive_when_no_hook_configured(
        self, tmp_path: Path
    ) -> None:
        temp_dir = str(tmp_path)
        config = load_pipeline_config(str(PIPELINE_DIR / "pipeline.toml"))
        phase_names = list(config.phases.keys())

        active_run: list[RunState | None] = [None]
        active_run_dir: list[str] = [""]

        lc = _make_directive_lc(
            temp_dir,
            config,
            active_run,
            active_run_dir,
            with_directive=False,
        )

        run_dir = os.path.join(temp_dir, "runs", "meta-regression-no-hook")
        seed_state = init_run(
            "meta-reg-no-hook",
            config.pipeline.version,
            phase_names,
            run_dir,
            {
                "run_name": RUN_NAME,
                "run_directory_timestamp": RUN_TS,
            },
        )
        active_run[0] = seed_state
        active_run_dir[0] = (
            seed_state.run_data_dir
            if seed_state.run_data_dir is not None
            else run_dir
        )

        result = handle_start_phase({"phase": "curation"}, lc)
        body = json.loads(result.json)

        assert body.get("agent_directive") is None, (
            "pipeline_start_phase response must not include agent_directive "
            "when no hook is configured"
        )
