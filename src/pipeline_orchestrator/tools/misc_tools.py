"""Misc / analysis / ingest / feature / web MCP tool bodies (port of the five
non-KB handlers in ``legacy-node/misc-handlers.ts``: ``handleIngestDocuments`` /
``handleAnalyzeCodebase`` / ``handleValidateRun`` / ``handleFeatureRequest`` /
``handleWebSearch``).

The five tool handlers take a small DI context dataclass — the seam ``server.py``
wires to the real schemas / validator / storage / run-state / collection-store
functions — and return a :class:`~pipeline_orchestrator.models.HandlerResponse`.
The KB handlers (``handleKBSearch`` / ``handleKBSQLQuery`` / ``handleKBExportQueryLog``
/ ``handleKBBuildIndex``), ``handleGetConfig`` (lifecycle) and
``handleRegisterScaffoldOutputs`` (artifact) are deliberately NOT in this module —
they belong to other tasks.

Wire format (spec §4.2 / §5.7):

* ``handle_ingest_documents`` (async) -- the ``{status, data, next_step}``
  envelope pretty-JSON (E).
* ``handle_analyze_codebase`` (sync) -- a plain JSON object (J), 2-space indent.
* ``handle_validate_run`` (sync) -- a plain JSON object (J), 2-space indent, with
  ``is_error = not report.valid`` (the ``{run_id, ...report}`` spread).
* ``handle_feature_request`` (sync) -- plain text (T). **Every error is RETURNED
  as ``HandlerResponse(text, is_error=True)``, never raised** (spec §4.2 convention 2).
* ``handle_web_search`` (async) -- the ``{status, data, next_step}`` envelope
  pretty-JSON (E).

Parity notes (same rules as ``artifact_tools.py`` -- the established pattern):

* **``ensure_ascii=False`` on every ``json.dumps``** so non-ASCII survives raw
  (the Node ``JSON.stringify`` emits raw UTF-8). The envelope is serialized
  directly via :func:`_dumps` here rather than via ``ResponseEnvelope.to_json``
  (which omits ``ensure_ascii=False``).
* **Nullish, not falsy.** Every TS ``??`` / ``=== undefined`` / ``!== undefined``
  ports as ``.get(k, default)`` / ``is None`` / ``is not None`` so an explicit
  falsy value (``0`` / ``False`` / ``""`` / ``[]``) survives. The ONE exception is
  ``handleFeatureRequest`` whose guards use TS truthiness (``!title`` / ``||``)
  -- ported verbatim as ``not title`` / ``or`` to keep its error strings exact.
* **undefined-drop.** TS conditional spreads / ``r.source_runs?.map(...) ?? []``
  port to OMITTING the key or defaulting to ``[]`` -- never emitting
  ``null``/``None`` where Node drops the key.
* **Diagnostics -> stderr.** Nothing here writes to stdout (the JSON-RPC channel).
* **smol-toml byte-exact serializer.** :func:`_serialize_feature_requests_toml`
  reproduces ``smol-toml@1.6.1``'s ``stringify({feature_request: entries})`` output
  byte-for-byte for the array-of-tables shape: ``[[feature_request]]`` header,
  ``key = "value"`` per field in insertion order, a single blank line between
  entries, a trailing newline, no trailing blank line. Basic strings only; the
  escape table is the TOML basic-string set (the short escapes
  for ``\\b \\t \\n \\f \\r \\" \\\\``; ``\\uXXXX`` lowercase for the remaining
  C0 controls + ``0x7f``; everything ``>= 0x80`` raw UTF-8). Round-trips cleanly
  through stdlib
  ``tomllib``.

:func:`register_misc_tools` registers the 5 ``pipeline_*`` misc tools with their
per-tool ``_meta.max_result_chars`` + ``ToolAnnotations`` subset (byte-exact from
the frozen ``tool-schemas.ts`` map). It is NOT called from anywhere yet: the
global ``register_tools`` seam (``tools/__init__.py``) stays a no-op until the
wiring task (T6.5), so the global handshake enumerates 0 tools.
"""

from __future__ import annotations

