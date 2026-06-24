"""Port of ``legacy-node/tests/quality-gates-config.test.ts`` (1 case).

Config-integrity test pinning every ``artifact_type`` referenced in the bundled
``pipeline/quality-gates.toml`` to a known registered artifact type. Mirrors the
Node test's hardcoded ``KNOWN_ARTIFACT_TYPES`` set (derived from
``pipeline/schemas/*.json``) — update this set whenever a new
``schemas/*.json`` file is added.

Every ``artifact_type`` in the live toml (``curated-collection``, ``design-spec``,
``knowledge-overview``, ``validation-report``) is a member of this set; the test
fails loudly if a future edit references an unknown type.
"""

from __future__ import annotations

from pathlib import Path

from pipeline_orchestrator.toml_loader import load_quality_gates

QUALITY_GATES_TOML = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "pipeline_orchestrator"
    / "pipeline"
    / "quality-gates.toml"
)

# Canonical artifact types — derived from pipeline/schemas/*.json.
# Update this set whenever a new pipeline/schemas/*.json file is added.
KNOWN_ARTIFACT_TYPES: frozenset[str] = frozenset(
    {
        "raw-collection",
        "curated-collection",
        "knowledge-overview",
        "design-spec",
        "debate-transcript",
        "codebase-requirements",
        "validation-report",
        "research-manifest",
        "scaffold-document",
        "scaffold-manifest",
    }
)


class TestQualityGatesTomlConfigIntegrity:
    def test_every_artifact_type_param_references_a_known_registered_type(
        self,
    ) -> None:
        gates = load_quality_gates(QUALITY_GATES_TOML)
        assert len(gates) > 0, "expected quality-gates.toml to load at least one gate"

        for gate in gates:
            phase_name = gate.phase
            for check in gate.checks:
                gate_id = check.check_type
                params = check.params
                raw_artifact_type = params.get("artifact_type")
                if raw_artifact_type is None:
                    continue
                artifact_type = str(raw_artifact_type)
                assert artifact_type in KNOWN_ARTIFACT_TYPES, (
                    f'Phase "{phase_name}" gate "{gate_id}" references unknown '
                    f'artifact_type="{artifact_type}". Known: '
                    f"{', '.join(sorted(KNOWN_ARTIFACT_TYPES))}"
                )
