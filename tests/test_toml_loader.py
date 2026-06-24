"""Port of ``legacy-node/tests/toml-loader.test.ts`` (9 cases).

Byte-exact behavioral parity tests for the TOML config loaders
(:mod:`pipeline_orchestrator.toml_loader`). Each Node ``it(...)`` in the
``describe('loadPipelineConfig')`` block maps 1:1 to a ``test_*`` method on
:class:`TestLoadPipelineConfig`.

The Node test resolves ``PIPELINE_TOML`` as ``../pipeline/pipeline.toml``
relative to the test file (the ``legacy-node`` layout). The Python package
ships a **byte-identical** copy under ``src/pipeline_orchestrator/pipeline/``
(pinned by ``tests/test_schema_assets.py``); we resolve it via
:func:`pipeline_orchestrator.config.bundled_pipeline_toml_path`, which is
OS-agnostic (``Path(__file__)``-relative, no hardcoded separators).

The 9 ported cases exercise ``load_pipeline_config`` only — the Node test file
covers exactly that loader. :class:`TestLoadOtherLoaders` adds parity coverage
for ``load_quality_gates`` / ``load_hooks_config`` (file-absent → ``[]``, the
trigger filter, the bundled-asset shapes); these are not among the 9 ported
cases but pin the remaining two loaders the brief asks the gate to cover.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from pipeline_orchestrator.config import bundled_pipeline_toml_path
from pipeline_orchestrator.toml_loader import (
    load_hooks_config,
    load_pipeline_config,
    load_quality_gates,
)

PIPELINE_TOML = bundled_pipeline_toml_path()
PIPELINE_DIR = PIPELINE_TOML.parent
QUALITY_GATES_TOML = PIPELINE_DIR / "quality-gates.toml"
HOOKS_CONFIG_TOML = PIPELINE_DIR / "hooks-config.toml"


class TestLoadPipelineConfig:
    """Ports ``describe('loadPipelineConfig')`` — 9 cases, 1:1 with the Node tests."""

    def test_loads_and_parses_pipeline_toml(self) -> None:
        config = load_pipeline_config(PIPELINE_TOML)

        assert config.pipeline.id == "research-to-implementation"
        assert config.pipeline.version == "1.0.0"

    def test_parses_all_9_phases(self) -> None:
        config = load_pipeline_config(PIPELINE_TOML)
        phase_names = list(config.phases.keys())

        assert len(phase_names) == 9
        assert "research_discovery" in phase_names
        assert "debate" in phase_names
        assert "implementation_scaffold" in phase_names

    def test_parses_phase_properties_correctly(self) -> None:
        config = load_pipeline_config(PIPELINE_TOML)
        discovery = config.phases["research_discovery"]

        assert discovery.id == 0
        assert discovery.entry_point is True
        assert "research-manifest" in discovery.inputs
        assert "raw-collection" in discovery.outputs
        assert len(discovery.tools) > 0

    def test_parses_edges(self) -> None:
        config = load_pipeline_config(PIPELINE_TOML)

        assert len(config.edges) > 0
        first_edge = config.edges[0]
        assert isinstance(first_edge.from_, str)
        assert isinstance(first_edge.to, str)

    def test_parses_debate_config(self) -> None:
        config = load_pipeline_config(PIPELINE_TOML)

        assert config.debate is not None
        assert "advocate" in config.debate.agents
        assert "synthesizer" in config.debate.agents
        assert config.debate.rounds["round_1"] == "divergent"
        assert config.debate.rounds["round_2"] == "convergent"

    def test_parses_schema_references(self) -> None:
        config = load_pipeline_config(PIPELINE_TOML)

        assert len(config.schemas) == 9
        assert config.schemas["pipeline_run"].endswith(".json")

    def test_parses_storage_config(self) -> None:
        config = load_pipeline_config(PIPELINE_TOML)

        assert config.storage is not None
        assert config.storage.base_dir == "pipeline_mcp_data"
        assert config.storage.paths["specs"]
        assert config.storage.paths["overviews"]

    def test_throws_on_missing_file(self) -> None:
        with pytest.raises(FileNotFoundError):
            load_pipeline_config("/nonexistent/pipeline.toml")

    def test_parses_optional_per_phase_model_field(self, tmp_path: Path) -> None:
        """Feature B precedence top: optional per-phase ``model`` field.

        A phase without a ``model`` field must parse to ``None`` (the Python
        analogue of the Node test's ``undefined`` — the precedence fall-through
        sentinel); a phase with ``model`` keeps the override string verbatim.
        """
        toml_path = tmp_path / "pipeline.toml"
        toml_path.write_text(
            textwrap.dedent(
                """\
                [pipeline]
                id = "test-pipeline"
                version = "0.0.1"
                description = "fixture for B3 model-field parser test"

                [phases.phase_a]
                id = 0
                description = "no model override"
                inputs = []
                outputs = []
                tools = []
                entry_point = true

                [phases.phase_b]
                id = 1
                description = "with model override"
                inputs = []
                outputs = []
                tools = []
                entry_point = false
                model = "claude-opus-4-6"

                [[edges]]
                from = "phase_a"
                to = "phase_b"

                [debate]
                [debate.rounds]
                [debate.output]
                includes_transcript = true
                includes_refined_artifact = true
                artifact_version_bump = "minor"

                [schemas]
                pipeline_run = "pipeline/schemas/pipeline-run.json"

                [storage]
                base_dir = "pipeline_mcp_data"
                [storage.paths]
                specs = "specs/"
                """
            ),
            encoding="utf-8",
        )

        config = load_pipeline_config(toml_path)

        assert config.phases["phase_a"].model is None, (
            "phase without model field must parse to None for precedence fall-through"
        )
        assert config.phases["phase_b"].model == "claude-opus-4-6", (
            "phase with model field must parse the override string verbatim"
        )


class TestLoadOtherLoaders:
    """Parity coverage for ``load_quality_gates`` / ``load_hooks_config``.

    Not among the 9 ported ``loadPipelineConfig`` cases; these pin the two
    remaining loaders' documented behaviour (file-absent → ``[]``, the hook
    trigger filter, and the bundled-asset shapes).
    """

    def test_quality_gates_missing_file_returns_empty(self, tmp_path: Path) -> None:
        assert load_quality_gates(tmp_path / "nope.toml") == []

    def test_quality_gates_parses_bundled_asset(self) -> None:
        gates = load_quality_gates(QUALITY_GATES_TOML)

        phases = [g.phase for g in gates]
        assert phases == [
            "curation",
            "design_synthesis",
            "concept_extraction",
            "validation",
        ]
        curation = gates[0]
        assert curation.on_failure == "block"
        assert curation.checks[0].check_type == "field_present"
        assert curation.checks[0].params["field"] == "sources"

    def test_hooks_missing_file_returns_empty(self, tmp_path: Path) -> None:
        assert load_hooks_config(tmp_path / "nope.toml") == []

    def test_hooks_bundled_asset_has_no_hooks_array_returns_empty(self) -> None:
        # The bundled hooks-config.toml is the Claude Code harness config — it
        # has no [[hooks]] array, so load_hooks_config returns [].
        assert load_hooks_config(HOOKS_CONFIG_TOML) == []

    def test_hooks_trigger_filter_and_defaults(self, tmp_path: Path) -> None:
        toml_path = tmp_path / "hooks.toml"
        toml_path.write_text(
            textwrap.dedent(
                """\
                [[hooks]]
                trigger = "pre_start"
                command = "scripts/validate-env.sh"
                args = ["--strict"]
                timeout_ms = 1234

                [[hooks]]
                trigger = "post_complete"
                command = "scripts/notify.sh"

                [[hooks]]
                trigger = "not_a_real_trigger"
                command = "scripts/ignored.sh"
                """
            ),
            encoding="utf-8",
        )

        hooks = load_hooks_config(toml_path)

        assert len(hooks) == 2  # invalid-trigger hook dropped, not errored
        assert hooks[0].trigger == "pre_start"
        assert hooks[0].args == ["--strict"]
        assert hooks[0].timeout_ms == 1234
        # nullish defaults applied to the second hook
        assert hooks[1].phase_filter == "*"
        assert hooks[1].args == []
        assert hooks[1].timeout_ms == 5000
