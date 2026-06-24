"""Concept-collection engine (port of ``legacy-node/concepts.ts``).

Byte-exact behavioral port of the Node ``concepts.ts`` module: builds the
synthesis prompt + blank :class:`KnowledgeOverview` template, the three
``collect_concepts`` sub-paths (small / chunked / focus), cross-reference
analysis, and the overview persistence helpers (``save_overview`` /
``list_overviews`` / ``get_overview`` / ``persist_overview`` with Feature-C
run-scoped routing).

Parity notes:

* **``overview_id``** = ``randomBytes(4).toString('hex')`` → 8 lowercase hex
  chars (:func:`secrets.token_hex` with 4 bytes).
* **``created_date``** = ``new Date().toISOString()`` → a millisecond-precision
  ``Z``-suffixed UTC timestamp (:func:`_now_iso`, identical to the helper in
  ``bm25.py``).
* **JSON template embedding** mirrors Node ``JSON.stringify(template, null, 2)``
  (``json.dumps(obj, indent=2, ensure_ascii=False)``). ``ensure_ascii=False``
  is required to match Node's raw-UTF-8 ``JSON.stringify``: the template (and
  overview) values carry static non-ASCII characters (e.g. em/en-dashes), and
  Python's default ``ensure_ascii=True`` would ``\\uXXXX``-escape them while
  Node emits them raw — an unconditional byte-level divergence.
* **Map iteration order.** The TS ``Map`` insertion order (tag/metadata/dep
  frequency tables, chunk regrouping) is reproduced with insertion-ordered
  ``dict``; ``.sort(...)`` on tied counts is stable in both V8 and CPython, so
  ``sorted(..., key=..., reverse=True)`` keeps insertion order for ties.
* **``OUTPUT_DIR`` resolution** keeps the TS precedence: ``CONCEPTS_OUTPUT_DIR``
  env > ``config.local.json`` (``output_dir`` key) > ``./overviews``. Resolved
  lazily per-call (not at import — handshake-safe, spec §3.2) so importing the
  module reads no env.
* **``continuation_token``** is ``Buffer.from(String(i)).toString('base64')``
  → ``base64.b64encode(str(i).encode()).decode()``.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from math import ceil

from pipeline_orchestrator.collections_store import ResearchItem
from pipeline_orchestrator.models import (
    KnowledgeOverview,
    OverviewSource,
    Theme,
)
from pipeline_orchestrator.storage import get_artifact_dir

# ── Constants ────────────────────────────────────────────────────────

CHUNK_SIZE = 8


# ── Local option/result shapes (TS ``CollectOptions`` / ``ChunkInfo``) ──


@dataclass
class SourceGroup:
    """One collection's items (mirrors the inline TS ``SourceGroup``)."""

    collection: str
    items: list[ResearchItem]


@dataclass
class ChunkInfo:
    """Chunk metadata for a chunked collect (mirrors the TS ``ChunkInfo``).

    ``continuation_token`` is the ``?:`` optional (``None`` on the final chunk).
    """

    chunk_index: int
    total_chunks: int
    item_count: int
    total_items: int
    is_final_chunk: bool
    continuation_token: str | None = None


@dataclass
class CollectResult:
    """Return shape of :func:`collect_concepts` (mirrors the TS return object).

    ``chunk`` is present only in chunked mode (TS spreads ``...(chunkInfo ?
    {chunk} : {})``), so it is the ``?:`` optional here too.
    """

    template: KnowledgeOverview
    synthesis_prompt: str
    chunk: ChunkInfo | None = None


# ── Configurable output directory ────────────────────────────────────