import json
import os
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from mcp.types import ToolAnnotations

from pipeline_orchestrator import validator
from pipeline_orchestrator.models import HandlerResponse, RunState
from pipeline_orchestrator.tools._envelope import tool_result

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP

    from pipeline_orchestrator.cross_ref import RunValidationReport
    from pipeline_orchestrator.storage import StorageConfig
    from pipeline_orchestrator.validator import SchemaMap, ValidationResult


# -- Helpers ---------------------------------------------------------------


def _now_iso() -> str:
    """Return the current UTC time as an ISO 8601 string (Node ``toISOString()``)."""
    now = datetime.now(UTC)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def _dumps(obj: object) -> str:
    """``json.dumps(obj, indent=2)`` with ``ensure_ascii=False`` (raw-UTF-8 parity)."""
    return json.dumps(obj, indent=2, ensure_ascii=False)


FEATURE_REQUESTS_FILE = "feature_requests.toml"


# -- smol-toml byte-exact serializer (for handle_feature_request) ----------

# Short escapes that smol-toml emits for these specific C0 control chars.
_TOML_SHORT_ESCAPES: dict[str, str] = {
    "\b": "\\b",
    "\t": "\\t",
    "\n": "\\n",
    "\f": "\\f",
    "\r": "\\r",
    '"': '\\"',
    "\\": "\\\\",
}


def _toml_escape_basic_string(value: str) -> str:
    """Escape ``value`` as a smol-toml basic string body (no surrounding quotes).

    Reproduces ``smol-toml@1.6.1``'s basic-string escaping byte-for-byte (verified
    against ``stringify`` across the full ``0x00``-``0xFF`` range):

    * ``\\b \\t \\n \\f \\r \\" \\\\`` -> the short escape;
    * any other char in ``0x00``-``0x1f`` or ``0x7f`` -> ``\\uXXXX`` (lowercase
      4-hex, e.g. ``\\u0000`` / ``\\u007f``);
    * everything else (printable ASCII and every codepoint ``>= 0x80``,
      including C1 controls and non-BMP astral chars) -> the raw character.
    """
    out: list[str] = []
    for ch in value:
        short = _TOML_SHORT_ESCAPES.get(ch)
        if short is not None:
            out.append(short)
            continue
        code = ord(ch)
        if code <= 0x1F or code == 0x7F:
            out.append(f"\\u{code:04x}")
        else:
            out.append(ch)
    return "".join(out)


def _serialize_feature_requests_toml(entries: list[dict[str, Any]]) -> str:
    """Serialize ``{feature_request: entries}`` exactly as ``smol-toml.stringify``.

    Emits an array-of-tables: one ``[[feature_request]]`` block per entry, each
    field rendered ``key = "escaped-value"`` in the entry dict's insertion order,
    blocks separated by a single blank line, with a trailing newline and no
    trailing blank line. All values are strings (the feature-request schema has
    only string fields); each is rendered as a basic (double-quoted) string via
    :func:`_toml_escape_basic_string`. Output round-trips through stdlib
    ``tomllib``.
    """
    blocks: list[str] = []
    for entry in entries:
        lines = ["[[feature_request]]"]
        for key, raw_value in entry.items():
            value = raw_value if isinstance(raw_value, str) else str(raw_value)
            lines.append(f'{key} = "{_toml_escape_basic_string(value)}"')
        blocks.append("\n".join(lines))
    # smol-toml joins array-of-table blocks with a blank line and ends with "\n".
    return "\n\n".join(blocks) + "\n"


# -- Context objects provided by server.py (the DI seams) ------------------


@dataclass
class IngestContext:
    """DI object for :func:`handle_ingest_documents` (TS ``IngestContext``).

    ``ingest_documents_async`` / ``enumerate_dir`` / ``persist_raw_collection``
    are imported directly (pure M4 module functions); the ctx supplies only the
    run-state / schema / base-dir seams that ``server.py`` owns.
    """

    validate_artifact: Callable[[SchemaMap, str, object], ValidationResult]
    get_schemas: Callable[[], SchemaMap]
    base_dir_from_run_dir: Callable[[str], str]
    get_project_root: Callable[[], str]
    get_config_storage_base_dir: Callable[[], str]
    get_active_run: Callable[[], RunState | None]
    get_active_run_dir: Callable[[], str]


