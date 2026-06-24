"""Quality gates (spec §7.5) — port of ``legacy-node/quality-gates.ts``.

Pure quality-gate check functions plus the per-phase orchestrator. Three check
kinds, byte-exact with the Node originals:

* :func:`check_field_present` — required field present + non-empty, with
  **dot-path** recursive descent (e.g. ``"architecture.components"``).
* :func:`check_min_items` — array field has at least ``min`` items (literal key,
  **no** dot-path).
* :func:`check_cross_ref_valid` — every value in a source array exists as a key
  (object target) or value (array target) of a target artifact field.

:func:`run_check` dispatches by ``check_type``; :func:`load_artifact_for_gate`
reads + JSON-parses an artifact from disk (``None`` on missing/unparseable);
:func:`run_gate_checks` loads the phase's artifacts and evaluates every check.

Result shapes (:class:`CheckResult` / :class:`GateResult`) are the run-time
evaluation outputs from ``types.ts``; they live here (not ``models.py``) because
this is the only module that produces them. They are **frozen** dataclasses — a
gate result is an immutable record of one evaluation.

Scope boundary (T2.4): this module is the *pure* gate engine. Enforcement wiring
into ``complete_phase`` (the ``gate_evaluated`` event emission + atomic rollback
ordering before ``gate_blocked``) lands in the lifecycle-handler layer (T6.2);
the engine here is tested in isolation. :func:`load_quality_gates` is the TOML
loader, already implemented in :mod:`pipeline_orchestrator.toml_loader` and
re-exported here for the ``quality-gates.ts`` import surface.

Parity notes carried over from the Node source:

* Param reads use ``params.get(key)`` (the TS ``params.field as string`` cast,
  which yields ``undefined``/``None`` when absent) — the misconfigured-check
  guards mirror the TS ``if (!field)`` / ``min === undefined`` falsy tests.
* ``check_min_items`` reproduces the Node behavior: a non-array ``field`` is
  reported as "not an array", and the pass message is byte-identical to the fail
  message (Node returns the same string in both arms).
* ``load_artifact_for_gate`` swallows *any* parse error (Node ``catch {}``),
  returning ``None`` — matching the JSON-decode + read-error fall-through.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from pipeline_orchestrator.models import ArtifactRef, QualityCheck, QualityGate
from pipeline_orchestrator.toml_loader import load_quality_gates

__all__ = [
    "CheckResult",
    "GateResult",
    "check_cross_ref_valid",
    "check_field_present",
    "check_min_items",
    "load_artifact_for_gate",
    "load_quality_gates",
    "run_check",
    "run_gate_checks",
]


# ── Result shapes (mirror the TS ``CheckResult`` / ``GateResult``) ───


@dataclass(frozen=True)
class CheckResult:
    """Outcome of a single quality check (mirrors TS ``CheckResult``).

    ``check_type`` is a free-form ``str`` (NOT the :data:`CheckType` literal) so
    an unknown check type can be echoed back verbatim in
    :func:`run_check`'s failure path, exactly as the Node code does.
    """

    check_type: str
    passed: bool
    message: str


@dataclass(frozen=True)
class GateResult:
    """Outcome of running all checks for a phase gate (mirrors TS ``GateResult``)."""

    phase: str
    passed: bool
    on_failure: str
    results: list[CheckResult]


# ── Pure check functions ─────────────────────────────────────────────


def check_field_present(
    artifact: Mapping[str, Any],
    params: Mapping[str, Any],
) -> CheckResult:
    """Check that a required field is present and non-empty in an artifact.

    ``params``: ``{ field: str, artifact_type?: str }``. Supports dot-notation
    paths like ``"architecture.components"`` via recursive descent (Fix 4.2):
    each segment must index into an object, else the value resolves to ``None``.
    Empty strings (after ``strip``) and empty arrays fail.
    """
    field = params.get("field")
    if not field:
        return CheckResult(
            check_type="field_present",
            passed=False,
            message='Check misconfigured: missing "field" param',
        )

    field = str(field)

    # Support dot-notation paths like "architecture.components".
    if "." in field:
        value: Any = artifact
        for key in field.split("."):
            if value is None or not isinstance(value, Mapping):
                value = None
                break
            value = value.get(key)
    else:
        value = artifact.get(field)

    if value is None:
        return CheckResult(
            check_type="field_present",
            passed=False,
            message=f'Required field "{field}" is missing',
        )
    if isinstance(value, str) and len(value.strip()) == 0:
        return CheckResult(
            check_type="field_present",
            passed=False,
            message=f'Required field "{field}" is empty',
        )
    if isinstance(value, list) and len(value) == 0:
        return CheckResult(
            check_type="field_present",
            passed=False,
            message=f'Required field "{field}" is an empty array',
        )

    return CheckResult(
        check_type="field_present",
        passed=True,
        message=f'Field "{field}" is present',
    )


def check_min_items(
    artifact: Mapping[str, Any],
    params: Mapping[str, Any],
) -> CheckResult:
    """Check that an array field has at least ``min`` items.

    ``params``: ``{ field: str, min: number }``. Unlike
    :func:`check_field_present`, the key is **literal** — no dot-path descent.
    The pass and fail messages are identical (parity with the Node source, which
    returns the same string in both arms).
    """
    field = params.get("field")
    min_ = params.get("min")

    if not field or min_ is None:
        return CheckResult(
            check_type="min_items",
            passed=False,
            message='Check misconfigured: missing "field" or "min" param',
        )

    field = str(field)
    value = artifact.get(field)
    if not isinstance(value, list):
        return CheckResult(
            check_type="min_items",
            passed=False,
            message=f'Field "{field}" is not an array',
        )

    if len(value) < min_:
        return CheckResult(
            check_type="min_items",
            passed=False,
            message=f'Field "{field}" has {len(value)} items (minimum: {min_})',
        )

    return CheckResult(
        check_type="min_items",
        passed=True,
        message=f'Field "{field}" has {len(value)} items (minimum: {min_})',
    )


def check_cross_ref_valid(
    artifact: Mapping[str, Any],
    params: Mapping[str, Any],
    all_artifacts: Mapping[str, Mapping[str, Any]],
) -> CheckResult:
    """Check that cross-references between artifacts are valid.

    Validates that values in a source field exist as keys (object target) or
    values (array target) in a target artifact. ``params``:
    ``{ source_field, target_artifact_type, target_field }``.
    """
    source_field = params.get("source_field")
    target_type = params.get("target_artifact_type")
    target_field = params.get("target_field")

    if not source_field or not target_type or not target_field:
        return CheckResult(
            check_type="cross_ref_valid",
            passed=False,
            message=(
                "Check misconfigured: missing source_field, "
                "target_artifact_type, or target_field param"
            ),
        )

    source_field = str(source_field)
    target_type = str(target_type)
    target_field = str(target_field)

    source_values = artifact.get(source_field)
    if not isinstance(source_values, list):
        return CheckResult(
            check_type="cross_ref_valid",
            passed=False,
            message=f'Source field "{source_field}" is not an array',
        )

    # Find target artifact by type (insertion-order scan, matching the TS loop).
    target_artifact: Mapping[str, Any] | None = None
    for art_type, art in all_artifacts.items():
        if art_type == target_type:
            target_artifact = art
            break

    if target_artifact is None:
        return CheckResult(
            check_type="cross_ref_valid",
            passed=False,
            message=f'Target artifact of type "{target_type}" not found',
        )

    target_values = target_artifact.get(target_field)
    # TS: `!Array.isArray(targetValues) && typeof targetValues !== 'object'`.
    # In JS `typeof null === 'object'`, so a null target field is treated as
    # "iterable" and would fall through to Object.keys(null); the gate config
    # never produces that, and reproducing the JS null-as-object quirk is out of
    # scope. We treat list and (non-None) Mapping as iterable, everything else
    # not.
    if not isinstance(target_values, (list, Mapping)):
        return CheckResult(
            check_type="cross_ref_valid",
            passed=False,
            message=(
                f'Target field "{target_field}" in "{target_type}" '
                "is not iterable"
            ),
        )

    # For object targets, check keys; for array targets, check values.
    if isinstance(target_values, list):
        valid_refs = {str(v) for v in target_values}
    else:
        valid_refs = {str(k) for k in target_values}

    invalid = [v for v in source_values if str(v) not in valid_refs]
    if len(invalid) > 0:
        return CheckResult(
            check_type="cross_ref_valid",
            passed=False,
            message=(
                f"Cross-reference invalid: {len(invalid)} value(s) in "
                f'"{source_field}" not found in "{target_type}.{target_field}"'
            ),
        )

    return CheckResult(
        check_type="cross_ref_valid",
        passed=True,
        message=(
            f"All {len(source_values)} cross-references from "
            f'"{source_field}" are valid'
        ),
    )


# ── Check dispatcher ─────────────────────────────────────────────────


def run_check(
    check: QualityCheck,
    artifact: Mapping[str, Any],
    all_artifacts: Mapping[str, Mapping[str, Any]],
) -> CheckResult:
    """Run a single check against an artifact, dispatching by ``check_type``.

    Returns a failing :class:`CheckResult` (echoing the unknown type) when the
    ``check_type`` is not one of the three known kinds — parity with the Node
    dispatcher's ``Unknown check type`` fall-through.
    """
    if check.check_type == "field_present":
        return check_field_present(artifact, check.params)
    if check.check_type == "min_items":
        return check_min_items(artifact, check.params)
    if check.check_type == "cross_ref_valid":
        return check_cross_ref_valid(artifact, check.params, all_artifacts)
    return CheckResult(
        check_type=check.check_type,
        passed=False,
        message=f'Unknown check type: "{check.check_type}"',
    )


# ── Gate orchestrator ────────────────────────────────────────────────


def load_artifact_for_gate(path: str) -> dict[str, Any] | None:
    """Load an artifact from disk and parse as JSON.

    Returns ``None`` if the file does not exist or cannot be parsed (any error —
    matching the Node ``try { JSON.parse(...) } catch { return null }``).
    """
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            parsed: Any = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


def run_gate_checks(
    gate: QualityGate,
    artifact_refs: list[ArtifactRef],
) -> GateResult:
    """Run all gate checks for a phase.

    Loads artifacts from disk using ``artifact_refs`` (keyed by type into an
    insertion-ordered map — last writer per type wins, as in the JS ``Map.set``),
    then runs each check against the artifact selected by the check's
    ``artifact_type`` param (or the first loaded artifact when unspecified). A
    check whose target artifact is absent yields a failing result; the gate
    passes only when every check passes.
    """
    # Load all artifacts into a dict keyed by type (insertion order preserved).
    all_artifacts: dict[str, dict[str, Any]] = {}
    for ref in artifact_refs:
        artifact = load_artifact_for_gate(ref.path)
        if artifact is not None:
            all_artifacts[ref.type] = artifact

    results: list[CheckResult] = []
    for check in gate.checks:
        # Determine which artifact to check against.
        artifact_type = check.params.get("artifact_type")
        target_artifact: dict[str, Any] | None
        if artifact_type:
            target_artifact = all_artifacts.get(str(artifact_type))
        else:
            # Default to first artifact if no type specified.
            target_artifact = next(iter(all_artifacts.values()), None)

        if target_artifact is None:
            results.append(
                CheckResult(
                    check_type=check.check_type,
                    passed=False,
                    message=(
                        f'Artifact of type "{artifact_type}" not found for check'
                        if artifact_type
                        else "No artifacts available for check"
                    ),
                )
            )
            continue

        results.append(run_check(check, target_artifact, all_artifacts))

    passed = all(r.passed for r in results)

    return GateResult(
        phase=gate.phase,
        passed=passed,
        on_failure=gate.on_failure,
        results=results,
    )
