"""Spec synthesis / save / list engine (port of ``legacy-node/synth.ts``).

Byte-exact behavioral port of the Node ``synth.ts`` module: the
``create_spec_synthesis`` prompt builder (flatten items → sources, build the
cross-reference section, embed a BLANK :class:`DesignSpec` template), the
``spec_file_name`` slug helper, and the spec-persistence helpers
(``save_spec`` / ``list_specs`` / ``resolve_specs_dir`` / ``persist_spec`` with
Feature-C run-scoped routing).

Parity notes:

* **``spec_id``** = ``crypto.randomUUID()`` → :func:`uuid.uuid4` (canonical
  lowercase hyphenated UUID string).
* **``created_date`` / ``updated_date``** = ``new Date().toISOString()
  .split('T')[0]`` → a ``YYYY-MM-DD`` UTC date string (:func:`_now_date`).
* **JSON template / on-disk serialization** mirrors Node
  ``JSON.stringify(x, null, 2)`` (``json.dumps(x, indent=2,
  ensure_ascii=False)``). ``ensure_ascii=False`` is required to match Node's
  raw-UTF-8 ``JSON.stringify``: the template and spec values can carry non-ASCII
  characters (titles, notes, em/en-dashes), and Python's default
  ``ensure_ascii=True`` would ``\\uXXXX``-escape them while Node emits them raw —
  an unconditional byte-level divergence.
* **Optional ``domain`` key.** The TS template literal sets ``domain:
  options.domain``; when ``undefined`` ``JSON.stringify`` drops the key
  entirely. :func:`_spec_to_dict` omits ``domain`` when ``None`` to match.
* **Map iteration order.** The TS ``Map`` / ``Set`` insertion order (tag counts,
  dep/platform/instrument sets) is reproduced with insertion-ordered ``dict`` /
  ``dict.fromkeys``; ``.sort(...)`` on tied counts is stable in both V8 and
  CPython.
* **``??`` nullish defaults** are ported as explicit ``is None`` checks /
  ``dict.get(k, default)``, never ``or`` — an explicit falsy value (``0`` /
  ``''`` / ``False`` / ``[]``) must survive.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import UTC, datetime

from pipeline_orchestrator.collections_store import ResearchItem
from pipeline_orchestrator.models import (
    DesignSpec,
    SpecArchitecture,
    SpecImplementation,
    SpecOverview,
    SpecSource,
)
from pipeline_orchestrator.storage import get_artifact_dir

# ── Local option/result shapes (TS inline ``sourceGroups`` literal) ──


# ``createSpecSynthesis`` takes ``Array<{ collection; items; relevance_notes? }>``
# as plain dicts (the handler builds them inline), so the engine accepts the same
# loosely-typed shape rather than a dedicated dataclass — matching the TS where
# ``sourceGroups`` is an anonymous object-literal array.


def _now_date() -> str:
    """Return today's UTC date as ``YYYY-MM-DD``.

    Mirrors Node ``new Date().toISOString().split('T')[0]`` — the date half of a
    ``Z``-suffixed UTC ISO timestamp.
    """
    return datetime.now(UTC).strftime("%Y-%m-%d")


def _js_str(value: object) -> str:
    """Coerce ``value`` the way JS ``String(value)`` does (subset used here).

    Mirrors the TS ``String(v)`` in the metadata formatting: ``True``→``"true"``,
    ``False``→``"false"``, ``None``→``"null"``, whole floats render without
    ``.0`` (``2.0``→``"2"``).
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


def _format_metadata_value(value: object) -> str:
    """Format a metadata value the TS
    ``typeof v === 'object' ? JSON.stringify(v) : String(v)`` way.

    JS ``typeof`` returns ``'object'`` for arrays AND plain objects (and ``null``,
    but ``null`` is filtered out by the ``!= null`` guard before this is called),
    so lists and dicts are JSON-serialized (compact, the way ``JSON.stringify(v)``
    with no spacing argument does) while every other primitive coerces via
    :func:`_js_str`.
    """
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return _js_str(value)