@dataclass
class ValidateRunContext:
    """DI object for :func:`handle_validate_run` (TS ``ValidateRunContext``)."""

    require_run: Callable[[], RunState]
    get_storage_config: Callable[..., StorageConfig]
    base_dir_from_run_dir: Callable[[str], str]
    get_active_run_dir: Callable[[], str]


@dataclass
class WebSearchContext:
    """DI object for :func:`handle_web_search` (TS ``WebSearchContext`` -- empty)."""


# -- Default phase->outputs map (port verbatim from handleValidateRun) -----

# The TS ``defaultPhaseOutputs`` literal. Overrides merge on top of this.
_DEFAULT_PHASE_OUTPUTS: dict[str, list[str]] = {
    "document_ingestion": ["raw-collection"],
    "research_discovery": ["raw-collection"],
    "curation": ["curated-collection"],
    "concept_extraction": ["knowledge-overview"],
    "codebase_analysis": ["codebase-requirements"],
    "design_synthesis": ["design-spec"],
    "debate": ["debate-transcript"],
    "validation": ["validation-report"],
}


def _validation_report_to_dict(report: RunValidationReport) -> dict[str, Any]:
    """Spread a :class:`RunValidationReport` into the TS field shape + order.

    The TS does ``JSON.stringify({ run_id, ...report })`` where the report's keys
    spread in declaration order: ``valid, cross_ref_issues, semantic_issues,
    completeness_issues, summary, artifacts_checked``. Each issue dataclass maps
    to its TS object literal (field order matches the TS interface).
    """
    return {
        "valid": report.valid,
        "cross_ref_issues": [
            {
                "source_artifact": i.source_artifact,
                "field": i.field,
                "referenced_id": i.referenced_id,
                "expected_type": i.expected_type,
                "message": i.message,
            }
            for i in report.cross_ref_issues
        ],
        "semantic_issues": [
            {"artifact": i.artifact, "rule": i.rule, "message": i.message}
            for i in report.semantic_issues
        ],
        "completeness_issues": [
            {
                "phase": i.phase,
                "missing_type": i.missing_type,
                "message": i.message,
            }
            for i in report.completeness_issues
        ],
        "summary": report.summary,
        "artifacts_checked": report.artifacts_checked,
    }


# -- Handlers --------------------------------------------------------------


