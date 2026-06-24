"""Cross-reference / semantic / completeness validation (port of
``legacy-node/cross-ref-validator.ts``, spec §7.5 / R3 §8).

Byte-exact behavioral port of the Node ``cross-ref-validator.ts`` module. The
four public validators —:func:`validate_cross_references`,
:func:`validate_semantics`, :func:`validate_run_completeness`, and the
orchestrator :func:`validate_run` — operate over a :class:`RunState`'s
registered artifacts, loading their JSON bodies from the **legacy flat per-type
directories** resolved through :class:`StorageConfig` (matching the TS R3 §8
behavior: read ``config.base_dir + config.paths[storage_key]``).

The four result interfaces from ``cross-ref-validator.ts`` map to ``@dataclass``
records (:class:`CrossRefIssue` / :class:`SemanticIssue` /
:class:`CompletenessIssue` / :class:`RunValidationReport`); the TS data fields
are already snake_case, so the field names are preserved verbatim.

Nullish parity: the TS ``getArtifactId`` uses ``(data.x as string) ?? ''`` —
ported as ``data.get(key) or ''`` so a missing/``None`` id collapses to ``''``
and id comparisons stay string-vs-string. The TS ``?? []`` array defaults map to
``or []`` over the fetched value.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

from .models import RunState
from .storage import StorageConfig

# ── Result types ─────────────────────────────────────────────────────


@dataclass
class CrossRefIssue:
    """A dangling cross-reference (mirrors TS ``CrossRefIssue``)."""

    source_artifact: str  # e.g. "debate-transcript:dt-aabb1122"
    field: str  # e.g. "input_id"
    referenced_id: str  # e.g. "ov-12345678"
    expected_type: str  # e.g. "knowledge-overview"
    message: str


@dataclass
class SemanticIssue:
    """A semantic-rule violation (mirrors TS ``SemanticIssue``)."""

    artifact: str  # e.g. "debate-transcript:dt-aabb1122"
    rule: str  # e.g. "debate_min_round1_agents"
    message: str


@dataclass
class CompletenessIssue:
    """A completed phase missing an output (mirrors TS ``CompletenessIssue``)."""

    phase: str
    missing_type: str
    message: str


@dataclass
class RunValidationReport:
    """Aggregate :func:`validate_run` result (mirrors TS ``RunValidationReport``)."""

    valid: bool
    cross_ref_issues: list[CrossRefIssue] = field(default_factory=list)
    semantic_issues: list[SemanticIssue] = field(default_factory=list)
    completeness_issues: list[CompletenessIssue] = field(default_factory=list)
    summary: str = ""
    artifacts_checked: int = 0


# ── Internal helpers ─────────────────────────────────────────────────


@dataclass
class _ArtifactEntry:
    """A loaded artifact: its :class:`ArtifactRef`-derived ``ref`` plus parsed ``data``.

    Mirrors the TS ``{ ref: ArtifactRef; data: Record<string, unknown> }`` entry
    value in the ``Map<string, Array<…>>`` returned by ``loadArtifactsByType``.
    """

    ref: Any  # ArtifactRef
    data: dict[str, Any]


# Map artifact types to storage keys (the exact 6 mappings from the TS source).
_TYPE_TO_STORAGE_KEY: dict[str, str] = {
    "raw-collection": "raw_collections",
    "curated-collection": "curated_collections",
    "knowledge-overview": "overviews",
    "design-spec": "specs",
    "debate-transcript": "debates",
    "codebase-requirements": "codebase",
}


def load_artifacts_by_type(
    state: RunState,
    config: StorageConfig,
    parse_errors: list[CrossRefIssue] | None = None,
) -> dict[str, list[_ArtifactEntry]]:
    """Build a map of artifact type → list of loaded artifact objects.

    Only loads artifacts registered in the run state and whose type maps to a
    populated storage key. Reads from the legacy flat per-type directory
    (``config.base_dir + config.paths[storage_key]``), matching by
    ``basename(ref.path)``. On a JSON parse failure, appends a
    :class:`CrossRefIssue` to ``parse_errors`` only when that list was supplied
    (``validate_cross_references`` passes its issues list;
    ``validate_semantics`` passes ``None``).
    """
    artifact_map: dict[str, list[_ArtifactEntry]] = {}

    for ref in state.available_artifacts:
        storage_key = _TYPE_TO_STORAGE_KEY.get(ref.type)
        if not storage_key or not config.paths.get(storage_key):
            continue

        directory = os.path.join(config.base_dir, config.paths[storage_key])
        file_name = os.path.basename(ref.path)
        file_path = os.path.join(directory, file_name)

        if not os.path.exists(file_path):
            continue

        try:
            with open(file_path, encoding="utf-8") as fh:
                data: dict[str, Any] = json.load(fh)
            entries = artifact_map.get(ref.type)
            if entries is None:
                entries = []
            entries.append(_ArtifactEntry(ref=ref, data=data))
            artifact_map[ref.type] = entries
        except (json.JSONDecodeError, ValueError, OSError) as err:
            if parse_errors is not None:
                parse_errors.append(
                    CrossRefIssue(
                        source_artifact=f"{ref.type}:{os.path.basename(ref.path)}",
                        field="(file)",
                        referenced_id="",
                        expected_type=ref.type,
                        message=f'Failed to parse artifact at "{file_path}": {err}',
                    )
                )

    return artifact_map


def get_artifact_id(type: str, data: dict[str, Any]) -> str:
    """Return the primary ID of an artifact based on its type, or ``''``.

    Mirrors the TS ``(data.x as string) ?? ''`` via ``data.get(key) or ''`` so a
    missing/``None`` id collapses to the empty string.
    """
    if type in ("raw-collection", "curated-collection"):
        return data.get("collection_id") or ""
    if type == "knowledge-overview":
        return data.get("overview_id") or ""
    if type == "design-spec":
        return data.get("spec_id") or ""
    if type == "debate-transcript":
        return data.get("transcript_id") or ""
    if type == "codebase-requirements":
        return data.get("requirements_id") or ""
    return ""


def id_exists(
    artifact_map: dict[str, list[_ArtifactEntry]],
    target_types: list[str],
    target_id: str,
) -> bool:
    """Whether ``target_id`` is the primary id of any loaded artifact of those types."""
    for type_ in target_types:
        entries = artifact_map.get(type_)
        if not entries:
            continue
        for entry in entries:
            if get_artifact_id(type_, entry.data) == target_id:
                return True
    return False


def collection_exists(
    artifact_map: dict[str, list[_ArtifactEntry]],
    collection_id: str,
) -> bool:
    """Whether a ``collection_id`` exists in either raw or curated collections."""
    return id_exists(
        artifact_map, ["raw-collection", "curated-collection"], collection_id
    )


# ── Cross-reference validation ───────────────────────────────────────


def validate_cross_references(
    state: RunState,
    config: StorageConfig,
) -> list[CrossRefIssue]:
    """Validate that every artifact cross-reference resolves to a run artifact.

    Four rules (byte-exact messages from the TS source):
      1. knowledge-overview ``sources[].collection`` must exist in raw|curated.
      2. design-spec ``sources[].collection`` likewise.
      3. debate-transcript ``input_id`` must exist in the ``input_type``-resolved
         target (``knowledge-overview`` or ``design-spec``).
      4. debate-transcript ``codebase_requirements_id`` (when present) must exist
         in ``codebase-requirements``.
    """
    issues: list[CrossRefIssue] = []
    artifact_map = load_artifacts_by_type(state, config, issues)

    # Check knowledge-overview sources[].collection
    for entry in artifact_map.get("knowledge-overview") or []:
        id_ = get_artifact_id("knowledge-overview", entry.data)
        label = f"knowledge-overview:{id_}"
        sources = entry.data.get("sources") or []

        for source in sources:
            collection = source.get("collection")
            if collection and not collection_exists(artifact_map, collection):
                issues.append(
                    CrossRefIssue(
                        source_artifact=label,
                        field="sources[].collection",
                        referenced_id=collection,
                        expected_type="curated-collection or raw-collection",
                        message=(
                            f'Overview "{id_}" references collection "{collection}" '
                            "which is not in the run's artifacts"
                        ),
                    )
                )

    # Check design-spec sources[].collection
    for entry in artifact_map.get("design-spec") or []:
        id_ = get_artifact_id("design-spec", entry.data)
        label = f"design-spec:{id_}"
        sources = entry.data.get("sources") or []

        for source in sources:
            collection = source.get("collection")
            if collection and not collection_exists(artifact_map, collection):
                issues.append(
                    CrossRefIssue(
                        source_artifact=label,
                        field="sources[].collection",
                        referenced_id=collection,
                        expected_type="curated-collection or raw-collection",
                        message=(
                            f'Spec "{id_}" references collection "{collection}" '
                            "which is not in the run's artifacts"
                        ),
                    )
                )

    # Check debate-transcript input_id
    for entry in artifact_map.get("debate-transcript") or []:
        id_ = get_artifact_id("debate-transcript", entry.data)
        label = f"debate-transcript:{id_}"
        input_id = entry.data.get("input_id")
        input_type = entry.data.get("input_type")

        if input_id and input_type:
            target_type = (
                "knowledge-overview"
                if input_type == "knowledge-overview"
                else "design-spec"
            )
            if not id_exists(artifact_map, [target_type], input_id):
                issues.append(
                    CrossRefIssue(
                        source_artifact=label,
                        field="input_id",
                        referenced_id=input_id,
                        expected_type=target_type,
                        message=(
                            f'Debate "{id_}" references {target_type} "{input_id}" '
                            "which is not in the run's artifacts"
                        ),
                    )
                )

        # Check codebase_requirements_id
        cr_id = entry.data.get("codebase_requirements_id")
        if cr_id:
            if not id_exists(artifact_map, ["codebase-requirements"], cr_id):
                issues.append(
                    CrossRefIssue(
                        source_artifact=label,
                        field="codebase_requirements_id",
                        referenced_id=cr_id,
                        expected_type="codebase-requirements",
                        message=(
                            f'Debate "{id_}" references codebase-requirements '
                            f'"{cr_id}" which is not in the run\'s artifacts'
                        ),
                    )
                )

    return issues


# ── Semantic validation ──────────────────────────────────────────────


def validate_semantics(
    state: RunState,
    config: StorageConfig,
) -> list[SemanticIssue]:
    """Validate per-artifact semantic invariants (byte-exact messages).

    Four rules: debate transcripts need ≥2 round-1 agents
    (``debate_min_round1_agents``); design specs need ≥1 source
    (``spec_has_sources``); knowledge overviews need ≥1 concept
    (``overview_has_concepts``); overview theme ``concept_names`` must reference
    actual concept names (``overview_theme_concepts_exist``).
    """
    issues: list[SemanticIssue] = []
    artifact_map = load_artifacts_by_type(state, config)

    # Rule: debate transcripts should have at least 2 round-1 arguments
    for entry in artifact_map.get("debate-transcript") or []:
        id_ = get_artifact_id("debate-transcript", entry.data)
        label = f"debate-transcript:{id_}"
        rounds = entry.data.get("rounds") or []
        round1 = next((r for r in rounds if r.get("round") == 1), None)

        agents = round1.get("agents") if round1 else None
        if not round1 or not agents or len(agents) < 2:
            found = len(agents) if agents else 0
            issues.append(
                SemanticIssue(
                    artifact=label,
                    rule="debate_min_round1_agents",
                    message=(
                        f'Debate "{id_}" has fewer than 2 round-1 arguments '
                        f"(found {found}). Effective debate requires at least "
                        "advocate and critic."
                    ),
                )
            )

    # Rule: design specs should have at least one source
    for entry in artifact_map.get("design-spec") or []:
        id_ = get_artifact_id("design-spec", entry.data)
        label = f"design-spec:{id_}"
        sources = entry.data.get("sources") or []

        if len(sources) == 0:
            issues.append(
                SemanticIssue(
                    artifact=label,
                    rule="spec_has_sources",
                    message=(
                        f'Spec "{id_}" has no sources. Design specs should '
                        "reference at least one research source."
                    ),
                )
            )

    # Rule: knowledge overviews should have at least one concept
    for entry in artifact_map.get("knowledge-overview") or []:
        id_ = get_artifact_id("knowledge-overview", entry.data)
        label = f"knowledge-overview:{id_}"
        concepts = entry.data.get("concepts") or []

        if len(concepts) == 0:
            issues.append(
                SemanticIssue(
                    artifact=label,
                    rule="overview_has_concepts",
                    message=(
                        f'Overview "{id_}" has no concepts. Concept extraction '
                        "should produce at least one concept."
                    ),
                )
            )

        # Rule: theme concept_names should reference actual concept names
        concept_names = {c.get("name") for c in concepts}
        themes = entry.data.get("themes") or []

        for theme in themes:
            for c_name in theme.get("concept_names") or []:
                if c_name not in concept_names:
                    issues.append(
                        SemanticIssue(
                            artifact=label,
                            rule="overview_theme_concepts_exist",
                            message=(
                                f'Overview "{id_}" theme "{theme.get("name")}" '
                                f'references concept "{c_name}" which is not '
                                "defined in the overview's concepts list."
                            ),
                        )
                    )

    return issues


# ── Run completeness validation ──────────────────────────────────────


def validate_run_completeness(
    state: RunState,
    phase_output_map: dict[str, list[str]],
) -> list[CompletenessIssue]:
    """Flag completed phases whose expected output artifact types are absent.

    Pure ref-type check (no file loads): for each ``completed`` phase that has a
    ``phase_output_map`` entry, every expected type must appear among the
    registered ``available_artifacts`` types.
    """
    issues: list[CompletenessIssue] = []
    available_types = {a.type for a in state.available_artifacts}

    for phase_name, phase_state in state.phases.items():
        # Only check completed phases
        if phase_state.status != "completed":
            continue

        expected_outputs = phase_output_map.get(phase_name)
        if not expected_outputs:
            continue

        for expected_type in expected_outputs:
            if expected_type not in available_types:
                issues.append(
                    CompletenessIssue(
                        phase=phase_name,
                        missing_type=expected_type,
                        message=(
                            f'Phase "{phase_name}" completed but no '
                            f'"{expected_type}" artifact was produced.'
                        ),
                    )
                )

    return issues


# ── Full run validation ──────────────────────────────────────────────


def validate_run(
    state: RunState,
    config: StorageConfig,
    phase_output_map: dict[str, list[str]],
) -> RunValidationReport:
    """Run all three validators and aggregate into a :class:`RunValidationReport`."""
    cross_ref_issues = validate_cross_references(state, config)
    semantic_issues = validate_semantics(state, config)
    completeness_issues = validate_run_completeness(state, phase_output_map)

    total_issues = (
        len(cross_ref_issues) + len(semantic_issues) + len(completeness_issues)
    )
    valid = total_issues == 0

    parts: list[str] = []
    if len(cross_ref_issues) > 0:
        parts.append(f"{len(cross_ref_issues)} cross-reference issue(s)")
    if len(semantic_issues) > 0:
        parts.append(f"{len(semantic_issues)} semantic issue(s)")
    if len(completeness_issues) > 0:
        parts.append(f"{len(completeness_issues)} completeness issue(s)")

    summary = (
        f"All checks passed. {len(state.available_artifacts)} artifacts validated."
        if valid
        else f"Found {total_issues} issue(s): {', '.join(parts)}."
    )

    return RunValidationReport(
        valid=valid,
        cross_ref_issues=cross_ref_issues,
        semantic_issues=semantic_issues,
        completeness_issues=completeness_issues,
        summary=summary,
        artifacts_checked=len(state.available_artifacts),
    )