def create_spec_synthesis(
    title: str,
    source_groups: list[dict[str, object]],
    spec_type: str | None = None,
    domain: str | None = None,
    focus: str | None = None,
) -> tuple[DesignSpec, str]:
    """Build the blank :class:`DesignSpec` template + synthesis prompt.

    Port of TS ``createSpecSynthesis``.

    ``source_groups`` is the handler-built ``[{collection, items, relevance_notes?}]``
    list (``items`` are :class:`ResearchItem`s; ``relevance_notes`` is an optional
    ``{item_id: note}`` mapping). Returns ``(template, synthesis_prompt)``.
    """
    spec_id = str(uuid.uuid4())
    now = _now_date()

    # Flatten all items, carrying their collection + per-item relevance note.
    all_items: list[tuple[ResearchItem, str, str]] = []
    for g in source_groups:
        collection = str(g["collection"])
        items = g["items"]
        assert isinstance(items, list)
        relevance_notes = g.get("relevance_notes")
        notes = relevance_notes if isinstance(relevance_notes, dict) else {}
        for item in items:
            assert isinstance(item, ResearchItem)
            relevance = notes.get(item.id, "")
            all_items.append((item, collection, str(relevance)))

    spec_sources: list[SpecSource] = [
        SpecSource(
            collection=collection,
            item_id=item.id,
            title=item.title,
            relevance=relevance,
        )
        for item, collection, relevance in all_items
    ]

    source_blocks: list[str] = []
    for item, collection, _relevance in all_items:
        lines: list[str] = [
            f"=== Source: {item.title} ===",
            f"Collection: {collection} | ID: {item.id}",
        ]
        if item.author:
            handle_suffix = (
                f" ({item.author.handle})" if item.author.handle else ""
            )
            lines.append(f"Author: {item.author.name}{handle_suffix}")
            if item.author.bio:
                lines.append(f"Bio: {item.author.bio}")
        if item.date:
            lines.append(f"Date: {item.date}")
        if item.url:
            lines.append(f"URL: {item.url}")
        if len(item.tags) > 0:
            lines.append(f"Tags: {', '.join(item.tags)}")
        lines.append("")
        lines.append(item.content)

        domain_keys = [
            "strategy_type",
            "instrument",
            "platform",
            "dependencies",
            "engagement",
            "embedded_content",
            "has_article_content",
        ]
        for k in domain_keys:
            v = item.metadata.get(k)
            if v is not None:
                lines.append(f"{k}: {_format_metadata_value(v)}")

        source_blocks.append("\n".join(lines))

    source_content = "\n\n---\n\n".join(source_blocks)

    # Cross-reference analysis (insertion-ordered tag counts; insertion-ordered
    # dep/platform/instrument sets — mirror the TS Map / Set order).
    tag_counts: dict[str, int] = {}
    for item, _collection, _relevance in all_items:
        for t in item.tags:
            tag_counts[t] = tag_counts.get(t, 0) + 1
    shared_tags = [
        f"{t} ({c})"
        for t, c in sorted(
            [(t, c) for t, c in tag_counts.items() if c > 1],
            key=lambda kv: kv[1],
            reverse=True,
        )
    ]

    all_deps: dict[str, None] = {}
    all_platforms: dict[str, None] = {}
    all_instruments: dict[str, None] = {}
    for item, _collection, _relevance in all_items:
        deps = item.metadata.get("dependencies")
        if isinstance(deps, list):
            for d in deps:
                all_deps.setdefault(_js_str(d), None)
        platform = item.metadata.get("platform")
        if platform:
            all_platforms.setdefault(_js_str(platform), None)
        instrument = item.metadata.get("instrument")
        if instrument:
            all_instruments.setdefault(_js_str(instrument), None)

    template = DesignSpec(
        spec_id=spec_id,
        title=title,
        created_date=now,
        updated_date=now,
        version="1.0",
        status="draft",
        domain=domain,
        spec_type=spec_type if spec_type is not None else "implementation",  # type: ignore[arg-type]
        sources=spec_sources,
        overview=SpecOverview(
            description="",
            objectives=[],
            constraints=[focus] if focus else [],
            assumptions=[],
        ),
        architecture=SpecArchitecture(
            components=[],
            data_flow="",
            integration_points=[*all_deps.keys(), *all_platforms.keys()],
        ),
        implementation=SpecImplementation(
            phases=[],
            tech_stack=[*all_deps.keys()],
            complexity="medium",
        ),
        risks=[],
        success_criteria=[],
        notes="",
    )

    template_json = json.dumps(_spec_to_dict(template), indent=2, ensure_ascii=False)

    # Build the synthesis prompt. Mirrors the TS array-of-lines literal that
    # ``.filter(line => line !== null).join('\n')``s — the two ``? : null``
    # lines (Domain / Focus) are conditionally included.
    synthesis_lines: list[str | None] = [
        f"# Design Spec Synthesis: {title}",
        "",
        f"**Spec ID:** {spec_id}",
        f"**Type:** {spec_type if spec_type is not None else 'implementation'}",
        f"**Domain:** {domain}" if domain else None,
        f"**Focus:** {focus}" if focus else None,
        "",
        f"## Source Material ({len(all_items)} items from "
        f"{len(source_groups)} collection(s))",
        "",
        source_content,
        "",
        "## Cross-Reference Analysis",
        "",
        f"**Shared tags:** {', '.join(shared_tags) or 'None'}",
        f"**Platforms:** {', '.join(all_platforms.keys()) or 'None'}",
        f"**Instruments:** {', '.join(all_instruments.keys()) or 'None'}",
        f"**Dependencies:** {', '.join(all_deps.keys()) or 'None'}",
        "",
        "## Spec Template",
        "",
        "Complete this spec by synthesising the source material above.",
        "Fill in all empty strings and arrays. Return the full JSON object.",
        "",
        "```json",
        template_json,
        "```",
    ]
    synthesis = "\n".join(line for line in synthesis_lines if line is not None)

    return template, synthesis