async def handle_ingest_documents(
    args: dict[str, Any], ctx: IngestContext
) -> HandlerResponse:
    """Ingest local files into a raw-collection (Node ``handleIngestDocuments``).

    Required ``file_paths`` (non-empty list). Directory entries are expanded via
    ``enumerate_dir`` (plain files pass through; a directory yielding zero
    supported files is silently skipped). Optionally validates the produced
    collection, persists it to disk (run-scoped when ``run_data_dir`` is set,
    legacy fallback otherwise), and returns the ``{status, data, next_step}``
    envelope (E).
    """
    from pipeline_orchestrator.collections_store import persist_raw_collection
    from pipeline_orchestrator.ingest import (
        enumerate_dir,
        ingest_documents_async,
    )

    file_paths = args.get("file_paths")
    if not isinstance(file_paths, list) or len(file_paths) == 0:
        raise ValueError("file_paths must be a non-empty array of file path strings")

    ingest_name_arg = args.get("ingest_name")
    ingest_name = ingest_name_arg if ingest_name_arg is not None else "ingest"
    should_validate_arg = args.get("validate")
    should_validate = should_validate_arg if should_validate_arg is not None else True
    json_items_key = args.get("json_items_key")

    # Expand directory entries into their supported files; plain-file entries
    # pass through unchanged (Phase 3 — SP3-P3).
    expanded_paths: list[str] = []
    for entry in file_paths:
        is_dir = False
        try:
            is_dir = os.path.isdir(entry) and os.path.exists(entry)
        except OSError:
            # os.stat throws for non-existent paths; ingest will surface the error.
            is_dir = False
        if is_dir:
            expanded_paths.extend(enumerate_dir(entry))
        else:
            expanded_paths.append(entry)

    collection = await ingest_documents_async(
        expanded_paths, ingest_name, json_items_key
    )

    if should_validate:
        result = ctx.validate_artifact(
            ctx.get_schemas(), "raw-collection.json", collection
        )
        if not result.valid:
            raise ValueError(
                "Produced collection failed schema validation:\n"
                + "\n".join(result.errors)
            )

    # Write collection to disk first — keeps the MCP response under 10KB.
    # Feature C: route through run_data_dir when set, legacy fallback otherwise.
    active_run = ctx.get_active_run()
    if active_run is not None:
        legacy_base_dir = ctx.base_dir_from_run_dir(ctx.get_active_run_dir())
        run_data_dir = active_run.run_data_dir
    else:
        legacy_base_dir = os.path.abspath(
            os.path.join(ctx.get_project_root(), ctx.get_config_storage_base_dir())
        )
        run_data_dir = None

    collection_id = collection["collection_id"]
    assert isinstance(collection_id, str)
    items = collection["items"]
    assert isinstance(items, list)

    artifact_path = persist_raw_collection(
        run_data_dir,
        legacy_base_dir,
        collection,
        f"{collection_id}.json",
    )

    # ``collection.source_runs?.map(r => r.path).filter(...) ?? []`` -- nullish
    # default to [], keep only string paths.
    source_runs = collection.get("source_runs")
    sources: list[str] = []
    if isinstance(source_runs, list):
        for r in source_runs:
            if isinstance(r, dict):
                path = r.get("path")
                if isinstance(path, str):
                    sources.append(path)

    envelope = {
        "status": "ok",
        "data": {
            "collection_id": collection_id,
            "item_count": len(items),
            "sources": sources,
            "artifact_path": artifact_path,
        },
        "next_step": (
            f'Call pipeline_register_artifact with file_path="{artifact_path}" '
            "to register this collection."
        ),
    }
    return HandlerResponse(json=_dumps(envelope))


def handle_analyze_codebase(args: dict[str, Any]) -> HandlerResponse:
    """Analyze a package directory (Node ``handleAnalyzeCodebase``).

    Required ``codebase_path``. Wraps ``codebase_analyzer.analyze_codebase`` and
    returns the result as a plain JSON object (J), 2-space indent.
    """
    from pipeline_orchestrator.codebase_analyzer import analyze_codebase

    codebase_path = args.get("codebase_path")
    if not codebase_path:
        raise ValueError("codebase_path is required")
    assert isinstance(codebase_path, str)
    result = analyze_codebase(codebase_path)
    return HandlerResponse(json=_dumps(result))


def handle_validate_run(
    args: dict[str, Any], ctx: ValidateRunContext
) -> HandlerResponse:
    """Run cross-ref / semantic / completeness validation (Node ``handleValidateRun``).

    Merges optional ``phase_output_overrides`` on top of the default
    phase->outputs map, runs ``cross_ref.validate_run``, and returns the
    ``{run_id, ...report}`` object as plain JSON (J) with
    ``is_error = not report.valid``.
    """
    from pipeline_orchestrator.cross_ref import validate_run

    state = ctx.require_run()
    base_dir = ctx.base_dir_from_run_dir(ctx.get_active_run_dir())
    sc = ctx.get_storage_config(base_dir)

    overrides_arg = args.get("phase_output_overrides")
    overrides = overrides_arg if overrides_arg is not None else {}
    assert isinstance(overrides, dict)
    phase_output_map = {**_DEFAULT_PHASE_OUTPUTS, **overrides}

    report = validate_run(state, sc, phase_output_map)

    payload = {"run_id": state.run_id, **_validation_report_to_dict(report)}
    return HandlerResponse(json=_dumps(payload), is_error=not report.valid)