def _now_iso() -> str:
    """Return the current UTC time as a millisecond ISO 8601 string.

    Mirrors Node ``new Date().toISOString()`` — e.g. ``2026-06-24T12:34:56.789Z``.
    """
    now = datetime.now(UTC)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def _load_output_dir() -> str:
    """Resolve the default overviews output dir (TS ``loadOutputDir``).

    Precedence: ``CONCEPTS_OUTPUT_DIR`` env (resolved absolute) > a local
    ``config.local.json`` sibling's ``output_dir`` key > ``./overviews``
    resolved against the cwd. Resolved lazily (not memoized at import) so the
    module import path reads no env — matching the handshake-safety contract.
    """
    # 1. Environment variable takes highest priority.
    env = os.environ.get("CONCEPTS_OUTPUT_DIR")
    if env:
        return os.path.abspath(env)

    # 2. Local config file (not committed to git), sibling to this module.
    config_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "config.local.json"
    )
    if os.path.exists(config_path):
        try:
            with open(config_path, encoding="utf-8") as fh:
                config = json.load(fh)
            output_dir = config.get("output_dir") if isinstance(config, dict) else None
            if output_dir:
                return os.path.abspath(str(output_dir))
        except (OSError, ValueError):
            # Ignore malformed config, fall through to default.
            pass

    # 3. Default: relative ./overviews directory.
    return os.path.abspath("./overviews")


# ── Collect concepts from research items ─────────────────────────────


