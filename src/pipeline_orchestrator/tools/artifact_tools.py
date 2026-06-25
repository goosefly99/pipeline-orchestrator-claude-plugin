"""Artifact + scaffold MCP tool bodies (port of ``legacy-node/artifact-handlers.ts``
plus ``handleRegisterScaffoldOutputs`` from ``legacy-node/misc-handlers.ts``).

The six tool handlers (``handle_validate_artifact`` / ``handle_store_artifact`` /
``handle_register_artifact`` / ``handle_load_artifact`` / ``handle_list_artifacts``
/ ``handle_register_scaffold_outputs``) take an :class:`ArtifactContext` DI object —
the seam ``server.py`` wires to the real schemas / validator / storage / run-state /
event functions — and return a :class:`~pipeline_orchestrator.models.HandlerResponse`.
Plus the two pure helpers :func:`compute_next_version` / :func:`enforce_response_size`
and the two scaffold-anchor helpers :func:`extract_proposed_diff_anchor_refs` /
:func:`markdown_has_heading`.

Wire format (sec 4.2 / sec 5):

* ``handle_validate_artifact`` -- VALID returns the ``{status, data, next_step}``
  envelope pretty-JSON (E); INVALID returns a plain-text ``"Validation FAILED
  against ...\n<errors>"`` block with ``is_error=True`` (T). No JSON wrap on
  the failure path.
* ``handle_store_artifact`` -- the ``{status, data, next_step}`` envelope (E).
* ``handle_register_artifact`` -- a plain JSON object (``{registered, ...}``) (J)
  on success; plain-text ``"Validation FAILED ..."`` with ``is_error=True`` (T)
  when ``validate=True`` and validation fails.
* ``handle_load_artifact`` -- summary / full (``enforce_response_size`` 50000) /
  fields modes; all JSON, never JSON-wrapped.
* ``handle_list_artifacts`` -- a plain JSON object (``{storage_key, artifacts}``).
* ``handle_register_scaffold_outputs`` -- the ``{status, data, [warnings],
  next_step}`` envelope (E); ``data.anchor_warnings`` AND top-level ``warnings``
  are OMITTED when no anchors are missing (undefined-drop parity with the TS
  conditional spread).

Parity notes:

* **``ensure_ascii=False`` on every ``json.dumps``** so non-ASCII survives raw
  (the Node ``JSON.stringify`` emits raw UTF-8). The envelope is serialized
  directly here rather than via ``ResponseEnvelope.to_json`` (which omits
  ``ensure_ascii=False``).
* **Nullish, not falsy.** Every TS ``??`` / ``=== undefined`` / ``!== undefined``
  ports as ``.get(k, default)`` / ``is None`` / ``is not None`` so an explicit
  falsy value (``0`` / ``False`` / ``""`` / ``[]``) survives.
* **undefined-drop.** TS conditional spreads ``...(x ? {k:v} : {})`` port to
  OMITTING the key from the dict when absent -- never emitting ``null``/``None``
  where Node drops the key.
* **``sorted(os.listdir(...))``** for the run-scoped listing -- the Node code uses
  ``readdirSync`` (code-point order); Python ``os.listdir`` is unordered, so we
  sort for byte-exact array parity.
* **Diagnostics -> stderr.** ``safe_size_bytes`` / anchor-validation warnings go
  to stderr (the Node ``console.warn``); nothing writes to stdout.
* **Events.** ``artifact_stored`` events reuse :func:`append_event` (the same seam
  ``lifecycle_tools`` uses); the payload key order matches the TS object literal
  (``timestamp``, ``event``, ``phase``, ``run_id``, ``details`` -> ``artifact_type``,
  ``path``, ``size_bytes``).

:func:`register_artifact_tools` registers the 6 ``pipeline_*`` artifact/scaffold
tools (``validate_artifact`` / ``store_artifact`` / ``register_artifact`` /
``register_scaffold_outputs`` / ``load_artifact`` / ``list_artifacts``) with their
per-tool ``_meta.max_result_chars`` (50000 read: validate/load/list / 10000 mutate:
store/register_artifact/register_scaffold_outputs) + ``ToolAnnotations`` subset from
the frozen ``tool-schemas.ts`` map. It is NOT called from anywhere yet: the global
``register_tools`` seam (``tools/__init__.py``) stays a no-op until the wiring task
(T6.5), so the global handshake enumerates 0 tools.
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from mcp.types import ToolAnnotations

from pipeline_orchestrator.errors import ErrorClass, PipelineError
from pipeline_orchestrator.models import ArtifactRef, HandlerResponse, RunState
from pipeline_orchestrator.storage import (
    ArtifactSummary,
    StorageConfig,
    get_artifact_dir,
    storage_key_to_subtype,
)
from pipeline_orchestrator.tools._envelope import tool_result
from pipeline_orchestrator.tools.lifecycle_tools import append_event
from pipeline_orchestrator.validator import SchemaMap, ValidationResult

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP


# -- Helpers ---------------------------------------------------------------


def _now_iso() -> str:
    """Return the current UTC time as an ISO 8601 string (Node ``toISOString()``)."""
    now = datetime.now(UTC)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def _dumps(obj: object) -> str:
    """``json.dumps(obj, indent=2)`` with ``ensure_ascii=False`` (raw-UTF-8 parity)."""
    return json.dumps(obj, indent=2, ensure_ascii=False)


def safe_size_bytes(path: str) -> int:
    """Read a file's size in bytes; warn to stderr and return 0 on failure.

    Mirrors ``safeSizeBytes`` in ``artifact-handlers.ts`` -- if ``stat`` throws
    (the file was moved/deleted between write and event emission), log a warning
    and return 0 rather than swallowing the exception, keeping the event log
    authoritative under races.
    """
    try:
        return os.stat(path).st_size
    except OSError as err:
        print(
            f"[artifact-handlers] unable to stat artifact for size_bytes: "
            f"{path} — {err}",
            file=sys.stderr,
        )
        return 0


def _resolve_file_path(file_path: str, base_dir: str) -> str:
    """Resolve a possibly-relative path against ``base_dir`` (Node ``resolveFilePath``).

    Absolute paths pass through unchanged; relative paths resolve against
    ``base_dir`` exactly as Node ``resolve(baseDir, filePath)``.
    """
    if os.path.isabs(file_path):
        return file_path
    return os.path.abspath(os.path.join(base_dir, file_path))


# -- Context provided by server.py (the DI seam) --------------------------


@dataclass
class ArtifactContext:
    """Dependency object the artifact handlers call through (TS ``ArtifactContext``).

    ``server.py`` (T6.5) wires every callable to the real schemas / validator /
    storage / run-state / event functions; tests inject stubs that capture local
    state (mirroring the TS ``makeCtx`` factory). ``persist_artifact`` is the
    optional run-scoped writer (Feature C): when present AND the active run's
    ``run_data_dir`` is populated AND the ``storage_key`` maps to a known subtype,
    :func:`handle_store_artifact` routes through it instead of ``store_artifact``.
    """

    get_schemas: Callable[[], SchemaMap]
    validate_artifact: Callable[[SchemaMap, str, object], ValidationResult]
    store_artifact: Callable[..., str]
    load_artifact: Callable[[StorageConfig, str, str], object | None]
    list_artifacts: Callable[[StorageConfig, str], list[str]]
    build_artifact_summary: Callable[[object, str, str], ArtifactSummary]
    get_storage_config: Callable[..., StorageConfig]
    base_dir_from_run_dir: Callable[[str], str]
    get_project_root: Callable[[], str]
    get_config_storage_base_dir: Callable[[], str]
    get_active_run: Callable[[], RunState | None]
    set_active_run: Callable[[RunState], None]
    get_active_run_dir: Callable[[], str]
    require_run: Callable[[], RunState]
    add_artifact: Callable[[RunState, ArtifactRef, str], RunState]
    persist_artifact: Callable[..., str] | None = None


@dataclass
class RegisterScaffoldOutputsContext:
    """DI object for :func:`handle_register_scaffold_outputs` (TS
    ``RegisterScaffoldOutputsContext``). A narrower seam than
    :class:`ArtifactContext`."""

    require_run: Callable[[], RunState]
    set_active_run: Callable[[RunState], None]
    get_active_run: Callable[[], RunState | None]
    get_active_run_dir: Callable[[], str]
    base_dir_from_run_dir: Callable[[str], str]
    add_artifact: Callable[[RunState, ArtifactRef, str], RunState]


# -- Pure helpers (exported) ----------------------------------------------


def compute_next_version(
    available_artifacts: list[ArtifactRef],
    artifact_type: str,
    phase: str,
) -> int:
    """Compute the next version number for an artifact of a given type+phase.

    Mirrors ``computeNextVersion`` in ``artifact-handlers.ts``. Scans
    ``available_artifacts`` for refs matching BOTH ``type`` and ``phase``, then
    returns ``max(existing versions) + 1`` (a missing ``version`` counts as 1,
    matching the TS ``a.version ?? 1`` nullish coalesce). Returns 1 when no
    duplicates exist.
    """
    matches = [
        a for a in available_artifacts if a.type == artifact_type and a.phase == phase
    ]
    if len(matches) == 0:
        return 1
    max_version = max((a.version if a.version is not None else 1) for a in matches)
    return max_version + 1


def enforce_response_size(
    response_json: str,
    max_chars: int,
    continuation_ref: dict[str, str] | None = None,
) -> str:
    """Enforce response size limits (Node ``enforceResponseSize``).

    If ``response_json`` is within ``max_chars`` it is returned unchanged. When it
    exceeds and a ``continuation_ref`` (``{storage_key, file_name}``) is supplied,
    a truncation envelope is returned (pretty JSON). With no continuation ref the
    response is sliced to ``max_chars`` and an ``"\n... (truncated)"`` marker is
    appended.
    """
    if len(response_json) <= max_chars:
        return response_json

    if continuation_ref:
        truncated = {
            "truncated": True,
            "preview_length": max_chars,
            "full_length": len(response_json),
            "message": (
                f"Response truncated to {max_chars} chars. Use "
                "pipeline_load_artifact with inline=true to retrieve full content."
            ),
            "continuation_ref": continuation_ref,
        }
        return _dumps(truncated)

    # No continuation ref -- just truncate with a message.
    return response_json[:max_chars] + "\n... (truncated)"


# -- Scaffold anchor-validation helpers (exported) ------------------------

# Quoted forms we care about (Node ``patterns`` in ``extractProposedDiffAnchorRefs``).
# Supports double quotes, single quotes, and backticks; the heading text itself
# cannot contain the matching quote char.
_ANCHOR_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"under the existing\s+[\"'`]([^\"'`]+?)[\"'`]", re.IGNORECASE),
    re.compile(r"insert(?:ing)? after the\s+[\"'`]([^\"'`]+?)[\"'`]", re.IGNORECASE),
    re.compile(
        r"under the\s+[\"'`]([^\"'`]+?)[\"'`]\s+(?:section|heading)",
        re.IGNORECASE,
    ),
]

_LEADING_HASHES = re.compile(r"^#+\s*")
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$")


def extract_proposed_diff_anchor_refs(diff_content: str) -> list[str]:
    """Extract heading/anchor references a proposed-diff claims already exist.

    Mirrors ``extractProposedDiffAnchorRefs`` in ``misc-handlers.ts``. Matches
    only phrasing that asserts an EXISTING anchor (``under the existing "X"`` /
    ``insert after the "X"`` / ``under the "X" section|heading``); "create a new
    section" declarations are deliberately not matched. Captured headings are
    lowercased + trimmed; leading markdown hashes are stripped so backtick-quoted
    ATX headings normalize to ``"quality gates"``. Returns a deduplicated list
    (first-seen order preserved).
    """
    if not diff_content:
        return []

    out: list[str] = []
    seen: set[str] = set()
    for pattern in _ANCHOR_PATTERNS:
        for match in pattern.finditer(diff_content):
            raw = match.group(1)
            if not raw:
                continue
            normalized = _LEADING_HASHES.sub("", raw).strip().lower()
            if normalized and normalized not in seen:
                seen.add(normalized)
                out.append(normalized)
    return out


def markdown_has_heading(markdown_content: str, heading_text: str) -> bool:
    """Report whether ``markdown_content`` has an ATX heading matching ``heading_text``.

    Mirrors ``markdownHasHeading`` in ``misc-handlers.ts``. Matches any ATX level
    (``#``..``######``); matching is case-insensitive and whitespace-trimmed.
    Returns ``False`` for empty content or an empty needle.
    """
    if not markdown_content or not heading_text:
        return False
    needle = heading_text.strip().lower()
    if not needle:
        return False
    for line in re.split(r"\r?\n", markdown_content):
        match = _HEADING_RE.match(line)
        if not match:
            continue
        heading = match.group(1).strip().lower()
        if heading == needle:
            return True
    return False


# -- Handlers -------------------------------------------------------------


def handle_validate_artifact(
    args: dict[str, Any], ctx: ArtifactContext
) -> HandlerResponse:
    """Validate an inline artifact or a file-path artifact against a schema.

    Port of ``handleValidateArtifact``. VALID -> ``{status, data, next_step}``
    envelope (E); INVALID -> plain-text ``"Validation FAILED against ...\n<errors>"``
    with ``is_error=True`` (T).
    """
    schema_file = args.get("schema")
    artifact = args.get("artifact")
    file_path = args.get("file_path")

    if not schema_file:
        raise PipelineError(
            "schema is required",
            ErrorClass.validation_error,
            recovery_action=(
                "Provide the schema parameter with the name of a JSON Schema file."
            ),
        )
    if not artifact and not file_path:
        raise PipelineError(
            "Either artifact (inline JSON) or file_path must be provided",
            ErrorClass.validation_error,
            recovery_action=(
                "Provide either an inline artifact object or a file_path to a "
                "JSON file."
            ),
        )

    resolved_artifact: object
    if file_path:
        assert isinstance(file_path, str)
        active_run_dir = ctx.get_active_run_dir()
        if ctx.get_active_run():
            base_dir = ctx.base_dir_from_run_dir(active_run_dir)
        else:
            base_dir = os.path.abspath(
                os.path.join(
                    ctx.get_project_root(), ctx.get_config_storage_base_dir()
                )
            )
        resolved = _resolve_file_path(file_path, base_dir)
        if not os.path.exists(resolved):
            raise PipelineError(
                f"File not found: {resolved}",
                ErrorClass.artifact_not_found,
                recovery_action=(
                    "Verify the file_path exists relative to the run directory."
                ),
                details={"path": resolved},
            )
        with open(resolved, encoding="utf-8") as fh:
            resolved_artifact = json.load(fh)
    else:
        resolved_artifact = artifact

    result = ctx.validate_artifact(ctx.get_schemas(), schema_file, resolved_artifact)

    if result.valid:
        envelope = {
            "status": "ok",
            "data": {"schema": schema_file, "valid": True},
            "next_step": (
                "Call pipeline_store_artifact to persist the validated artifact."
            ),
        }
        return HandlerResponse(json=_dumps(envelope))
    return HandlerResponse(
        json=(
            f"Validation FAILED against {schema_file}:\n"
            + "\n".join(result.errors)
        ),
        is_error=True,
    )


# Guard: prevent artifact types from being stored under a storage_key that
# belongs to a different type, which would cause resolveInputArtifacts to return
# the wrong artifact for downstream phases. Byte-exact port of the TS map.
_STORAGE_KEY_CONFLICTS: dict[str, tuple[str, ...]] = {
    "validation-report": ("specs",),
    "debate-transcript": ("specs",),
    "knowledge-overview": ("specs",),
    "research-manifest": ("specs",),
    "codebase-requirements": ("specs",),
}


def handle_store_artifact(
    args: dict[str, Any], ctx: ArtifactContext
) -> HandlerResponse:
    """Store a validated artifact + register it (Node ``handleStoreArtifact``).

    Run-scoped-vs-legacy routing via ``persist_artifact`` / ``store_artifact``;
    the ``_STORAGE_KEY_CONFLICTS`` guard; ``compute_next_version`` lineage; an
    ``artifact_stored`` event. Returns the ``{status, data, next_step}`` envelope (E).
    """
    state = ctx.require_run()
    storage_key = args.get("storage_key")
    file_name = args.get("file_name")
    artifact = args.get("artifact")
    file_path = args.get("file_path")
    artifact_type = args.get("artifact_type")
    phase = args.get("phase")
    force = args.get("force")
    parent_artifact = args.get("parent_artifact")

    if not storage_key or not file_name or not artifact_type or not phase:
        raise PipelineError(
            "storage_key, file_name, artifact_type, and phase are all required",
            ErrorClass.validation_error,
            recovery_action=(
                "Provide all required parameters: storage_key, file_name, "
                "artifact_type, and phase."
            ),
        )
    if not artifact and not file_path:
        raise PipelineError(
            "Either artifact (inline JSON) or file_path must be provided",
            ErrorClass.validation_error,
            recovery_action=(
                "Provide either an inline artifact object or a file_path to a "
                "JSON file."
            ),
        )

    assert isinstance(storage_key, str)
    assert isinstance(file_name, str)
    assert isinstance(artifact_type, str)
    assert isinstance(phase, str)

    conflicting_keys = _STORAGE_KEY_CONFLICTS.get(artifact_type)
    if conflicting_keys is not None and storage_key in conflicting_keys:
        raise PipelineError(
            f'Cannot store artifact of type "{artifact_type}" under '
            f'storage_key="{storage_key}": '
            f'this directory is reserved for "design-spec" artifacts and '
            f'"{artifact_type}" would shadow them. '
            f'Use a storage_key that matches the artifact type (e.g., "reports" '
            f'for validation-report, '
            f'"overviews" for knowledge-overview, "debates" for debate-transcript).',
            ErrorClass.validation_error,
            recovery_action=(
                f'Choose a storage_key appropriate for "{artifact_type}". '
                f'Canonical keys: validation-report → "reports", '
                f'knowledge-overview → "overviews", '
                f'debate-transcript → "debates", research-manifest → "manifests".'
            ),
            details={"artifact_type": artifact_type, "storage_key": storage_key},
        )

    active_run_dir = ctx.get_active_run_dir()
    base_dir = ctx.base_dir_from_run_dir(active_run_dir)
    sc = ctx.get_storage_config(base_dir)

    content: object
    if file_path:
        assert isinstance(file_path, str)
        resolved = _resolve_file_path(file_path, base_dir)
        if not os.path.exists(resolved):
            raise PipelineError(
                f"File not found: {resolved}",
                ErrorClass.artifact_not_found,
                recovery_action=(
                    "Verify the file_path exists relative to the run directory."
                ),
                details={"path": resolved},
            )
        with open(resolved, encoding="utf-8") as fh:
            content = json.load(fh)
    else:
        content = artifact

    run_data_dir = state.run_data_dir
    subtype = storage_key_to_subtype(storage_key)
    can_use_run_scoped = (
        ctx.persist_artifact is not None
        and isinstance(run_data_dir, str)
        and len(run_data_dir) > 0
        and subtype is not None
    )

    storaged_path: str
    if can_use_run_scoped and ctx.persist_artifact is not None:
        storaged_path = ctx.persist_artifact(storage_key, file_name, content, force)
    else:
        storaged_path = ctx.store_artifact(sc, storage_key, file_name, content, force)

    version = compute_next_version(state.available_artifacts, artifact_type, phase)
    ref = ArtifactRef(
        type=artifact_type,
        path=storaged_path,
        phase=phase,
        created_at=_now_iso(),
        version=version,
    )
    if parent_artifact is not None:
        assert isinstance(parent_artifact, str)
        ref.parent_artifact = parent_artifact
    updated = ctx.add_artifact(state, ref, active_run_dir)
    ctx.set_active_run(updated)

    # Emit artifact_stored audit event after the successful add_artifact call.
    append_event(
        active_run_dir,
        {
            "timestamp": _now_iso(),
            "event": "artifact_stored",
            "phase": phase,
            "run_id": updated.run_id,
            "details": {
                "artifact_type": artifact_type,
                "path": storaged_path,
                "size_bytes": safe_size_bytes(storaged_path),
            },
        },
    )

    data: dict[str, Any] = {
        "path": storaged_path,
        "artifact_type": artifact_type,
        "phase": phase,
        "version": version,
    }
    if parent_artifact is not None:
        data["parent_artifact"] = parent_artifact
    envelope = {
        "status": "ok",
        "data": data,
        "next_step": (
            "Call pipeline_complete_phase when done, or pipeline_store_artifact "
            "for more artifacts."
        ),
    }
    return HandlerResponse(json=_dumps(envelope))


def handle_register_artifact(
    args: dict[str, Any], ctx: ArtifactContext
) -> HandlerResponse:
    """Register an existing on-disk file (Node ``handleRegisterArtifact``).

    Returns a plain JSON object (``{registered, ...}``) (J) on success; a
    plain-text ``"Validation FAILED ..."`` block with ``is_error=True`` (T)
    when ``validate=True`` and validation fails.
    """
    state = ctx.require_run()
    file_path = args.get("file_path")
    artifact_type = args.get("artifact_type")
    phase = args.get("phase")
    storage_key = args.get("storage_key")
    should_validate = args.get("validate")
    if should_validate is None:
        should_validate = False

    if not file_path or not artifact_type or not phase or not storage_key:
        raise PipelineError(
            "file_path, artifact_type, phase, and storage_key are all required",
            ErrorClass.validation_error,
            recovery_action=(
                "Provide all required parameters: file_path, artifact_type, "
                "phase, and storage_key."
            ),
        )

    assert isinstance(file_path, str)
    assert isinstance(artifact_type, str)
    assert isinstance(phase, str)

    active_run_dir = ctx.get_active_run_dir()
    base_dir = ctx.base_dir_from_run_dir(active_run_dir)
    resolved = _resolve_file_path(file_path, base_dir)

    if not os.path.exists(resolved):
        raise PipelineError(
            f"File not found: {resolved}",
            ErrorClass.artifact_not_found,
            recovery_action="Verify the file_path exists and is accessible.",
            details={"path": resolved},
        )

    if should_validate:
        schema_file = f"{artifact_type}.json"
        with open(resolved, encoding="utf-8") as fh:
            content = json.load(fh)
        result = ctx.validate_artifact(ctx.get_schemas(), schema_file, content)
        if not result.valid:
            return HandlerResponse(
                json=(
                    f"Validation FAILED against {schema_file}:\n"
                    + "\n".join(result.errors)
                ),
                is_error=True,
            )

    version = compute_next_version(state.available_artifacts, artifact_type, phase)
    updated = ctx.add_artifact(
        state,
        ArtifactRef(
            type=artifact_type,
            path=resolved,
            phase=phase,
            created_at=_now_iso(),
            version=version,
        ),
        active_run_dir,
    )
    ctx.set_active_run(updated)

    # Emit artifact_stored audit event after the successful add_artifact call.
    append_event(
        active_run_dir,
        {
            "timestamp": _now_iso(),
            "event": "artifact_stored",
            "phase": phase,
            "run_id": updated.run_id,
            "details": {
                "artifact_type": artifact_type,
                "path": resolved,
                "size_bytes": safe_size_bytes(resolved),
            },
        },
    )

    return HandlerResponse(
        json=_dumps(
            {
                "registered": True,
                "artifact_type": artifact_type,
                "path": resolved,
                "phase": phase,
                "version": version,
                "validated": should_validate,
            }
        )
    )


def handle_load_artifact(
    args: dict[str, Any], ctx: ArtifactContext
) -> HandlerResponse:
    """Load a stored artifact: summary / full / fields (Node ``handleLoadArtifact``).

    ``full=true`` (or ``inline=true``) returns the complete artifact through
    :func:`enforce_response_size` (50000-char truncation); ``fields`` returns only
    the requested top-level keys; otherwise a lightweight summary. Reads the
    run-scoped dir first when ``run_data_dir`` is populated, falling back to legacy.
    """
    storage_key = args.get("storage_key")
    file_name = args.get("file_name")
    inline = args.get("inline")
    full_arg = args.get("full")
    full = full_arg if full_arg else inline
    fields = args.get("fields")
    if not storage_key or not file_name:
        raise PipelineError(
            "storage_key and file_name are required",
            ErrorClass.validation_error,
            recovery_action="Provide both storage_key and file_name parameters.",
        )

    assert isinstance(storage_key, str)
    assert isinstance(file_name, str)

    active_run = ctx.get_active_run()
    active_run_dir = ctx.get_active_run_dir()
    if active_run:
        base_dir = ctx.base_dir_from_run_dir(active_run_dir)
    else:
        base_dir = os.path.abspath(
            os.path.join(ctx.get_project_root(), ctx.get_config_storage_base_dir())
        )
    sc = ctx.get_storage_config(base_dir)

    # Feature C: read from the run-scoped directory first when the active run has
    # a populated run_data_dir and the storage_key maps to a known subtype.
    artifact: object | None = None
    run_data_dir = active_run.run_data_dir if active_run else None
    subtype = storage_key_to_subtype(storage_key)
    if (
        isinstance(run_data_dir, str)
        and len(run_data_dir) > 0
        and subtype is not None
    ):
        candidate = os.path.join(get_artifact_dir(run_data_dir, subtype), file_name)
        if os.path.exists(candidate):
            with open(candidate, encoding="utf-8") as fh:
                artifact = json.load(fh)
    if artifact is None:
        artifact = ctx.load_artifact(sc, storage_key, file_name)

    if artifact is None:
        raise PipelineError(
            f"Artifact not found: {storage_key}/{file_name}",
            ErrorClass.artifact_not_found,
            recovery_action=(
                "Call pipeline_list_artifacts to see available artifacts for "
                "this storage key."
            ),
            details={"storage_key": storage_key, "file_name": file_name},
        )

    if full:
        full_json = _dumps(artifact)
        enforced = enforce_response_size(
            full_json,
            50000,
            {"storage_key": storage_key, "file_name": file_name},
        )
        return HandlerResponse(json=enforced)

    if fields is not None and len(fields) > 0:
        obj = artifact if isinstance(artifact, dict) else {}
        filtered: dict[str, object] = {}
        for key in fields:
            if key in obj:
                filtered[key] = obj[key]
        return HandlerResponse(json=_dumps(filtered))

    summary = ctx.build_artifact_summary(artifact, storage_key, file_name)
    return HandlerResponse(
        json=_dumps(
            {
                "artifact_type": summary.artifact_type,
                "id": summary.id,
                "key_count": summary.key_count,
                "total_size_bytes": summary.total_size_bytes,
                "truncated": summary.truncated,
                "preview_keys": summary.preview_keys,
            }
        )
    )


def handle_list_artifacts(
    args: dict[str, Any], ctx: ArtifactContext
) -> HandlerResponse:
    """List artifacts under a storage key (Node ``handleListArtifacts``).

    Lists from the run-scoped directory when ``run_data_dir`` is populated and the
    storage_key maps to a subtype (a missing dir returns ``[]``); otherwise the
    legacy listing. Each entry surfaces ``version`` / ``parent_artifact`` from the
    matching ``available_artifacts`` ref (keyed by the ref's directory == listed
    directory), omitting them when absent. Returns ``{storage_key, artifacts}``.
    """
    storage_key = args.get("storage_key")
    if not storage_key:
        raise PipelineError(
            "storage_key is required",
            ErrorClass.validation_error,
            recovery_action="Provide the storage_key parameter.",
        )

    assert isinstance(storage_key, str)

    active_run = ctx.get_active_run()
    active_run_dir = ctx.get_active_run_dir()
    if active_run:
        base_dir = ctx.base_dir_from_run_dir(active_run_dir)
    else:
        base_dir = os.path.abspath(
            os.path.join(ctx.get_project_root(), ctx.get_config_storage_base_dir())
        )
    sc = ctx.get_storage_config(base_dir)

    run_data_dir = active_run.run_data_dir if active_run else None
    subtype = storage_key_to_subtype(storage_key)
    use_run_scoped = (
        isinstance(run_data_dir, str)
        and len(run_data_dir) > 0
        and subtype is not None
    )

    files: list[str]
    storage_dir: str | None
    if use_run_scoped and isinstance(run_data_dir, str) and subtype is not None:
        run_scoped_dir = get_artifact_dir(run_data_dir, subtype)
        if os.path.exists(run_scoped_dir):
            files = [
                f for f in sorted(os.listdir(run_scoped_dir)) if f.endswith(".json")
            ]
        else:
            files = []
        storage_dir = os.path.normpath(run_scoped_dir)
    else:
        files = ctx.list_artifacts(sc, storage_key)
        sub_path = sc.paths.get(storage_key)
        storage_dir = (
            os.path.normpath(os.path.join(sc.base_dir, sub_path))
            if sub_path is not None
            else None
        )

    available_artifacts = active_run.available_artifacts if active_run else []
    ref_by_file_name: dict[str, ArtifactRef] = {}
    if storage_dir is not None:
        for ref in available_artifacts:
            ref_dir = os.path.normpath(os.path.abspath(os.path.join(ref.path, "..")))
            if ref_dir == storage_dir:
                ref_by_file_name[os.path.basename(ref.path)] = ref

    artifacts: list[dict[str, object]] = []
    for name in files:
        matched_ref = ref_by_file_name.get(name)
        entry: dict[str, object] = {"name": name}
        if matched_ref is not None:
            if matched_ref.version is not None:
                entry["version"] = matched_ref.version
            if matched_ref.parent_artifact is not None:
                entry["parent_artifact"] = matched_ref.parent_artifact
        artifacts.append(entry)

    return HandlerResponse(
        json=_dumps({"storage_key": storage_key, "artifacts": artifacts})
    )


def handle_register_scaffold_outputs(
    args: dict[str, Any], ctx: RegisterScaffoldOutputsContext
) -> HandlerResponse:
    """Scan a scaffold dir and register every regular file as an artifact.

    Port of ``handleRegisterScaffoldOutputs`` (``misc-handlers.ts``). Files named
    ``scaffold-manifest.json`` register as ``scaffold-manifest``; all others as
    ``scaffold-document``. Idempotent -- files whose absolute path already appears
    in ``available_artifacts`` are skipped. Anchor validation for
    ``*.proposed-diff.md`` files is advisory: missing anchors are surfaced through
    both ``data.anchor_warnings`` AND a top-level ``warnings`` array, but
    registration always proceeds. Both surfaces are OMITTED when no anchors are
    missing (undefined-drop parity). Returns the ``{status, data, [warnings],
    next_step}`` envelope (E).
    """
    state = ctx.require_run()
    active_run_dir = ctx.get_active_run_dir()
    base_dir = ctx.base_dir_from_run_dir(active_run_dir)

    scaffold_dir_arg = args.get("scaffold_dir")
    phase_arg = args.get("phase")
    phase = phase_arg if phase_arg is not None else "implementation_scaffold"

    # Resolve scaffold dir: absolute wins, relative resolves against base_dir,
    # omitted/empty defaults to the per-run scaffold dir when run_data_dir is set
    # (Feature C), otherwise the legacy "<base_dir>/scaffold" layout.
    active_run = ctx.get_active_run()
    run_data_dir = active_run.run_data_dir if active_run else None
    default_scaffold_dir = (
        get_artifact_dir(run_data_dir, "scaffold")
        if run_data_dir
        else os.path.join(base_dir, "scaffold")
    )
    scaffold_dir: str
    if scaffold_dir_arg is None or scaffold_dir_arg == "":
        scaffold_dir = default_scaffold_dir
    elif os.path.isabs(scaffold_dir_arg):
        scaffold_dir = scaffold_dir_arg
    else:
        scaffold_dir = os.path.abspath(os.path.join(base_dir, scaffold_dir_arg))

    if not os.path.exists(scaffold_dir) or not os.path.isdir(scaffold_dir):
        raise PipelineError(
            f"Scaffold directory not found: {scaffold_dir}",
            ErrorClass.artifact_not_found,
            recovery_action=(
                "Verify the scaffold_dir argument points to an existing directory "
                "containing scaffold files. By default this tool looks for "
                '"<storage.base_dir>/scaffold".'
            ),
            details={"scaffold_dir": scaffold_dir},
        )

    # Build a set of already-registered absolute paths for O(1) dedup lookup.
    already_registered = {os.path.abspath(a.path) for a in state.available_artifacts}

    entries = sorted(os.listdir(scaffold_dir))
    registered: list[dict[str, object]] = []
    skipped_existing: list[str] = []
    anchor_warnings: list[dict[str, str]] = []

    # Project root is one level above base_dir (the pipeline_mcp_data dir).
    project_root = os.path.abspath(os.path.join(base_dir, ".."))

    working_state = state

    for filename in entries:
        full = os.path.join(scaffold_dir, filename)
        if not os.path.isfile(full):
            continue

        abs_path = os.path.abspath(os.path.join(scaffold_dir, filename))

        if abs_path in already_registered:
            skipped_existing.append(filename)
            continue

        # Anchor validation for proposed-diff files (best-effort, advisory).
        if filename.endswith(".proposed-diff.md"):
            target_filename = filename[: len(filename) - len(".proposed-diff.md")]
            target_path = os.path.join(project_root, target_filename)
            if os.path.exists(target_path) and os.path.isfile(target_path):
                try:
                    with open(abs_path, encoding="utf-8") as fh:
                        diff_content = fh.read()
                    with open(target_path, encoding="utf-8") as fh:
                        target_content = fh.read()
                    anchors = extract_proposed_diff_anchor_refs(diff_content)
                    for anchor in anchors:
                        if not markdown_has_heading(target_content, anchor):
                            anchor_warnings.append(
                                {
                                    "filename": filename,
                                    "target_file": target_filename,
                                    "missing_anchor": anchor,
                                }
                            )
                            print(
                                f"[misc-handlers] scaffold anchor warning: "
                                f'proposed diff "{filename}" references heading '
                                f'"{anchor}" that does not exist in target '
                                f'"{target_filename}"',
                                file=sys.stderr,
                            )
                except OSError as err:
                    print(
                        f"[misc-handlers] unable to validate scaffold anchors "
                        f"for {filename}: {err}",
                        file=sys.stderr,
                    )

        artifact_type = (
            "scaffold-manifest"
            if filename == "scaffold-manifest.json"
            else "scaffold-document"
        )

        version = compute_next_version(
            working_state.available_artifacts, artifact_type, phase
        )

        ref = ArtifactRef(
            type=artifact_type,
            path=abs_path,
            phase=phase,
            created_at=_now_iso(),
            version=version,
        )

        working_state = ctx.add_artifact(working_state, ref, active_run_dir)

        # Emit artifact_stored audit event, mirroring artifact-handlers.ts shape.
        append_event(
            active_run_dir,
            {
                "timestamp": _now_iso(),
                "event": "artifact_stored",
                "phase": phase,
                "run_id": working_state.run_id,
                "details": {
                    "artifact_type": artifact_type,
                    "path": abs_path,
                    "size_bytes": safe_size_bytes(abs_path),
                },
            },
        )

        registered.append(
            {
                "filename": filename,
                "artifact_type": artifact_type,
                "path": abs_path,
                "version": version,
            }
        )
        already_registered.add(abs_path)

    ctx.set_active_run(working_state)

    data: dict[str, Any] = {
        "scaffold_dir": scaffold_dir,
        "registered": registered,
        "skipped_existing": skipped_existing,
    }
    if len(anchor_warnings) > 0:
        data["anchor_warnings"] = anchor_warnings

    envelope: dict[str, Any] = {"status": "ok", "data": data}
    if len(anchor_warnings) > 0:
        envelope["warnings"] = [
            f'Proposed diff "{w["filename"]}" references heading '
            f'"{w["missing_anchor"]}" that does not exist in target '
            f'"{w["target_file"]}"'
            for w in anchor_warnings
        ]
    envelope["next_step"] = (
        "Call pipeline_complete_phase when all scaffold outputs are registered."
    )
    return HandlerResponse(json=_dumps(envelope))


# -- Tool registration (spec sec 5, frozen annotations from tool-schemas.ts) --

_READ_MAX = 50000
_MUTATE_MAX = 10000


def register_artifact_tools(mcp: FastMCP) -> None:
    """Register the 6 artifact/scaffold tools onto ``mcp`` (spec sec 5).

    Per-tool ``_meta.max_result_chars`` + ``ToolAnnotations`` come from the frozen
    ``tool-schemas.ts`` map (byte-exact): read tools
    (``validate_artifact`` / ``load_artifact`` / ``list_artifacts``) carry
    ``readOnlyHint=True`` + 50000; mutating tools
    (``store_artifact`` / ``register_artifact`` / ``register_scaffold_outputs``)
    carry ``destructiveHint=True`` + 10000. The bodies are thin shells; the real
    DI context is wired by the global seam later (T6.5) -- this function only makes
    the tools enumerate correctly on ``tools/list``.

    Wired into ``tools/__init__.py``: the global ``register_tools`` seam fans
    out to this registrar so the tools dispatch through their handlers (T6.7).
    """
    from pipeline_orchestrator import run_state, storage, validator
    from pipeline_orchestrator.server import state

    artifact_ctx = ArtifactContext(
        get_schemas=state.schemas,
        validate_artifact=validator.validate_artifact,
        store_artifact=storage.store_artifact,
        load_artifact=storage.load_artifact,
        list_artifacts=storage.list_artifacts,
        build_artifact_summary=storage.build_artifact_summary,
        get_storage_config=state.get_storage_config,
        base_dir_from_run_dir=state.base_dir_from_run_dir,
        get_project_root=state.project_root,
        get_config_storage_base_dir=lambda: state.config_storage_base_dir(),
        get_active_run=lambda: state.active_run,
        set_active_run=lambda s: setattr(state, "active_run", s),
        get_active_run_dir=lambda: state.active_run_dir,
        require_run=state.require_run,
        add_artifact=run_state.add_artifact,
        persist_artifact=state.persist_artifact,
    )
    scaffold_register_ctx = RegisterScaffoldOutputsContext(
        require_run=state.require_run,
        set_active_run=lambda s: setattr(state, "active_run", s),
        get_active_run=lambda: state.active_run,
        get_active_run_dir=lambda: state.active_run_dir,
        base_dir_from_run_dir=state.base_dir_from_run_dir,
        add_artifact=run_state.add_artifact,
    )

    @mcp.tool(
        name="pipeline_validate_artifact",
        description=(
            "Validate a JSON artifact against one of the pipeline schemas. "
            "Returns validation result with any errors. Provide either "
            '"artifact" (inline JSON) or "file_path" (path to an existing JSON '
            "file on disk)."
        ),
        annotations=ToolAnnotations(readOnlyHint=True),
        meta={"max_result_chars": _READ_MAX},
    )
    @tool_result
    def pipeline_validate_artifact(
        schema: str,
        artifact: dict[str, object] | None = None,
        file_path: str | None = None,
    ) -> HandlerResponse:
        args: dict[str, Any] = {"schema": schema}
        if artifact is not None:
            args["artifact"] = artifact
        if file_path is not None:
            args["file_path"] = file_path
        return handle_validate_artifact(args, artifact_ctx)

    @mcp.tool(
        name="pipeline_store_artifact",
        description=(
            "Store a validated artifact to the correct directory and register it "
            'in run state. Provide either "artifact" (inline JSON) or '
            '"file_path" (path to an existing JSON file on disk). When file_path '
            "is provided the file is copied into the storage directory; when "
            "artifact is provided it is written directly."
        ),
        annotations=ToolAnnotations(destructiveHint=True),
        meta={"max_result_chars": _MUTATE_MAX},
    )
    @tool_result
    def pipeline_store_artifact(
        storage_key: str,
        file_name: str,
        artifact_type: str,
        phase: str,
        artifact: dict[str, object] | None = None,
        file_path: str | None = None,
        force: bool | None = None,
        parent_artifact: str | None = None,
    ) -> HandlerResponse:
        args: dict[str, Any] = {
            "storage_key": storage_key,
            "file_name": file_name,
            "artifact_type": artifact_type,
            "phase": phase,
        }
        if artifact is not None:
            args["artifact"] = artifact
        if file_path is not None:
            args["file_path"] = file_path
        if force is not None:
            args["force"] = force
        if parent_artifact is not None:
            args["parent_artifact"] = parent_artifact
        return handle_store_artifact(args, artifact_ctx)

    @mcp.tool(
        name="pipeline_register_artifact",
        description=(
            "Register an already-existing file on disk as a pipeline artifact "
            "without passing its content inline. Use this for large artifacts "
            "(>256KB) that already exist on disk. The file is verified to exist, "
            "optionally validated against a schema, and registered in the run "
            "state."
        ),
        annotations=ToolAnnotations(destructiveHint=True),
        meta={"max_result_chars": _MUTATE_MAX},
    )
    @tool_result
    def pipeline_register_artifact(
        file_path: str,
        artifact_type: str,
        phase: str,
        storage_key: str,
        validate: bool | None = None,
    ) -> HandlerResponse:
        args: dict[str, Any] = {
            "file_path": file_path,
            "artifact_type": artifact_type,
            "phase": phase,
            "storage_key": storage_key,
        }
        if validate is not None:
            args["validate"] = validate
        return handle_register_artifact(args, artifact_ctx)

    @mcp.tool(
        name="pipeline_register_scaffold_outputs",
        description=(
            "Scan a scaffold directory and register every file as a pipeline "
            'artifact. Files named "scaffold-manifest.json" become '
            '"scaffold-manifest" artifacts; all other files become '
            '"scaffold-document" artifacts. Idempotent: files already registered '
            "in run-state are skipped. Call this once at the end of the "
            "implementation_scaffold phase after writing all scaffold documents."
        ),
        annotations=ToolAnnotations(destructiveHint=True),
        meta={"max_result_chars": _MUTATE_MAX},
    )
    @tool_result
    def pipeline_register_scaffold_outputs(
        scaffold_dir: str | None = None,
        phase: str | None = None,
    ) -> HandlerResponse:
        args: dict[str, Any] = {}
        if scaffold_dir is not None:
            args["scaffold_dir"] = scaffold_dir
        if phase is not None:
            args["phase"] = phase
        return handle_register_scaffold_outputs(args, scaffold_register_ctx)

    @mcp.tool(
        name="pipeline_load_artifact",
        description=(
            "Load a previously stored artifact by storage key and filename. By "
            "default returns metadata and path only (no content). Pass "
            "inline=true (or full=true) to retrieve full artifact content — "
            "caller explicitly opts into the context cost. Pass fields to "
            "retrieve only specific top-level keys."
        ),
        annotations=ToolAnnotations(readOnlyHint=True),
        meta={"max_result_chars": _READ_MAX},
    )
    @tool_result
    def pipeline_load_artifact(
        storage_key: str,
        file_name: str,
        inline: bool | None = None,
        full: bool | None = None,
        fields: list[str] | None = None,
    ) -> HandlerResponse:
        args: dict[str, Any] = {
            "storage_key": storage_key,
            "file_name": file_name,
        }
        if inline is not None:
            args["inline"] = inline
        if full is not None:
            args["full"] = full
        if fields is not None:
            args["fields"] = fields
        return handle_load_artifact(args, artifact_ctx)

    @mcp.tool(
        name="pipeline_list_artifacts",
        description="List all stored artifacts in a given storage path.",
        annotations=ToolAnnotations(readOnlyHint=True),
        meta={"max_result_chars": _READ_MAX},
    )
    @tool_result
    def pipeline_list_artifacts(
        storage_key: str,
    ) -> HandlerResponse:
        args: dict[str, Any] = {"storage_key": storage_key}
        return handle_list_artifacts(args, artifact_ctx)
