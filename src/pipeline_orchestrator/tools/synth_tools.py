"""Synth (design-spec) MCP tool bodies (port of ``legacy-node/synth-handlers.ts``).

The six ``handle_synth_*`` handlers take a :class:`SynthContext` dependency
object (the DI seam that ``server.py`` wires to the real collection / spec
functions) and return a :class:`~pipeline_orchestrator.models.HandlerResponse`.
Every synth handler output is a **plain-text line block** (not JSON) — except
``create_spec``, which returns the synthesis-prompt string verbatim — so the
exact strings are the contract and are preserved byte-for-byte.

:func:`register_synth_tools` registers the six ``pipeline_synth_*`` tools onto a
FastMCP instance with the spec'd ``_meta.max_result_chars`` (50000 read / 10000
mutate, §5.5) and the matching ``ToolAnnotations`` subset —
``pipeline_synth_load_collection`` carries **only** ``_meta`` and **no**
annotations (§5.5, mirroring cc's ``load_collection``). It is NOT called from
anywhere yet; the global ``register_tools`` seam (``tools/__init__.py``) stays a
no-op until the wiring task (T6.5).

Parity notes:

* **``synth_get_items`` returns FULL content always** — unlike
  ``cc_get_items`` (which truncates to 500), the synth handler never truncates
  (TS ``synth-handlers.ts`` has no ``max_content_length`` path).
* **``synth_load_collection`` flat overrides.** The handler reads the five flat
  string args (``items_key`` / ``id_field`` / ``content_field`` /
  ``title_field`` / ``tags_field``) and passes them as a ``Partial<FieldMap>``
  — no nested author/date/url dict (contrast cc's ``field_overrides``).
* **Field-map line** = ``JSON.stringify(col.field_map)`` (compact, no spacing),
  with ``None`` optional keys dropped (TS ``JSON.stringify`` drops ``undefined``
  values) — reproduced by :func:`_field_map_json`.
* **``save_spec`` routing.** Prefers ``ctx.persist_spec`` (Feature-C run-scoped)
  when present, else falls back to ``ctx.save_spec(spec, output_dir ??
  ctx.get_specs_dir())``.
* **``??`` nullish defaults** are ported as explicit ``is None`` checks /
  ``dict.get`` — never ``or`` — so an explicit falsy value survives.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from mcp.types import ToolAnnotations

from pipeline_orchestrator import collections_store, synth
from pipeline_orchestrator.collections_store import FieldMap, ResearchItem
from pipeline_orchestrator.models import DesignSpec, HandlerResponse
from pipeline_orchestrator.synth import (
    _format_metadata_value,
    create_spec_synthesis,
    list_specs,
    save_spec,
)
from pipeline_orchestrator.tools._envelope import tool_result

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP


# ── Context provided by server.py (the DI seam) ──────────────────────


@dataclass
class SynthContext:
    """Dependency object the synth handlers call through (TS ``SynthContext``).

    Each field is a callable injected by ``server.py``. ``persist_spec`` is the
    ``?:`` optional (``None`` when a test fixture omits it);
    ``handle_synth_save_spec`` prefers ``persist_spec`` when present and
    otherwise falls back to ``save_spec`` via ``get_specs_dir``.
    """

    load_collection: Callable[..., object]
    query_items: Callable[..., list[ResearchItem]]
    get_items: Callable[[str, list[str]], list[ResearchItem]]
    get_specs_dir: Callable[[], str]
    persist_spec: Callable[..., str] | None = None


# ── Handlers ─────────────────────────────────────────────────────────


def _field_map_json(fm: FieldMap) -> str:
    """Serialize a :class:`FieldMap` the way TS ``JSON.stringify(col.field_map)`` does.

    Compact (no spacing), keys in TS interface order, ``None`` optionals dropped
    (``JSON.stringify`` omits ``undefined``-valued properties).
    """
    obj: dict[str, object] = {
        "items_key": fm.items_key,
        "id_field": fm.id_field,
        "content_field": fm.content_field,
        "title_field": fm.title_field,
        "tags_field": fm.tags_field,
    }
    if fm.author_field is not None:
        obj["author_field"] = fm.author_field
    if fm.date_field is not None:
        obj["date_field"] = fm.date_field
    if fm.url_field is not None:
        obj["url_field"] = fm.url_field
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def handle_synth_load_collection(
    args: dict[str, object], ctx: SynthContext
) -> HandlerResponse:
    """Load a collection; report field map + tags + first 5 items.

    Port of TS ``handleSynthLoadCollection``.
    """
    file_path = args.get("file_path")
    if not file_path:
        raise ValueError("file_path is required")

    overrides: dict[str, object] = {}
    for key in (
        "items_key",
        "id_field",
        "content_field",
        "title_field",
        "tags_field",
    ):
        if args.get(key):
            overrides[key] = args[key]

    col = ctx.load_collection(file_path, args.get("name"), overrides)

    items = col.items  # type: ignore[attr-defined]
    available_tags = col.available_tags  # type: ignore[attr-defined]

    lines: list[str] = [
        f'Collection "{col.name}" loaded successfully',  # type: ignore[attr-defined]
        f"  Items: {len(items)}",
        f"  Source: {col.file_path}",  # type: ignore[attr-defined]
        f"  Field map: {_field_map_json(col.field_map)}",  # type: ignore[attr-defined]
        "",
        f"  Tags ({len(available_tags)}): {', '.join(available_tags)}",
        "",
        "  First 5 items:",
    ]
    for i in items[:5]:
        tag_suffix = f" ({', '.join(i.tags[:4])})" if len(i.tags) > 0 else ""
        lines.append(f"    [{i.id}] {i.title}{tag_suffix}")
    lines.append(
        f"    ... and {len(items) - 5} more" if len(items) > 5 else ""
    )

    return HandlerResponse(json="\n".join(lines))


def handle_synth_query(args: dict[str, object], ctx: SynthContext) -> HandlerResponse:
    """Query loaded items and render an author/tags preview block.

    Port of TS ``handleSynthQuery``.
    """
    # Nullish-not-falsy limit (TS ``{ limit = 20 }`` defaults only on
    # ``undefined``). An absent ``limit`` must NOT be forwarded — passing an
    # explicit ``None`` into ``query_items(..., limit: int = 20)`` would defeat
    # its default and crash on a non-empty store (``int >= None``). An explicit
    # value (including ``0``) is forwarded unchanged.
    query_kwargs: dict[str, object] = {
        "collection": args.get("collection"),
        "tags": args.get("tags"),
        "search": args.get("search"),
        "fields": args.get("fields"),
    }
    limit = args.get("limit")
    if limit is not None:
        query_kwargs["limit"] = limit
    results = ctx.query_items(**query_kwargs)

    if len(results) == 0:
        return HandlerResponse(json="No matching items found.")

    lines = [f"Found {len(results)} item(s):", ""]
    for item in results:
        lines.append(f"[{item.id}] {item.title}")
        if item.author:
            handle_suffix = (
                f" ({item.author.handle})" if item.author.handle else ""
            )
            lines.append(f"  Author: {item.author.name}{handle_suffix}")
        if len(item.tags) > 0:
            lines.append(f"  Tags: {', '.join(item.tags)}")
        lines.append("")

    return HandlerResponse(json="\n".join(lines))


def handle_synth_get_items(
    args: dict[str, object], ctx: SynthContext
) -> HandlerResponse:
    """Retrieve items by id with FULL content + all metadata.

    Port of TS ``handleSynthGetItems``.

    Unlike ``cc_get_items`` there is NO truncation: the full ``content`` and
    every metadata key/value is emitted.
    """
    collection = args.get("collection")
    item_ids = args.get("item_ids")
    if not collection:
        raise ValueError("collection is required")
    if not item_ids:
        raise ValueError("item_ids is required (non-empty array)")
    assert isinstance(collection, str)
    assert isinstance(item_ids, list)

    items = ctx.get_items(collection, item_ids)

    blocks: list[str] = []
    for item in items:
        parts = [f"=== {item.title} ({item.id}) ==="]
        if item.author:
            handle_suffix = (
                f" ({item.author.handle})" if item.author.handle else ""
            )
            parts.append(f"Author: {item.author.name}{handle_suffix}")
            if item.author.bio:
                parts.append(f"Bio: {item.author.bio}")
        if item.date:
            parts.append(f"Date: {item.date}")
        if item.url:
            parts.append(f"URL: {item.url}")
        if len(item.tags) > 0:
            parts.append(f"Tags: {', '.join(item.tags)}")
        parts.append("")
        parts.append(item.content)
        parts.append("")

        for k, v in item.metadata.items():
            parts.append(f"{k}: {_format_metadata_value(v)}")

        blocks.append("\n".join(parts))

    return HandlerResponse(json="\n\n---\n\n".join(blocks))


def handle_synth_create_spec(
    args: dict[str, object], ctx: SynthContext
) -> HandlerResponse:
    """Build the spec-synthesis prompt from selected items.

    Port of TS ``handleSynthCreateSpec``.

    Groups the raw ``items`` (``[{collection, item_id, relevance?}]``) by
    collection — preserving first-seen collection order and per-collection id
    order — resolves each group's items via ``ctx.get_items``, then returns
    ``create_spec_synthesis(...).synthesis_prompt`` verbatim.
    """
    title = args.get("title")
    if not title:
        raise ValueError("title is required")
    assert isinstance(title, str)

    raw_items = args.get("items")
    if not raw_items:
        raise ValueError("items is required (non-empty array)")
    assert isinstance(raw_items, list)

    # Group by collection (insertion-ordered, mirrors the TS Map).
    by_collection: dict[str, dict[str, object]] = {}
    for entry in raw_items:
        assert isinstance(entry, dict)
        collection = entry.get("collection")
        item_id = entry.get("item_id")
        if not collection or not item_id:
            raise ValueError("Each item must have collection and item_id")
        existing = by_collection.setdefault(
            collection, {"ids": [], "relevance": {}}
        )
        ids = existing["ids"]
        relevance = existing["relevance"]
        assert isinstance(ids, list)
        assert isinstance(relevance, dict)
        ids.append(item_id)
        entry_relevance = entry.get("relevance")
        if entry_relevance:
            relevance[item_id] = entry_relevance

    source_groups: list[dict[str, object]] = []
    for collection_name, group in by_collection.items():
        ids = group["ids"]
        relevance = group["relevance"]
        assert isinstance(ids, list)
        items = ctx.get_items(collection_name, ids)
        source_groups.append(
            {
                "collection": collection_name,
                "items": items,
                "relevance_notes": relevance,
            }
        )

    _template, synthesis_prompt = create_spec_synthesis(
        title,
        source_groups,
        spec_type=args.get("spec_type"),  # type: ignore[arg-type]
        domain=args.get("domain"),  # type: ignore[arg-type]
        focus=args.get("focus"),  # type: ignore[arg-type]
    )

    return HandlerResponse(json=synthesis_prompt)


def handle_synth_save_spec(
    args: dict[str, object], ctx: SynthContext
) -> HandlerResponse:
    """Persist a design spec, preferring run-scoped routing.

    Port of TS ``handleSynthSaveSpec``.

    Prefers ``ctx.persist_spec`` (Feature-C) when present, else
    ``ctx.save_spec(spec, output_dir ?? ctx.get_specs_dir())``; passes the
    ``output_dir`` override through. Validates ``spec`` is present with
    ``spec_id`` + ``title``.
    """
    spec = args.get("spec")
    if not spec:
        raise ValueError("spec is required")
    assert isinstance(spec, DesignSpec)
    if not spec.spec_id or not spec.title:
        raise ValueError("spec must have spec_id and title")

    output_dir_override = args.get("output_dir")
    if ctx.persist_spec is not None:
        file_path = ctx.persist_spec(spec, output_dir_override)
    else:
        output_dir = (
            output_dir_override
            if output_dir_override is not None
            else ctx.get_specs_dir()
        )
        assert isinstance(output_dir, str)
        file_path = save_spec(spec, output_dir)

    return HandlerResponse(
        json="\n".join(
            [
                f"Spec saved: {file_path}",
                f"  Title: {spec.title}",
                f"  Status: {spec.status}",
                f"  Sources: {len(spec.sources)}",
            ]
        )
    )


def handle_synth_list_specs(
    args: dict[str, object], ctx: SynthContext
) -> HandlerResponse:
    """List saved specs as a line block (TS ``handleSynthListSpecs``)."""
    directory = args.get("directory")
    target = directory if directory is not None else ctx.get_specs_dir()
    assert isinstance(target, str)
    specs = list_specs(target)

    if len(specs) == 0:
        return HandlerResponse(json="No saved specs found.")

    lines = [f"{len(specs)} saved spec(s):", ""]
    for s in specs:
        spec_id = str(s["spec_id"])
        lines.append(f"[{spec_id[:8]}] {s['title']}")
        lines.append(
            f"  Type: {s['spec_type']} | Status: {s['status']} | "
            f"Sources: {s['source_count']} | Created: {s['created_date']}"
        )
        lines.append(f"  File: {s['file_path']}")
        lines.append("")

    return HandlerResponse(json="\n".join(lines))


# ── Tool registration (wired into the global register_tools seam) ───

_READ_MAX = 50000
_MUTATE_MAX = 10000


def register_synth_tools(mcp: FastMCP) -> None:
    """Register the six ``pipeline_synth_*`` tools onto ``mcp`` (spec §5.5).

    Each tool carries ``_meta.max_result_chars`` (50000 read / 10000 mutate) and
    the matching ``ToolAnnotations`` subset. ``pipeline_synth_load_collection``
    carries **only** ``_meta`` and **no** annotations (§5.5, mirroring cc's
    ``load_collection``). The bodies are thin shells over the ``handle_synth_*``
    handlers; the real DI context is wired by the global seam later (T6.5) —
    this function only makes the tools enumerate correctly on ``tools/list``.

    Wired into ``tools/__init__.py``: the global ``register_tools`` seam fans
    out to this registrar so the tools dispatch through their handlers (T6.7).
    """

    from pipeline_orchestrator.server import state  # noqa: PLC0415

    def _get_specs_dir() -> str:
        try:
            cfg = state.config()
            paths = cfg.storage.paths if cfg.storage is not None else None
            specs = paths.get("specs", "specs") if paths else "specs"
            base_dir = cfg.storage.base_dir if cfg.storage is not None else ""
            return os.path.realpath(
                os.path.join(state.project_root(), base_dir, specs)
            )
        except Exception:  # noqa: BLE001 — legacy synthCtx.getSpecsDir try/catch
            return os.path.realpath("./specs")

    def _persist_spec(
        spec: DesignSpec, output_dir_override: str | None = None
    ) -> str:
        legacy_base_dir = (
            state.base_dir_from_run_dir(state.active_run_dir)
            if state.active_run is not None
            else os.path.realpath(
                os.path.join(state.project_root(), state.config_storage_base_dir())
            )
        )
        run_data_dir = (
            state.active_run.run_data_dir if state.active_run is not None else None
        )
        return synth.persist_spec(
            run_data_dir, legacy_base_dir, spec, output_dir_override
        )

    synth_ctx = SynthContext(
        load_collection=collections_store.load_collection,
        query_items=collections_store.query_items,
        get_items=collections_store.get_items,
        get_specs_dir=_get_specs_dir,
        persist_spec=_persist_spec,
    )

    @mcp.tool(
        name="pipeline_synth_load_collection",
        description="Load a research collection from a JSON file into the store.",
        meta={"max_result_chars": _MUTATE_MAX},
    )
    @tool_result
    def pipeline_synth_load_collection(
        file_path: str,
        name: str | None = None,
        items_key: str | None = None,
        id_field: str | None = None,
        content_field: str | None = None,
        title_field: str | None = None,
        tags_field: str | None = None,
    ) -> HandlerResponse:
        args: dict[str, object] = {"file_path": file_path}
        if name is not None:
            args["name"] = name
        if items_key is not None:
            args["items_key"] = items_key
        if id_field is not None:
            args["id_field"] = id_field
        if content_field is not None:
            args["content_field"] = content_field
        if title_field is not None:
            args["title_field"] = title_field
        if tags_field is not None:
            args["tags_field"] = tags_field
        return handle_synth_load_collection(args, synth_ctx)

    @mcp.tool(
        name="pipeline_synth_query",
        description="Query loaded research items by collection/tags/search/fields.",
        annotations=ToolAnnotations(readOnlyHint=True),
        meta={"max_result_chars": _READ_MAX},
    )
    @tool_result
    def pipeline_synth_query(
        collection: str | None = None,
        tags: list[str] | None = None,
        search: str | None = None,
        fields: dict[str, str] | None = None,
        limit: int | None = None,
    ) -> HandlerResponse:
        args: dict[str, object] = {}
        if collection is not None:
            args["collection"] = collection
        if tags is not None:
            args["tags"] = tags
        if search is not None:
            args["search"] = search
        if fields is not None:
            args["fields"] = fields
        if limit is not None:
            args["limit"] = limit
        return handle_synth_query(args, synth_ctx)

    @mcp.tool(
        name="pipeline_synth_get_items",
        description="Retrieve full research items by id from a loaded collection.",
        annotations=ToolAnnotations(readOnlyHint=True),
        meta={"max_result_chars": _READ_MAX},
    )
    @tool_result
    def pipeline_synth_get_items(
        collection: str,
        item_ids: list[str],
    ) -> HandlerResponse:
        args: dict[str, object] = {
            "collection": collection,
            "item_ids": item_ids,
        }
        return handle_synth_get_items(args, synth_ctx)

    @mcp.tool(
        name="pipeline_synth_create_spec",
        description="Build a design-spec synthesis prompt from research items.",
        annotations=ToolAnnotations(destructiveHint=True),
        meta={"max_result_chars": _MUTATE_MAX},
    )
    @tool_result
    def pipeline_synth_create_spec(
        title: str,
        items: list[dict[str, object]],
        spec_type: str | None = None,
        domain: str | None = None,
        focus: str | None = None,
    ) -> HandlerResponse:
        args: dict[str, object] = {"title": title, "items": items}
        if spec_type is not None:
            args["spec_type"] = spec_type
        if domain is not None:
            args["domain"] = domain
        if focus is not None:
            args["focus"] = focus
        return handle_synth_create_spec(args, synth_ctx)

    @mcp.tool(
        name="pipeline_synth_save_spec",
        description="Persist a completed DesignSpec to disk.",
        annotations=ToolAnnotations(destructiveHint=True),
        meta={"max_result_chars": _MUTATE_MAX},
    )
    @tool_result
    def pipeline_synth_save_spec(
        spec: dict[str, object],
        output_dir: str | None = None,
    ) -> HandlerResponse:
        args: dict[str, object] = {"spec": spec}
        if output_dir is not None:
            args["output_dir"] = output_dir
        return handle_synth_save_spec(args, synth_ctx)

    @mcp.tool(
        name="pipeline_synth_list_specs",
        description="List saved design specs with summary metadata.",
        annotations=ToolAnnotations(readOnlyHint=True),
        meta={"max_result_chars": _READ_MAX},
    )
    @tool_result
    def pipeline_synth_list_specs(
        directory: str | None = None,
    ) -> HandlerResponse:
        args: dict[str, object] = {}
        if directory is not None:
            args["directory"] = directory
        return handle_synth_list_specs(args, synth_ctx)