def collect_concepts(
    source_groups: list[SourceGroup],
    title: str,
    focus: str | None = None,
    depth: str | None = None,
    chunk_index: int | None = None,
) -> CollectResult:
    """Build the synthesis prompt + blank overview template (TS ``collectConcepts``).

    Three sub-paths govern which items are processed:

    * **small** (``total_items <= 15``): every item, no chunk metadata;
    * **chunked** (``total_items > 15``): a ``CHUNK_SIZE``-item slice selected by
      ``chunk_index`` (default ``0``), with :class:`ChunkInfo` and a
      ``continuation_token`` until the final chunk;

    The focus/BM25 retrieval sub-path lives in the handler (``cc_tools.py``);
    this engine receives the already-filtered groups.
    """
    overview_id = secrets.token_hex(4)
    resolved_depth = depth if depth is not None else "standard"

    # Flatten all items for chunking logic (preserve (collection, item) pairs).
    all_items: list[tuple[str, ResearchItem]] = []
    for g in source_groups:
        for item in g.items:
            all_items.append((g.collection, item))
    total_items = len(all_items)

    chunk_info: ChunkInfo | None = None

    if total_items > 15:
        # Chunked mode.
        total_chunks = ceil(total_items / CHUNK_SIZE)
        chunk_idx = chunk_index if chunk_index is not None else 0
        start = chunk_idx * CHUNK_SIZE
        end = min(start + CHUNK_SIZE, total_items)
        items_to_process = all_items[start:end]

        is_final = end >= total_items
        chunk_info = ChunkInfo(
            chunk_index=chunk_idx,
            total_chunks=total_chunks,
            item_count=len(items_to_process),
            total_items=total_items,
            is_final_chunk=is_final,
            continuation_token=(
                None
                if is_final
                else base64.b64encode(str(chunk_idx + 1).encode("utf-8")).decode(
                    "ascii"
                )
            ),
        )

        # Rebuild source_groups from chunked items (preserve collection grouping
        # in first-seen order — mirrors the TS Map insertion order).
        chunk_groups: dict[str, list[ResearchItem]] = {}
        for collection, item in items_to_process:
            chunk_groups.setdefault(collection, []).append(item)
        source_groups = [
            SourceGroup(collection=collection, items=items)
            for collection, items in chunk_groups.items()
        ]
    else:
        items_to_process = all_items

    # Flatten sources for reference (from processed items only).
    sources: list[OverviewSource] = []
    for collection, item in items_to_process:
        sources.append(
            OverviewSource(
                collection=collection,
                item_id=item.id,
                title=item.title or item.content[:80],
            )
        )

    # Build formatted source material.
    source_sections: list[str] = []
    for group in source_groups:
        source_sections.append(f"\n## Collection: {group.collection}\n")
        for item in group.items:
            parts = [f"### [{item.id}] {item.title or '(untitled)'}"]
            if item.author:
                handle_suffix = (
                    f" (@{item.author.handle})" if item.author.handle else ""
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

            # Include relevant metadata fields (string / number / array only).
            meta_keys = [
                k
                for k, v in item.metadata.items()
                if isinstance(v, str)
                or (isinstance(v, (int, float)) and not isinstance(v, bool))
                or isinstance(v, list)
            ]
            if len(meta_keys) > 0:
                parts.append("")
                parts.append("Metadata:")
                for k in meta_keys:
                    v = item.metadata[k]
                    formatted = (
                        ", ".join(_js_str(x) for x in v)
                        if isinstance(v, list)
                        else _js_str(v)
                    )
                    parts.append(f"  {k}: {formatted}")

            source_sections.append("\n".join(parts))

    # Cross-reference analysis.
    cross_ref = _build_cross_references(source_groups)

    # Build the template. The TS seeds ``themes`` with a single placeholder
    # object so the LLM sees the required ``concept_names`` field name; the SAME
    # template object is both returned to the caller and JSON-embedded in the
    # prompt, so the returned template carries the populated ``sources`` and the
    # placeholder theme.
    template = KnowledgeOverview(
        overview_id=overview_id,
        title=title,
        created_date=_now_iso(),
        status="draft",
        sources=sources,
        summary="",
        concepts=[],
        themes=[Theme(name="", description="", concept_names=[])],
        key_findings=[],
        knowledge_gaps=[],
        open_questions=[],
    )

    # Serialize that same template (populated sources + placeholder theme) in TS
    # interface field order for the embedded JSON block.
    template_dict = _overview_to_dict(template)

    # Depth-specific instructions.
    depth_instructions = {
        "brief": " ".join(
            [
                "Extract 3-5 top-level concepts. Keep descriptions concise "
                "(1-2 sentences each).",
                "Identify 1-2 major themes. List 3-5 key findings.",
            ]
        ),
        "standard": " ".join(
            [
                "Extract all distinct concepts with clear descriptions and "
                "key details.",
                "Group into logical themes. Identify relationships between concepts.",
                "List key findings and any knowledge gaps.",
            ]
        ),
        "deep": " ".join(
            [
                "Extract every distinct concept, technique, and pattern with "
                "thorough descriptions.",
                "Include implementation details, edge cases, and nuances in "
                "key_details.",
                "Map all relationships between concepts. Identify dependencies "
                "and prerequisites.",
                "List comprehensive findings, knowledge gaps, and open questions "
                "worth investigating.",
            ]
        ),
    }

    focus_clause = (
        f"\n\nFocus area: {focus} — prioritize concepts and findings related to this "
        "focus, but do not exclude other salient content."
        if focus
        else ""
    )

    chunk_clause = (
        f"\n**Chunk:** {chunk_info.chunk_index + 1} of {chunk_info.total_chunks} "
        f"(items {chunk_info.chunk_index * CHUNK_SIZE + 1}–"
        f"{chunk_info.chunk_index * CHUNK_SIZE + chunk_info.item_count} of "
        f"{chunk_info.total_items})"
        if chunk_info
        else ""
    )

    template_json = json.dumps(template_dict, indent=2, ensure_ascii=False)

    prompt = (
        "# Concepts Collection Task\n"
        "\n"
        f"**Title:** {title}\n"
        f"**Depth:** {resolved_depth}{chunk_clause}{focus_clause}\n"
        "\n"
        "## Instructions\n"
        "\n"
        "Analyze the source material below and produce a structured knowledge "
        "overview.\n"
        f"{depth_instructions[resolved_depth]}\n"
        "\n"
        "For each concept extracted:\n"
        "- **name**: Clear, specific name for the concept\n"
        '- **category**: Broad category (e.g., "strategy", "technique", '
        '"architecture", '
        '"data source", "risk management")\n'
        "- **description**: What this concept is and why it matters\n"
        "- **key_details**: Specific details, numbers, parameters, or "
        "implementation notes\n"
        "- **source_items**: Which source item IDs contributed to this concept\n"
        "- **relationships**: How this concept relates to other concepts "
        "you've identified\n"
        "\n"
        "For themes:\n"
        "- Group related concepts under broader themes\n"
        '- Each theme MUST have exactly three fields: "name" (string), "description" '
        '(what unifies the concepts), and "concept_names" (array of concept name '
        'strings — NOT "concepts")\n'
        "\n"
        f"{cross_ref}\n"
        "\n"
        "## Source Material\n"
        "\n"
        f"{_join_sections(source_sections)}\n"
        "\n"
        "## Output\n"
        "\n"
        "Fill in the following JSON template. Return ONLY the completed JSON — no "
        "surrounding text.\n"
        "\n"
        "```json\n"
        f"{template_json}\n"
        "```\n"
    )

    return CollectResult(template=template, synthesis_prompt=prompt, chunk=chunk_info)


def _join_sections(sections: list[str]) -> str:
    """Join source sections the TS way: ``sections.join('\\n\\n---\\n\\n')``."""
    return "\n\n---\n\n".join(sections)


def _js_str(value: object) -> str:
    """Coerce ``value`` the way JS ``String(value)`` does (subset used here).

    Mirrors the TS ``String(v)`` in the metadata formatting: ``True``→``"true"``,
    ``False``→``"false"``, whole floats render without ``.0`` (``2.0``→``"2"``).
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
    return str(value)


# ── Cross-reference analysis ─────────────────────────────────────────


def _build_cross_references(source_groups: list[SourceGroup]) -> str:
    """Build the cross-reference analysis section (TS ``buildCrossReferences``).

    Returns ``""`` when fewer than 2 items, or when no shared tag / metadata
    distribution / shared dependency clears its threshold (the TS
    ``sections.length > 1`` guard).
    """
    all_items: list[ResearchItem] = []
    for g in source_groups:
        all_items.extend(g.items)

    if len(all_items) < 2:
        return ""

    # Tag frequency (insertion-ordered, mirrors the TS Map).
    tag_counts: dict[str, int] = {}
    for item in all_items:
        for tag in item.tags:
            tag_counts[tag] = tag_counts.get(tag, 0) + 1

    # Metadata value frequency (string fields only).
    meta_freq: dict[str, dict[str, int]] = {}
    for item in all_items:
        for k, v in item.metadata.items():
            if not isinstance(v, str):
                continue
            field_map = meta_freq.setdefault(k, {})
            field_map[v] = field_map.get(v, 0) + 1

    # Dependencies / tools mentioned.
    dep_counts: dict[str, int] = {}
    for item in all_items:
        deps = item.metadata.get("dependencies")
        if isinstance(deps, list):
            for d in deps:
                key = _js_str(d)
                dep_counts[key] = dep_counts.get(key, 0) + 1

    sections: list[str] = ["## Cross-Reference Analysis"]

    # Shared tags (count >= 2, sorted desc by count, stable for ties).
    shared_tags = sorted(
        [(tag, count) for tag, count in tag_counts.items() if count >= 2],
        key=lambda kv: kv[1],
        reverse=True,
    )
    if len(shared_tags) > 0:
        sections.append("\n**Recurring tags:**")
        for tag, count in shared_tags:
            sections.append(f"- {tag} ({count} items)")

    # Metadata field distributions.
    for field_name, values in meta_freq.items():
        sorted_vals = sorted(values.items(), key=lambda kv: kv[1], reverse=True)
        if len(sorted_vals) >= 2 and any(c >= 2 for _, c in sorted_vals):
            sections.append(f"\n**{field_name} distribution:**")
            for val, count in sorted_vals[:10]:
                sections.append(f"- {val} ({count})")

    # Shared dependencies (count >= 2, sorted desc).
    shared_deps = sorted(
        [(dep, count) for dep, count in dep_counts.items() if count >= 2],
        key=lambda kv: kv[1],
        reverse=True,
    )
    if len(shared_deps) > 0:
        sections.append("\n**Common dependencies:**")
        for dep, count in shared_deps:
            sections.append(f"- {dep} ({count} items)")

    return "\n".join(sections) if len(sections) > 1 else ""


# ── Persistence ──────────────────────────────────────────────────────


def overview_file_name(overview: KnowledgeOverview) -> str:
    """Compute the on-disk filename for ``overview`` (TS ``overviewFileName``).

    Slug = title lowercased, non-alphanumeric runs collapsed to ``-``,
    leading/trailing ``-`` trimmed, truncated to 60 chars; then
    ``--{overview_id}.json``. Pure helper: no I/O, no module state.
    """
    import re

    slug = overview.title.lower()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    slug = re.sub(r"^-|-$", "", slug)
    slug = slug[:60]
    return f"{slug}--{overview.overview_id}.json"


def _overview_to_dict(overview: KnowledgeOverview) -> dict[str, object]:
    """Serialize a :class:`KnowledgeOverview` to a JSON-ready dict in TS field order.

    Mirrors the ``cc-types.ts`` interface insertion order
    (``overview_id, title, created_date, status, sources, summary, concepts,
    themes, key_findings, knowledge_gaps, open_questions``) so the on-disk
    ``JSON.stringify(overview, null, 2)`` byte layout is reproduced.
    """
    return {
        "overview_id": overview.overview_id,
        "title": overview.title,
        "created_date": overview.created_date,
        "status": overview.status,
        "sources": [
            {"collection": s.collection, "item_id": s.item_id, "title": s.title}
            for s in overview.sources
        ],
        "summary": overview.summary,
        "concepts": [
            {
                "name": c.name,
                "category": c.category,
                "description": c.description,
                "key_details": c.key_details,
                "source_items": c.source_items,
                "relationships": c.relationships,
            }
            for c in overview.concepts
        ],
        "themes": [
            {
                "name": t.name,
                "description": t.description,
                "concept_names": t.concept_names,
            }
            for t in overview.themes
        ],
        "key_findings": overview.key_findings,
        "knowledge_gaps": overview.knowledge_gaps,
        "open_questions": overview.open_questions,
    }


def _as_dict(value: object) -> dict[str, object]:
    """Narrow ``value`` to a ``dict`` for typed field access (else ``{}``)."""
    return value if isinstance(value, dict) else {}


def _as_str_list(value: object) -> list[str]:
    """Narrow ``value`` to ``list[str]``, coercing elements via ``str`` (else [])."""
    if not isinstance(value, list):
        return []
    return [str(v) for v in value]


def _overview_from_dict(data: dict[str, object]) -> KnowledgeOverview:
    """Reconstruct a :class:`KnowledgeOverview` from a parsed JSON dict.

    Used by :func:`get_overview`. Nested ``sources`` / ``concepts`` / ``themes``
    are rebuilt into their dataclasses; missing arrays default to empty (TS
    ``data.concepts?.length ?? 0`` tolerance).
    """
    from pipeline_orchestrator.models import Concept, Theme

    raw_sources = data.get("sources")
    raw_concepts = data.get("concepts")
    raw_themes = data.get("themes")
    sources_list = raw_sources if isinstance(raw_sources, list) else []
    concepts_list = raw_concepts if isinstance(raw_concepts, list) else []
    themes_list = raw_themes if isinstance(raw_themes, list) else []

    return KnowledgeOverview(
        overview_id=str(data["overview_id"]),
        title=str(data["title"]),
        created_date=str(data["created_date"]),
        status="complete" if data.get("status") == "complete" else "draft",
        summary=str(data.get("summary", "")),
        sources=[
            OverviewSource(
                collection=str(_as_dict(s).get("collection", "")),
                item_id=str(_as_dict(s).get("item_id", "")),
                title=str(_as_dict(s).get("title", "")),
            )
            for s in sources_list
        ],
        concepts=[
            Concept(
                name=str(_as_dict(c).get("name", "")),
                category=str(_as_dict(c).get("category", "")),
                description=str(_as_dict(c).get("description", "")),
                key_details=_as_str_list(_as_dict(c).get("key_details")),
                source_items=_as_str_list(_as_dict(c).get("source_items")),
                relationships=_as_str_list(_as_dict(c).get("relationships")),
            )
            for c in concepts_list
        ],
        themes=[
            Theme(
                name=str(_as_dict(t).get("name", "")),
                description=str(_as_dict(t).get("description", "")),
                concept_names=_as_str_list(_as_dict(t).get("concept_names")),
            )
            for t in themes_list
        ],
        key_findings=_as_str_list(data.get("key_findings")),
        knowledge_gaps=_as_str_list(data.get("knowledge_gaps")),
        open_questions=_as_str_list(data.get("open_questions")),
    )


def save_overview(overview: KnowledgeOverview, output_dir: str | None = None) -> str:
    """Write ``overview`` and return the path (TS ``saveOverview``).

    ``output_dir`` (resolved absolute) overrides the module default
    (:func:`_load_output_dir`). Creates the directory tree if absent;
    overwrite-ok (collision avoided in practice by the random-hex id suffix).
    """
    directory = os.path.abspath(output_dir) if output_dir else _load_output_dir()
    if not os.path.exists(directory):
        os.makedirs(directory, exist_ok=True)

    file_path = os.path.join(directory, overview_file_name(overview))
    with open(file_path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(_overview_to_dict(overview), indent=2, ensure_ascii=False))
    return file_path


def resolve_overviews_dir(
    run_data_dir: str | None,
    legacy_base_dir: str,
) -> str:
    """Resolve the overviews dir (TS ``resolveOverviewsDir``).

    ``get_artifact_dir(run_data_dir, 'overviews')`` when ``run_data_dir`` is
    truthy, else ``{legacy_base_dir}/overviews``. An empty-string
    ``run_data_dir`` is falsy and falls back.
    """
    if run_data_dir:
        return get_artifact_dir(run_data_dir, "overviews")
    return os.path.join(legacy_base_dir, "overviews")


def persist_overview(
    run_data_dir: str | None,
    legacy_base_dir: str,
    overview: KnowledgeOverview,
    output_dir_override: str | None = None,
) -> str:
    """Persist ``overview`` under the run's data dir (TS ``persistOverview``).

    Target-dir precedence: ``output_dir_override`` (resolved absolute) >
    ``get_artifact_dir(run_data_dir, 'overviews')`` when ``run_data_dir`` set >
    ``{legacy_base_dir}/overviews``. Always ``mkdir -p`` then write; returns the
    absolute path.
    """
    directory = (
        os.path.abspath(output_dir_override)
        if output_dir_override
        else resolve_overviews_dir(run_data_dir, legacy_base_dir)
    )

    os.makedirs(directory, exist_ok=True)
    file_path = os.path.join(directory, overview_file_name(overview))
    with open(file_path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(_overview_to_dict(overview), indent=2, ensure_ascii=False))
    return file_path


def list_overviews(directory: str | None = None) -> list[dict[str, object]]:
    """List saved overviews with summary metadata (TS ``listOverviews``).

    Returns ``[]`` for a nonexistent directory. ``concept_count`` /
    ``source_count`` tolerate missing arrays (TS ``?.length ?? 0``).
    """
    target = os.path.abspath(directory) if directory else _load_output_dir()
    if not os.path.exists(target):
        return []

    results: list[dict[str, object]] = []
    for f in os.listdir(target):
        if not f.endswith(".json"):
            continue
        with open(os.path.join(target, f), encoding="utf-8") as fh:
            data = json.load(fh)
        concepts = data.get("concepts")
        sources = data.get("sources")
        results.append(
            {
                "overview_id": data.get("overview_id"),
                "title": data.get("title"),
                "created_date": data.get("created_date"),
                "status": data.get("status"),
                "concept_count": len(concepts) if isinstance(concepts, list) else 0,
                "source_count": len(sources) if isinstance(sources, list) else 0,
                "file": f,
            }
        )
    return results


def get_overview(
    overview_id: str,
    directory: str | None = None,
) -> KnowledgeOverview | None:
    """Retrieve a saved overview by ``overview_id`` substring (TS ``getOverview``).

    Returns ``None`` for a nonexistent directory or no filename match.
    """
    target = os.path.abspath(directory) if directory else _load_output_dir()
    if not os.path.exists(target):
        return None

    files = [f for f in os.listdir(target) if f.endswith(".json")]
    for f in files:
        if overview_id in f:
            with open(os.path.join(target, f), encoding="utf-8") as fh:
                data = json.load(fh)
            return _overview_from_dict(data)
    return None