def handle_feature_request(args: dict[str, Any]) -> HandlerResponse:
    """Record a feature suggestion into a TOML file (Node ``handleFeatureRequest``).

    Output style **T** (plain text). **Every error is RETURNED as
    ``HandlerResponse(text, is_error=True)`` -- never raised** (spec §4.2
    convention 2). Guards use TS truthiness verbatim (``!title``, ``||``) to keep
    the error strings byte-exact. Appends to an existing file (parsing via
    ``tomllib``) or creates a new one, serializing with the smol-toml byte-exact
    serializer.
    """
    import tomllib

    target_dir = args.get("target_directory")
    if not target_dir:
        return HandlerResponse(
            json="Error: target_directory is required", is_error=True
        )
    assert isinstance(target_dir, str)

    # TS ``resolve(targetDir)`` -> cwd-relative absolute. Python ``abspath`` matches.
    dir_path = os.path.abspath(target_dir)
    if not os.path.exists(dir_path) or not os.path.isdir(dir_path):
        return HandlerResponse(
            json=(
                "Error: target_directory does not exist or is not a directory: "
                f"{dir_path}"
            ),
            is_error=True,
        )

    title = args.get("title")
    description = args.get("description")
    priority = args.get("priority")
    scope = args.get("scope")
    rationale = args.get("rationale")
    # TS ``(args.source as string) || 'agent'`` -- falsy OR-default (not nullish).
    source = args.get("source") or "agent"

    # TS ``!title || !description || ...`` -- truthiness guard ported verbatim.
    if not title or not description or not priority or not scope or not rationale:
        return HandlerResponse(
            json=(
                "Error: title, description, priority, scope, and rationale "
                "are all required"
            ),
            is_error=True,
        )
    if priority not in ("low", "medium", "high"):
        return HandlerResponse(
            json=(
                f'Error: priority must be "low", "medium", or "high" — '
                f'got "{priority}"'
            ),
            is_error=True,
        )

    assert isinstance(title, str)
    assert isinstance(description, str)
    assert isinstance(priority, str)
    assert isinstance(scope, str)
    assert isinstance(rationale, str)
    assert isinstance(source, str)

    # id: fr-<YYYYMMDD>-<6 hex chars> (Node date.slice(0,10).replace(/-/g,'') +
    # randomBytes(3).toString('hex')).
    date_part = datetime.now(UTC).strftime("%Y%m%d")
    hex_part = secrets.token_hex(3)
    fr_id = f"fr-{date_part}-{hex_part}"

    entry: dict[str, Any] = {
        "id": fr_id,
        "title": title,
        "description": description,
        "priority": priority,
        "scope": scope,
        "rationale": rationale,
        "status": "proposed",
        "timestamp": _now_iso(),
        "source": source,
    }

    file_path = os.path.join(dir_path, FEATURE_REQUESTS_FILE)

    # Read existing entries if the file exists.
    entries: list[dict[str, Any]] = []
    if os.path.exists(file_path):
        with open(file_path, encoding="utf-8") as fh:
            raw = fh.read()
        try:
            parsed = tomllib.loads(raw)
            existing = parsed.get("feature_request")
            if isinstance(existing, list):
                entries = existing
        except (tomllib.TOMLDecodeError, ValueError) as e:
            return HandlerResponse(
                json=(
                    f"Error: existing {FEATURE_REQUESTS_FILE} in {dir_path} is "
                    "malformed. Fix or remove it before adding new entries.\n"
                    f"  {e}"
                ),
                is_error=True,
            )

    entries.append(entry)
    toml = _serialize_feature_requests_toml(entries)
    with open(file_path, "w", encoding="utf-8") as fh:
        fh.write(toml)

    return HandlerResponse(
        json="\n".join(
            [
                "Feature request recorded.",
                f"  ID: {fr_id}",
                f"  Title: {title}",
                f"  Priority: {priority}",
                f"  File: {file_path}",
            ]
        )
    )