def spec_file_name(spec: DesignSpec) -> str:
    """Compute the on-disk filename for ``spec`` (TS ``specFileName``).

    Slug = title lowercased, non-alphanumeric runs collapsed to ``-``,
    leading/trailing ``-`` trimmed, truncated to 60 chars; then
    ``--{spec_id[0:8]}.json``. Pure helper: no I/O, no module state.
    """
    slug = spec.title.lower()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    slug = re.sub(r"^-|-$", "", slug)
    slug = slug[:60]
    return f"{slug}--{spec.spec_id[:8]}.json"


def _spec_to_dict(spec: DesignSpec) -> dict[str, object]:
    """Serialize a :class:`DesignSpec` to a JSON-ready dict in TS field order.

    Mirrors the ``createSpecSynthesis`` template object-literal insertion order
    (``spec_id, title, created_date, updated_date, version, status, domain?,
    spec_type, sources, overview, architecture, implementation, risks,
    success_criteria, notes``) so the on-disk ``JSON.stringify(spec, null, 2)``
    byte layout is reproduced. ``domain`` is omitted when ``None`` (TS leaves it
    ``undefined``, which ``JSON.stringify`` drops).
    """
    out: dict[str, object] = {
        "spec_id": spec.spec_id,
        "title": spec.title,
        "created_date": spec.created_date,
        "updated_date": spec.updated_date,
        "version": spec.version,
        "status": spec.status,
    }
    if spec.domain is not None:
        out["domain"] = spec.domain
    out["spec_type"] = spec.spec_type
    out["sources"] = [
        {
            "collection": s.collection,
            "item_id": s.item_id,
            "title": s.title,
            "relevance": s.relevance,
        }
        for s in spec.sources
    ]
    out["overview"] = {
        "description": spec.overview.description,
        "objectives": spec.overview.objectives,
        "constraints": spec.overview.constraints,
        "assumptions": spec.overview.assumptions,
    }
    out["architecture"] = {
        "components": [
            {
                "name": c.name,
                "purpose": c.purpose,
                "inputs": c.inputs,
                "outputs": c.outputs,
                "dependencies": c.dependencies,
            }
            for c in spec.architecture.components
        ],
        "data_flow": spec.architecture.data_flow,
        "integration_points": spec.architecture.integration_points,
    }
    out["implementation"] = {
        "phases": [
            {
                "phase": p.phase,
                "name": p.name,
                "tasks": p.tasks,
                "deliverables": p.deliverables,
            }
            for p in spec.implementation.phases
        ],
        "tech_stack": spec.implementation.tech_stack,
        "complexity": spec.implementation.complexity,
    }
    out["risks"] = [
        {
            "description": r.description,
            "severity": r.severity,
            "mitigation": r.mitigation,
        }
        for r in spec.risks
    ]
    out["success_criteria"] = spec.success_criteria
    out["notes"] = spec.notes
    return out


