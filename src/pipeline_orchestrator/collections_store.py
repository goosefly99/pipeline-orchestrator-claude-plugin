"""Shared in-memory research-collection store (port of ``legacy-node/collections.ts``).

Byte-exact behavioral port of the Node ``collections.ts`` module. Holds the
single module-level :data:`STORE` mapping (mirrors the TS ``const store = new
Map``) that both the cc and synth domains read/write, plus the auto-detection,
normalization, query, and per-run persistence helpers.

Parity notes:

* **Nullish-coalescing.** Every TS ``?? default`` becomes an explicit
  ``is None`` check (via :func:`_coalesce`) or ``dict.get(k, default)``, never
  ``or`` — a present-but-falsy value (e.g. an empty-string field) must survive
  rather than fall through to a candidate fallback.
* **JS ``String(x)`` coercion.** ``normalizeItem`` does ``String(get(...) ??
  default)``; :func:`_js_string` reproduces the JS ``String()`` rules for the
  values that can appear (``None`` cannot reach it because the ``??`` default is
  applied first, but booleans/numbers/arrays/objects coerce the JS way).
* **Path construction** is OS-native (``os.path.join`` / ``os.path.basename`` /
  ``os.path.abspath``), matching Node ``path.join`` / ``basename`` / ``resolve``.
* **JSON serialization** of persisted collections mirrors Node
  ``JSON.stringify(collection, null, 2)`` (``json.dumps(collection, indent=2,
  ensure_ascii=False)`` — ``ensure_ascii=False`` keeps non-ASCII raw, matching
  Node's UTF-8 output rather than Python's default ASCII-escaped output).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

from pipeline_orchestrator.storage import get_artifact_dir

# ── Type shapes (port of the three ``cc-types.ts`` interfaces in scope) ──


@dataclass
class FieldMap:
    """Field mapping for auto-detecting a collection's JSON schema.

    Mirrors the TS ``FieldMap`` interface: ``author_field`` / ``date_field`` /
    ``url_field`` are the ``?:`` optionals defaulting to ``None``.
    """

    items_key: str
    id_field: str
    content_field: str
    title_field: str
    tags_field: str
    author_field: str | None = None
    date_field: str | None = None
    url_field: str | None = None


@dataclass
class Author:
    """Normalized author (mirrors the inline TS ``{ name; handle?; bio? }``)."""

    name: str
    handle: str | None = None
    bio: str | None = None


@dataclass
class ResearchItem:
    """A normalized research item (mirrors the TS ``ResearchItem`` interface)."""

    id: str
    title: str
    content: str
    tags: list[str]
    url: str | None = None
    date: str | None = None
    author: Author | None = None
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass
class ResearchCollection:
    """A loaded research collection (mirrors the TS ``ResearchCollection``)."""

    name: str
    file_path: str
    items: list[ResearchItem]
    field_map: FieldMap
    available_tags: list[str]


# ── Shared module-level store ───────────────────────────────────────
#
# One dict, shared across the cc and synth domains (TS ``const store = new
# Map<string, ResearchCollection>()``). Do not split the namespace.

STORE: dict[str, ResearchCollection] = {}


def _store_max_items() -> int | None:
    """Opt-in STORE cap: ``PIPELINE_MCP_STORE_MAX_ITEMS`` (total normalized
    items across all loaded collections).

    Unset / unparseable / ``<= 0`` → ``None`` (unlimited — the byte-exact
    legacy behavior). Read from ``os.environ`` on every call (the ``@cache``'d
    ``config.py`` readers would go stale when tests toggle the env per-test).
    """
    raw = os.environ.get("PIPELINE_MCP_STORE_MAX_ITEMS")
    if not raw:
        return None
    try:
        cap = int(raw)
    except ValueError:
        return None
    return cap if cap > 0 else None


def _evict_over_cap(max_items: int, keep: str) -> None:
    """FIFO-evict whole oldest-loaded collections until :data:`STORE` holds at
    most ``max_items`` items in total.

    ``keep`` (the just-loaded collection) is never evicted, so it survives even
    when it alone exceeds the cap.
    """
    total = sum(len(c.items) for c in STORE.values())
    while total > max_items:
        oldest = next(iter(STORE))
        if oldest == keep:
            break
        total -= len(STORE[oldest].items)
        del STORE[oldest]


# ── Candidate lists for auto-detection (ported VERBATIM, same order) ─

ID_CANDIDATES = ["id", "tweet_id", "post_id", "article_id", "item_id", "uid", "key"]
CONTENT_CANDIDATES = [
    "content",
    "body",
    "text",
    "description",
    "full_text",
    "article_text",
]
TITLE_CANDIDATES = ["summary", "title", "name", "headline", "subject"]
TAGS_CANDIDATES = ["tags", "categories", "labels", "keywords", "topics"]
AUTHOR_CANDIDATES = ["profile", "author", "user", "creator", "poster"]
DATE_CANDIDATES = [
    "date",
    "created_at",
    "published",
    "timestamp",
    "published_at",
    "created",
]
URL_CANDIDATES = ["url", "link", "href", "source_url", "permalink"]


# ── Helpers ─────────────────────────────────────────────────────────


def _coalesce(*values: object) -> object:
    """Return the first value that is not ``None`` (JS ``a ?? b ?? ...``).

    Unlike ``or``, a present-but-falsy value (``""``, ``0``, ``False``, ``[]``)
    is returned as-is; only ``None`` (JS ``null``/``undefined``) is skipped.
    Returns ``None`` when every value is ``None``.
    """
    for v in values:
        if v is not None:
            return v
    return None


def _js_string(value: object) -> str:
    """Coerce ``value`` the way JS ``String(value)`` does.

    Differs from Python ``str`` for the cross-language cases: ``True``→``"true"``,
    ``False``→``"false"``, ``None``→``"null"`` (cannot occur here because the
    ``?? default`` is applied first), and whole floats render without the
    trailing ``.0`` (``2.0``→``"2"``). Lists/dicts coerce via JSON-ish rules but
    do not arise for id/title/content in practice; we fall back to JSON for them.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))
        return repr(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return ",".join(_js_string(v) if v is not None else "" for v in value)
    if isinstance(value, dict):
        return "[object Object]"
    return str(value)


def _find_field(
    sample: dict[str, object], candidates: list[str]
) -> str | None:
    """Return the first candidate key present in ``sample`` (TS ``findField``)."""
    for c in candidates:
        if c in sample:
            return c
    return None


def _find_id_field(sample: dict[str, object]) -> str:
    """Resolve the id field (TS ``findIdField``).

    First candidate present, else the first key ending in ``"_id"``, else
    ``"id"``.
    """
    direct = _find_field(sample, ID_CANDIDATES)
    if direct:
        return direct
    suffixed = next((k for k in sample.keys() if k.endswith("_id")), None)
    return suffixed if suffixed is not None else "id"


# ── Auto-detect field mapping ───────────────────────────────────────


def detect_field_map(
    data: dict[str, object],
    overrides: dict[str, object] | None = None,
) -> FieldMap:
    """Auto-detect the :class:`FieldMap` for ``data`` (TS ``detectFieldMap``).

    ``overrides`` is a partial mapping (TS ``Partial<FieldMap>``); a present
    override wins over the auto-detected value. Raises ``ValueError`` (TS throws
    ``Error``) when no non-empty items array is found.
    """
    ov: dict[str, object] = overrides or {}

    # Find the items array.
    items_key = "items"
    if not isinstance(data.get(items_key), list):
        array_key = next(
            (k for k in data.keys() if isinstance(data[k], list)), None
        )
        if array_key:
            items_key = array_key

    items = data.get(items_key)
    if not isinstance(items, list) or len(items) == 0:
        raise ValueError(f'No items array found in data (tried key "{items_key}")')

    sample: dict[str, object] = items[0]

    return FieldMap(
        items_key=_str(_coalesce(ov.get("items_key"), items_key)),
        id_field=_str(_coalesce(ov.get("id_field"), _find_id_field(sample))),
        content_field=_str(
            _coalesce(
                ov.get("content_field"),
                _find_field(sample, CONTENT_CANDIDATES),
                "content",
            )
        ),
        title_field=_str(
            _coalesce(
                ov.get("title_field"),
                _find_field(sample, TITLE_CANDIDATES),
                "title",
            )
        ),
        tags_field=_str(
            _coalesce(
                ov.get("tags_field"),
                _find_field(sample, TAGS_CANDIDATES),
                "tags",
            )
        ),
        author_field=_opt_str(
            _coalesce(ov.get("author_field"), _find_field(sample, AUTHOR_CANDIDATES))
        ),
        date_field=_opt_str(
            _coalesce(ov.get("date_field"), _find_field(sample, DATE_CANDIDATES))
        ),
        url_field=_opt_str(
            _coalesce(ov.get("url_field"), _find_field(sample, URL_CANDIDATES))
        ),
    )


def _str(value: object) -> str:
    """Narrow a known-string coalesce result to ``str`` for mypy."""
    assert isinstance(value, str)
    return value


def _opt_str(value: object) -> str | None:
    """Narrow an optional-string coalesce result for mypy."""
    if value is None:
        return None
    assert isinstance(value, str)
    return value


# ── Normalize a raw item ────────────────────────────────────────────


def normalize_item(
    raw: dict[str, object],
    fm: FieldMap,
    index: int,
) -> ResearchItem:
    """Normalize one raw item against ``fm`` (TS ``normalizeItem``)."""

    def get(field_name: str | None) -> object:
        return raw[field_name] if field_name and field_name in raw else None

    # Author can be a string or an object with name/handle/bio.
    author: Author | None = None
    raw_author = get(fm.author_field)
    if isinstance(raw_author, str):
        author = Author(name=raw_author)
    elif raw_author is not None and not isinstance(raw_author, str):
        # JS ``typeof x === 'object'`` matches ANY non-null object — arrays and
        # empty ``{}`` included (both truthy). Read keys only when it is a real
        # mapping; for a non-mapping object (e.g. a list) the keys are absent, so
        # ``a.display_name`` etc. are all ``undefined`` and the name falls
        # through to ``'Unknown'`` (matching the TS).
        a = raw_author if isinstance(raw_author, dict) else {}
        name = _coalesce(
            a.get("display_name"),
            a.get("name"),
            a.get("username"),
            a.get("handle"),
            "Unknown",
        )
        handle = _coalesce(a.get("handle"), a.get("username"))
        author = Author(
            name=_str(name),
            handle=_opt_str(handle),
            bio=_opt_str(a.get("bio")),
        )

    # Collect all remaining fields as metadata.
    known_fields = {
        f
        for f in (
            fm.id_field,
            fm.content_field,
            fm.title_field,
            fm.tags_field,
            fm.author_field,
            fm.date_field,
            fm.url_field,
        )
        if f
    }

    metadata: dict[str, object] = {}
    for k, v in raw.items():
        if k not in known_fields:
            metadata[k] = v

    raw_tags = get(fm.tags_field)
    tags: list[str] = raw_tags if isinstance(raw_tags, list) else []

    return ResearchItem(
        id=_js_string(_coalesce(get(fm.id_field), f"item_{index}")),
        title=_js_string(_coalesce(get(fm.title_field), "")),
        content=_js_string(_coalesce(get(fm.content_field), "")),
        url=_opt_str(get(fm.url_field)),
        date=_opt_str(get(fm.date_field)),
        author=author,
        tags=tags,
        metadata=metadata,
    )


# ── Public API ──────────────────────────────────────────────────────


def load_collection(
    file_path: str,
    name: str | None = None,
    field_overrides: dict[str, object] | None = None,
) -> ResearchCollection:
    """Load, normalize, and store a collection from a JSON file.

    Port of TS ``loadCollection``: resolves the path to absolute, JSON-parses a
    single object, derives the name from the basename (sans ``.json``) when not
    provided, collects the sorted unique tag set, stores into :data:`STORE`, and
    returns the collection.
    """
    abs_path = os.path.abspath(file_path)
    with open(abs_path, encoding="utf-8") as fh:
        raw: dict[str, object] = json.load(fh)
    collection_name = name if name is not None else _basename_no_json(abs_path)

    fm = detect_field_map(raw, field_overrides)
    raw_items = raw[fm.items_key]
    assert isinstance(raw_items, list)
    items = [normalize_item(item, fm, i) for i, item in enumerate(raw_items)]

    # Collect all unique tags.
    tag_set: set[str] = set()
    for item in items:
        for tag in item.tags:
            tag_set.add(tag)

    collection = ResearchCollection(
        name=collection_name,
        file_path=abs_path,
        items=items,
        field_map=fm,
        available_tags=sorted(tag_set),
    )

    max_items = _store_max_items()
    if max_items is None:
        # Knob unset → byte-exact legacy behavior (a reload keeps the
        # collection's original insertion position in the shared dict).
        STORE[collection_name] = collection
    else:
        # Opt-in cap: a reload moves the collection to the back of the FIFO
        # eviction queue, then whole oldest-loaded collections are evicted
        # while the total item count exceeds the cap.
        STORE.pop(collection_name, None)
        STORE[collection_name] = collection
        _evict_over_cap(max_items, keep=collection_name)
    return collection


def _basename_no_json(path: str) -> str:
    """``path.basename(p, '.json')`` — strip a trailing ``.json`` extension only."""
    base = os.path.basename(path)
    if base.endswith(".json"):
        return base[: -len(".json")]
    return base


def query_items(
    collection: str | None = None,
    tags: list[str] | None = None,
    search: str | None = None,
    fields: dict[str, str] | None = None,
    limit: int = 20,
) -> list[ResearchItem]:
    """Query loaded items (TS ``queryItems``).

    Tag-OR filter, then full-text search over ``"{title} {content} {tags}"``
    (lowercased), then metadata field-contains filters; breaks across
    collections once ``limit`` results are collected. Raises ``ValueError`` (TS
    throws) for an unknown explicit ``collection``.
    """
    search_lower = search.lower() if search else None

    sources: list[ResearchCollection]
    if collection:
        c = STORE.get(collection)
        if not c:
            raise ValueError(f'Collection "{collection}" not loaded')
        sources = [c]
    else:
        sources = list(STORE.values())

    results: list[ResearchItem] = []

    for col in sources:
        for item in col.items:
            # Tag filter.
            if tags and len(tags) > 0:
                if not any(t in item.tags for t in tags):
                    continue

            # Full-text search.
            if search_lower:
                haystack = (
                    f"{item.title} {item.content} {' '.join(item.tags)}"
                ).lower()
                if search_lower not in haystack:
                    continue

            # Metadata field filters.
            if fields:
                match = True
                for k, v in fields.items():
                    val = _js_string(_coalesce(item.metadata.get(k), "")).lower()
                    if v.lower() not in val:
                        match = False
                        break
                if not match:
                    continue

            results.append(item)
            if len(results) >= limit:
                break
        if len(results) >= limit:
            break

    return results


def get_items(collection_name: str, item_ids: list[str]) -> list[ResearchItem]:
    """Retrieve items by id from a loaded collection (TS ``getItems``).

    Raises ``ValueError`` (TS throws) when the collection is not loaded.
    """
    col = STORE.get(collection_name)
    if not col:
        raise ValueError(f'Collection "{collection_name}" not loaded')

    id_set = set(item_ids)
    return [item for item in col.items if item.id in id_set]


def get_collection(name: str) -> ResearchCollection | None:
    """Return a loaded collection by name, or ``None`` (TS ``getCollection``)."""
    return STORE.get(name)


def list_collections() -> list[dict[str, object]]:
    """List loaded collections with summary metadata (TS ``listCollections``)."""
    return [
        {
            "name": c.name,
            "item_count": len(c.items),
            "available_tags": c.available_tags,
            "field_map": c.field_map,
        }
        for c in STORE.values()
    ]


# ── Per-run collection write helpers (Feature C) ────────────────────


def resolve_collections_dir(
    run_data_dir: str | None,
    legacy_base_dir: str,
    subtype: str,
) -> str:
    """Resolve the on-disk dir for a collection subtype (TS ``resolveCollectionsDir``).

    When ``run_data_dir`` is truthy, returns ``get_artifact_dir(run_data_dir,
    'collections/<subtype>')``; otherwise falls back to ``{legacy_base_dir}/
    collections/<subtype>``. An empty-string ``run_data_dir`` is falsy and falls
    back.
    """
    if run_data_dir:
        return get_artifact_dir(
            run_data_dir,
            "collections/raw" if subtype == "raw" else "collections/curated",
        )
    return os.path.join(legacy_base_dir, "collections", subtype)


def _write_collection_file(dir_path: str, file_name: str, collection: object) -> str:
    """``mkdir -p`` the dir, write ``collection`` as pretty JSON, return the path."""
    os.makedirs(dir_path, exist_ok=True)
    file_path = os.path.join(dir_path, file_name)
    with open(file_path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(collection, indent=2, ensure_ascii=False))
    return file_path


def persist_raw_collection(
    run_data_dir: str | None,
    legacy_base_dir: str,
    collection: object,
    file_name: str,
) -> str:
    """Persist a raw collection under the run's data dir (TS ``persistRawCollection``).

    Mirrors the TS ``persistRawCollection`` wrapper around
    :func:`resolve_collections_dir` + :func:`_write_collection_file`.
    """
    return _write_collection_file(
        resolve_collections_dir(run_data_dir, legacy_base_dir, "raw"),
        file_name,
        collection,
    )


def persist_curated_collection(
    run_data_dir: str | None,
    legacy_base_dir: str,
    collection: object,
    file_name: str,
) -> str:
    """Persist a curated collection under the run's data dir.

    Mirrors the TS ``persistCuratedCollection`` wrapper; same semantics as
    :func:`persist_raw_collection` but targets the ``collections/curated``
    subtype.
    """
    return _write_collection_file(
        resolve_collections_dir(run_data_dir, legacy_base_dir, "curated"),
        file_name,
        collection,
    )