async def handle_web_search(
    args: dict[str, Any], _ctx: WebSearchContext
) -> HandlerResponse:
    """Search the web and/or fetch + merge URLs (Node ``handleWebSearch``).

    Required ``collection_path`` plus at least one of ``queries`` / ``fetch_urls``.
    Validates the collection file (existence + parseable JSON) when fetch mode
    will write to it. Wraps ``web_search.execute_web_search`` and returns the
    ``{status, data, next_step}`` envelope (E).
    """
    from pipeline_orchestrator.web_search import execute_web_search

    collection_path = args.get("collection_path")
    if not collection_path:
        raise ValueError("collection_path is required")
    assert isinstance(collection_path, str)

    queries = args.get("queries")
    fetch_urls = args.get("fetch_urls")
    max_results_per_query = args.get("max_results_per_query")

    # ``!queries?.length && !fetchUrls?.length`` -- neither provided/non-empty.
    queries_empty = not queries or len(queries) == 0
    fetch_empty = not fetch_urls or len(fetch_urls) == 0
    if queries_empty and fetch_empty:
        raise ValueError("Either queries or fetch_urls (or both) must be provided.")

    # Validate collection_path exists + is parseable JSON when fetch mode writes.
    if fetch_urls and len(fetch_urls) > 0:
        if not os.path.exists(collection_path):
            raise ValueError(f"Collection file not found: {collection_path}")
        try:
            with open(collection_path, encoding="utf-8") as fh:
                json.load(fh)
        except (OSError, ValueError):
            raise ValueError(
                f"Collection file is not valid JSON: {collection_path}"
            ) from None

    params: dict[str, Any] = {"collection_path": collection_path}
    if queries is not None:
        params["queries"] = queries
    if fetch_urls is not None:
        params["fetch_urls"] = fetch_urls
    if max_results_per_query is not None:
        params["max_results_per_query"] = max_results_per_query

    result = await execute_web_search(params)
    data = result["data"]
    next_step = result["next_step"]

    envelope = {"status": "ok", "data": data, "next_step": next_step}
    return HandlerResponse(json=_dumps(envelope))


# -- Tool registration (spec §5.7, frozen annotations from tool-schemas.ts) --

_READ_MAX = 50000
_MUTATE_MAX = 10000