def save_spec(spec: DesignSpec, output_dir: str) -> str:
    """Write ``spec`` and return the path (TS ``saveSpec``).

    Resolves ``output_dir`` to absolute, creates it if absent, mutates
    ``spec.updated_date`` to today, then writes ``{slug}--{id8}.json``.
    """
    directory = os.path.abspath(output_dir)
    if not os.path.exists(directory):
        os.makedirs(directory, exist_ok=True)

    file_path = os.path.join(directory, spec_file_name(spec))

    spec.updated_date = _now_date()
    with open(file_path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(_spec_to_dict(spec), indent=2, ensure_ascii=False))
    return file_path


def resolve_specs_dir(
    run_data_dir: str | None,
    legacy_base_dir: str,
) -> str:
    """Resolve the specs dir (TS ``resolveSpecsDir``).

    ``get_artifact_dir(run_data_dir, 'specs')`` when ``run_data_dir`` is truthy,
    else ``{legacy_base_dir}/specs``. An empty-string ``run_data_dir`` is falsy
    and falls back.
    """
    if run_data_dir:
        return get_artifact_dir(run_data_dir, "specs")
    return os.path.join(legacy_base_dir, "specs")


def persist_spec(
    run_data_dir: str | None,
    legacy_base_dir: str,
    spec: DesignSpec,
    output_dir_override: str | None = None,
) -> str:
    """Persist ``spec`` under the run's data dir (TS ``persistSpec``).

    Target-dir precedence: ``output_dir_override`` (resolved absolute) >
    ``get_artifact_dir(run_data_dir, 'specs')`` when ``run_data_dir`` set >
    ``{legacy_base_dir}/specs``. Always ``mkdir -p`` then write; mutates
    ``spec.updated_date`` to today before writing; returns the absolute path.
    """
    directory = (
        os.path.abspath(output_dir_override)
        if output_dir_override
        else resolve_specs_dir(run_data_dir, legacy_base_dir)
    )

    os.makedirs(directory, exist_ok=True)
    file_path = os.path.join(directory, spec_file_name(spec))

    spec.updated_date = _now_date()
    with open(file_path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(_spec_to_dict(spec), indent=2, ensure_ascii=False))
    return file_path


def list_specs(directory: str) -> list[dict[str, object]]:
    """List saved specs with summary metadata (TS ``listSpecs``).

    Returns ``[]`` for a nonexistent directory. Each entry carries
    ``spec_id`` / ``title`` / ``created_date`` / ``status`` / ``spec_type`` /
    ``source_count`` / ``file_path``. Files that fail to parse are skipped (TS
    ``try/catch`` → ``null`` → ``.filter(Boolean)``).
    """
    target = os.path.abspath(directory)
    if not os.path.exists(target):
        return []

    results: list[dict[str, object]] = []
    for f in os.listdir(target):
        if not f.endswith(".json"):
            continue
        file_path = os.path.join(target, f)
        try:
            with open(file_path, encoding="utf-8") as fh:
                spec = json.load(fh)
        except (OSError, ValueError):
            continue
        try:
            entry = {
                "spec_id": spec["spec_id"],
                "title": spec["title"],
                "created_date": spec["created_date"],
                "status": spec["status"],
                "spec_type": spec["spec_type"],
                # TS reads ``spec.sources.length`` unconditionally; a parsed
                # object missing ``sources`` (or with a non-array) throws here
                # and the file is skipped — matching the Node try/catch → null
                # → ``.filter(Boolean)`` behavior (no ``?.`` tolerance).
                "source_count": len(spec["sources"]),
                "file_path": file_path,
            }
        except (TypeError, KeyError):
            continue
        results.append(entry)
    return results