def register_misc_tools(mcp: FastMCP) -> None:
    """Register the 5 misc tools onto ``mcp`` (spec §5.7).

    Per-tool ``_meta.max_result_chars`` + ``ToolAnnotations`` come from the frozen
    ``tool-schemas.ts`` map (byte-exact): ``ingest_documents`` / ``analyze_codebase``
    / ``feature_request`` / ``web_search`` carry ``destructiveHint=True``;
    ``validate_run`` carries ``readOnlyHint=True``. ``validate_run`` carries 50000;
    the other four carry 10000. ``ingest_documents`` and ``web_search`` are the two
    async tools (§4.4). The bodies are thin shells; the real DI context is wired by
    the global seam later (T6.5) -- this function only makes the tools enumerate
    correctly on ``tools/list``.

    Wired into ``tools/__init__.py``: the global ``register_tools`` seam fans
    out to this registrar so the tools dispatch through their handlers (T6.7).
    """
    from pipeline_orchestrator.server import state

    ingest_ctx = IngestContext(
        validate_artifact=validator.validate_artifact,
        get_schemas=state.schemas,
        base_dir_from_run_dir=state.base_dir_from_run_dir,
        get_project_root=state.project_root,
        get_config_storage_base_dir=lambda: state.config_storage_base_dir(),
        get_active_run=lambda: state.active_run,
        get_active_run_dir=lambda: state.active_run_dir,
    )
    validate_run_ctx = ValidateRunContext(
        require_run=state.require_run,
        get_storage_config=state.get_storage_config,
        base_dir_from_run_dir=state.base_dir_from_run_dir,
        get_active_run_dir=lambda: state.active_run_dir,
    )
    web_search_ctx = WebSearchContext()


    @mcp.tool(
        name="pipeline_ingest_documents",
        description=(
            "Ingest local files (PDF, Markdown, text, CSV, HTML, JSON, YAML, "
            "TOML, Jupyter notebooks) into a raw-collection artifact. JSON files "
            "containing arrays are automatically split into individual items. "
            "Returns the raw-collection JSON. Use pipeline_store_artifact "
            "afterwards to persist it. Supports PDF via pdf-parse. All other "
            "types use built-in Node.js APIs."
        ),
        annotations=ToolAnnotations(destructiveHint=True),
        meta={"max_result_chars": _MUTATE_MAX},
    )
    @tool_result
    async def pipeline_ingest_documents(
        file_paths: list[str],
        ingest_name: str | None = None,
        validate: bool | None = None,
        json_items_key: str | None = None,
    ) -> HandlerResponse:
        args: dict[str, Any] = {"file_paths": file_paths}
        if ingest_name is not None:
            args["ingest_name"] = ingest_name
        if validate is not None:
            args["validate"] = validate
        if json_items_key is not None:
            args["json_items_key"] = json_items_key
        return await handle_ingest_documents(args, ingest_ctx)

    @mcp.tool(
        name="pipeline_analyze_codebase",
        description=(
            "Analyze a package directory to produce a codebase requirements "
            "artifact."
        ),
        annotations=ToolAnnotations(destructiveHint=True),
        meta={"max_result_chars": _MUTATE_MAX},
    )
    @tool_result
    def pipeline_analyze_codebase(
        codebase_path: str,
    ) -> HandlerResponse:
        args: dict[str, Any] = {"codebase_path": codebase_path}
        return handle_analyze_codebase(args)

    @mcp.tool(
        name="pipeline_validate_run",
        description=(
            "Run cross-reference, semantic, and completeness validation on the "
            "active pipeline run. Checks that artifact ID references resolve, "
            "enforces consistency rules, and verifies completed phases produced "
            "expected outputs. Returns a structured report."
        ),
        annotations=ToolAnnotations(readOnlyHint=True),
        meta={"max_result_chars": _READ_MAX},
    )
    @tool_result
    def pipeline_validate_run(
        phase_output_overrides: dict[str, list[str]] | None = None,
    ) -> HandlerResponse:
        args: dict[str, Any] = {}
        if phase_output_overrides is not None:
            args["phase_output_overrides"] = phase_output_overrides
        return handle_validate_run(args, validate_run_ctx)

    @mcp.tool(
        name="pipeline_feature_request",
        description=(
            "Record a feature suggestion into a feature_requests.toml file at a "
            "specified directory. Appends to an existing file or creates a new "
            "one. Use this to capture improvement ideas while working in the "
            "codebase or pipeline. Reviewing agents can later read these files "
            "when designing update specs."
        ),
        annotations=ToolAnnotations(destructiveHint=True),
        meta={"max_result_chars": _MUTATE_MAX},
    )
    @tool_result
    def pipeline_feature_request(
        target_directory: str,
        title: str,
        description: str,
        priority: str,
        scope: str,
        rationale: str,
        source: str | None = None,
    ) -> HandlerResponse:
        args: dict[str, Any] = {
            "target_directory": target_directory,
            "title": title,
            "description": description,
            "priority": priority,
            "scope": scope,
            "rationale": rationale,
        }
        if source is not None:
            args["source"] = source
        return handle_feature_request(args)

    @mcp.tool(
        name="pipeline_web_search",
        description=(
            "Search the web for additional resources and optionally fetch and "
            "merge content into an existing raw-collection. Two modes: (1) "
            "provide queries to get search result snippets for review; (2) "
            "provide fetch_urls to fetch pages and merge extracted text into the "
            "collection. Both may be combined in a single call — searches first, "
            "then fetches."
        ),
        annotations=ToolAnnotations(destructiveHint=True),
        meta={"max_result_chars": _MUTATE_MAX},
    )
    @tool_result
    async def pipeline_web_search(
        collection_path: str,
        queries: list[str] | None = None,
        max_results_per_query: float | None = None,
        fetch_urls: list[str] | None = None,
    ) -> HandlerResponse:
        args: dict[str, Any] = {"collection_path": collection_path}
        if queries is not None:
            args["queries"] = queries
        if max_results_per_query is not None:
            args["max_results_per_query"] = max_results_per_query
        if fetch_urls is not None:
            args["fetch_urls"] = fetch_urls
        return await handle_web_search(args, web_search_ctx)


