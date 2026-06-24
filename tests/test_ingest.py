"""Port of ``legacy-node/tests/ingest.test.ts`` (42 cases).

Byte-exact behavioral parity for ``pipeline_orchestrator.ingest``: file-type
detection (:func:`detect_file_type`), content extraction
(:func:`extract_content`), date normalization (:func:`normalize_date`), the
async collection build (:func:`ingest_documents_async`), recursive directory
enumeration (:func:`enumerate_dir`), the schema-validation integration cases
(against ``raw-collection.json`` via :mod:`pipeline_orchestrator.validator`),
and the disk-first response-envelope shape.

The async ``ingestDocumentsAsync`` is awaited in the TS via the test runner's
``await``; here it is driven with ``asyncio.run(...)`` inside sync test bodies,
matching the existing handler-test house style. Filesystem fixtures are rooted
at the pytest ``tmp_path`` fixture (the ``mkdtempSync``/``rmSync`` analogue), and
the random-hex id / ISO ``created_date`` are never asserted as literals — only
structural / substring / prefix checks, exactly as the Node suite does.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime
from pathlib import Path

from pipeline_orchestrator.ingest import (
    detect_file_type,
    enumerate_dir,
    extract_content,
    ingest_documents_async,
    normalize_date,
)
from pipeline_orchestrator.validator import load_schemas, validate_artifact

# ── Schema dir resolution (mirrors test_validator.py) ─────────────────
#
# The Node test reads ``../pipeline/schemas`` (relative to ``legacy-node/tests``);
# the Python schemas live at ``src/pipeline_orchestrator/schemas/``, resolved
# here OS-agnostically from ``REPO_ROOT``.

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMAS_DIR = REPO_ROOT / "src" / "pipeline_orchestrator" / "schemas"


# ── detectFileType ────────────────────────────────────────────


class TestDetectFileType:
    def test_detects_markdown(self) -> None:
        assert detect_file_type("README.md") == "markdown"
        assert detect_file_type("notes.MD") == "markdown"

    def test_detects_plain_text(self) -> None:
        assert detect_file_type("notes.txt") == "text"
        assert detect_file_type("log.log") == "text"

    def test_detects_code_files(self) -> None:
        assert detect_file_type("script.py") == "code"
        assert detect_file_type("index.ts") == "code"
        assert detect_file_type("main.js") == "code"
        assert detect_file_type("App.tsx") == "code"
        assert detect_file_type("server.go") == "code"
        assert detect_file_type("lib.rs") == "code"
        assert detect_file_type("Main.java") == "code"
        assert detect_file_type("util.rb") == "code"
        assert detect_file_type("script.sh") == "code"

    def test_detects_json(self) -> None:
        assert detect_file_type("data.json") == "json"
        assert detect_file_type("config.JSON") == "json"

    def test_detects_csv(self) -> None:
        assert detect_file_type("data.csv") == "csv"

    def test_detects_html(self) -> None:
        assert detect_file_type("page.html") == "html"
        assert detect_file_type("page.htm") == "html"

    def test_detects_yaml(self) -> None:
        assert detect_file_type("config.yaml") == "yaml"
        assert detect_file_type("config.yml") == "yaml"

    def test_detects_toml(self) -> None:
        assert detect_file_type("config.toml") == "toml"

    def test_detects_pdf(self) -> None:
        assert detect_file_type("report.pdf") == "pdf"
        assert detect_file_type("REPORT.PDF") == "pdf"

    def test_detects_jupyter_notebooks(self) -> None:
        assert detect_file_type("analysis.ipynb") == "notebook"

    def test_falls_back_to_text_for_unknown_extensions(self) -> None:
        assert detect_file_type("file.xyz") == "text"
        assert detect_file_type("noextension") == "text"


# ── extractContent ────────────────────────────────────────────


class TestExtractContent:
    def test_reads_markdown_as_is(self, tmp_path: Path) -> None:
        p = tmp_path / "notes.md"
        p.write_text("# Title\n\nSome text here.", encoding="utf-8")
        result = extract_content(str(p), "markdown")
        assert result["content"] == "# Title\n\nSome text here."
        assert result.get("raw_html") is None

    def test_reads_plain_text_as_is(self, tmp_path: Path) -> None:
        p = tmp_path / "notes.txt"
        p.write_text("Hello world", encoding="utf-8")
        result = extract_content(str(p), "text")
        assert result["content"] == "Hello world"

    def test_reads_code_files_as_is(self, tmp_path: Path) -> None:
        p = tmp_path / "script.py"
        p.write_text("def foo():\n    return 42\n", encoding="utf-8")
        result = extract_content(str(p), "code")
        assert result["content"] == "def foo():\n    return 42\n"

    def test_reads_json_and_restringifies_with_2_space_indent(
        self, tmp_path: Path
    ) -> None:
        p = tmp_path / "data.json"
        p.write_text('{"a":1,"b":[1,2]}', encoding="utf-8")
        result = extract_content(str(p), "json")
        assert result["content"] == json.dumps(
            {"a": 1, "b": [1, 2]}, indent=2, ensure_ascii=False
        )

    def test_reads_csv_as_raw_text(self, tmp_path: Path) -> None:
        p = tmp_path / "data.csv"
        p.write_text("name,value\nalpha,1\nbeta,2\n", encoding="utf-8")
        result = extract_content(str(p), "csv")
        assert result["content"] == "name,value\nalpha,1\nbeta,2\n"

    def test_strips_html_tags_and_stores_raw_in_raw_html(
        self, tmp_path: Path
    ) -> None:
        p = tmp_path / "page.html"
        p.write_text(
            "<html><body><h1>Title</h1><p>Body text.</p></body></html>",
            encoding="utf-8",
        )
        result = extract_content(str(p), "html")
        assert "Title" in result["content"]
        assert "Body text." in result["content"]
        assert "<h1>" not in result["content"]
        assert "<h1>Title</h1>" in result.get("raw_html", "")

    def test_reads_yaml_as_raw_text(self, tmp_path: Path) -> None:
        p = tmp_path / "config.yaml"
        p.write_text("key: value\nlist:\n  - a\n  - b\n", encoding="utf-8")
        result = extract_content(str(p), "yaml")
        assert result["content"] == "key: value\nlist:\n  - a\n  - b\n"

    def test_reads_toml_as_raw_text(self, tmp_path: Path) -> None:
        p = tmp_path / "config.toml"
        p.write_text('[section]\nkey = "value"\n', encoding="utf-8")
        result = extract_content(str(p), "toml")
        assert result["content"] == '[section]\nkey = "value"\n'

    def test_extracts_cell_sources_from_jupyter_notebooks(
        self, tmp_path: Path
    ) -> None:
        notebook = {
            "nbformat": 4,
            "cells": [
                {"cell_type": "markdown", "source": ["# Analysis\n", "Some intro."]},
                {
                    "cell_type": "code",
                    "source": ['import pandas as pd\n', 'df = pd.read_csv("data.csv")'],
                },
                {"cell_type": "raw", "source": ["raw cell content"]},
            ],
        }
        p = tmp_path / "analysis.ipynb"
        p.write_text(json.dumps(notebook), encoding="utf-8")
        result = extract_content(str(p), "notebook")
        assert "# Analysis" in result["content"]
        assert "import pandas as pd" in result["content"]
        assert "raw cell content" in result["content"]


# ── normalizeDate ────────────────────────────────────────────


def _assert_valid_iso_datetime(value: str, label: str) -> None:
    """Assert that ``value`` is a valid ISO 8601 date-time (TS helper)."""
    assert "T" in value, f'{label}: missing "T" separator — got "{value}"'
    parsed = _js_date_parse(value)
    assert parsed is not None, f'{label}: parse returned None — got "{value}"'


def _js_date_parse(value: str) -> datetime | None:
    """``Date.parse`` analogue for the ISO date-time strings the test produces.

    The asserted outputs are always ``...Z``-suffixed ISO date-times (the
    ``toISOString`` form); ``datetime.fromisoformat`` parses them after mapping
    the trailing ``Z`` to ``+00:00``.
    """
    s = value
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


class TestNormalizeDate:
    def test_coerces_us_locale_date_to_valid_iso(self) -> None:
        result = normalize_date("4/1/2026")
        _assert_valid_iso_datetime(result, "US locale")
        # The parsed date should represent April 1, 2026.
        d = _js_date_parse(result)
        assert d is not None
        d = d.astimezone(UTC)
        assert d.year == 2026
        assert d.month == 4  # April
        assert d.day == 1

    def test_coerces_date_only_to_valid_iso(self) -> None:
        result = normalize_date("2026-04-06")
        _assert_valid_iso_datetime(result, "date-only")
        d = _js_date_parse(result)
        assert d is not None
        d = d.astimezone(UTC)
        assert d.year == 2026
        # Month may be 4 (April) or 3 (March) depending on timezone handling.
        assert d.month in (4, 3), (
            f"Expected month 4 (April) or 3 (March, timezone edge), got {d.month}"
        )

    def test_coerces_human_readable_to_valid_iso(self) -> None:
        result = normalize_date("April 6, 2026")
        _assert_valid_iso_datetime(result, "human-readable")
        d = _js_date_parse(result)
        assert d is not None
        d = d.astimezone(UTC)
        assert d.year == 2026

    def test_returns_already_valid_iso_as_is(self) -> None:
        iso = "2026-04-06T12:00:00.000Z"
        result = normalize_date(iso)
        assert result == iso, "Already-valid ISO should be returned unchanged"

    def test_falls_back_to_fallback_now_for_unparseable_strings(self) -> None:
        fallback = "2026-01-15T08:30:00.000Z"
        result = normalize_date("not-a-date", fallback)
        assert result == fallback, "Unparseable input should return the fallback value"
        _assert_valid_iso_datetime(result, "unparseable fallback")


# ── ingestDocumentsAsync ─────────────────────────────────────


class TestIngestDocumentsAsync:
    def test_produces_a_valid_raw_collection_from_multiple_files(
        self, tmp_path: Path
    ) -> None:
        md_path = tmp_path / "report.md"
        csv_path = tmp_path / "data.csv"
        md_path.write_text("# Research Report\n\nFindings here.", encoding="utf-8")
        csv_path.write_text("symbol,price\nBTC,60000\n", encoding="utf-8")

        collection = asyncio.run(
            ingest_documents_async([str(md_path), str(csv_path)], "test-ingest")
        )

        assert collection["status"] == "raw"
        collection_id = collection["collection_id"]
        assert isinstance(collection_id, str)
        assert collection_id.startswith("test-ingest--")
        items = collection["items"]
        assert isinstance(items, list)
        assert len(items) == 2
        assert isinstance(collection["created_date"], str)

    def test_each_item_has_correct_fields(self, tmp_path: Path) -> None:
        p = tmp_path / "notes.md"
        p.write_text("# Notes\n\nSome content.", encoding="utf-8")

        collection = asyncio.run(ingest_documents_async([str(p)], "my-ingest"))
        items = collection["items"]
        assert isinstance(items, list)
        item = items[0]

        assert item["source_type"] == "local_ingest"
        source_ref = item["source_ref"]
        assert isinstance(source_ref, str)
        assert source_ref.startswith("file:")
        assert "notes.md" in source_ref
        item_id = item["id"]
        assert isinstance(item_id, str)
        assert item_id.startswith("local_")
        content = item["content"]
        assert isinstance(content, str)
        assert "Notes" in content
        assert isinstance(item["date"], str)
        assert isinstance(item["tags"], list)
        assert len(item["tags"]) == 0
        metadata = item["metadata"]
        assert isinstance(metadata, dict)
        assert metadata["file_type"] == "markdown"
        assert isinstance(metadata["file_size"], int)

    def test_source_runs_audit_trail_has_one_entry_per_file(
        self, tmp_path: Path
    ) -> None:
        p1 = tmp_path / "a.txt"
        p2 = tmp_path / "b.txt"
        p1.write_text("alpha", encoding="utf-8")
        p2.write_text("beta", encoding="utf-8")

        collection = asyncio.run(
            ingest_documents_async([str(p1), str(p2)], "audit-test")
        )

        source_runs = collection["source_runs"]
        assert isinstance(source_runs, list)
        assert len(source_runs) == 2
        run = source_runs[0]
        assert run["source_type"] == "local_ingest"
        assert run["items_found"] == 1
        assert run["items_stored"] == 1
        assert isinstance(run["executed_at"], str)
        path = run["path"]
        assert isinstance(path, str)
        assert "a.txt" in path or "b.txt" in path

    def test_stores_raw_html_in_metadata_for_html_files(
        self, tmp_path: Path
    ) -> None:
        p = tmp_path / "page.html"
        p.write_text("<html><body><h1>Hi</h1></body></html>", encoding="utf-8")

        collection = asyncio.run(ingest_documents_async([str(p)], "html-test"))
        items = collection["items"]
        assert isinstance(items, list)
        item = items[0]

        content = item["content"]
        assert isinstance(content, str)
        assert "<h1>" not in content
        metadata = item["metadata"]
        assert isinstance(metadata, dict)
        assert metadata["raw_html"]

    def test_item_title_is_the_filename_without_extension(
        self, tmp_path: Path
    ) -> None:
        p = tmp_path / "quarterly-report.md"
        p.write_text("# Q4 Results", encoding="utf-8")

        collection = asyncio.run(ingest_documents_async([str(p)], "title-test"))
        items = collection["items"]
        assert isinstance(items, list)
        assert items[0]["title"] == "quarterly-report"

    def test_throws_if_a_file_does_not_exist(self, tmp_path: Path) -> None:
        import pytest

        with pytest.raises(FileNotFoundError):
            asyncio.run(
                ingest_documents_async(
                    [str(tmp_path / "nonexistent.txt")], "err-test"
                )
            )

    def test_handles_empty_file_list_and_returns_empty_collection(self) -> None:
        collection = asyncio.run(ingest_documents_async([], "empty-test"))
        items = collection["items"]
        source_runs = collection["source_runs"]
        assert isinstance(items, list)
        assert isinstance(source_runs, list)
        assert len(items) == 0
        assert len(source_runs) == 0


# ── PDF extraction ────────────────────────────────────────────


class TestIngestDocumentsAsyncPdf:
    def test_handles_non_pdf_files_identically(self, tmp_path: Path) -> None:
        p = tmp_path / "notes.md"
        p.write_text("# Notes\n\nSome content.", encoding="utf-8")

        collection = asyncio.run(ingest_documents_async([str(p)], "async-test"))
        items = collection["items"]
        assert isinstance(items, list)
        assert len(items) == 1
        assert items[0]["source_type"] == "local_ingest"
        content = items[0]["content"]
        assert isinstance(content, str)
        assert "Notes" in content

    def test_returns_correct_metadata_for_markdown(self, tmp_path: Path) -> None:
        p = tmp_path / "report.md"
        p.write_text("# Report", encoding="utf-8")

        collection = asyncio.run(ingest_documents_async([str(p)], "meta-test"))
        items = collection["items"]
        assert isinstance(items, list)
        metadata = items[0]["metadata"]
        assert isinstance(metadata, dict)
        assert metadata["file_type"] == "markdown"


# ── Integration: validate against real schema ─────────────────


class TestIntegrationSchemaValid:
    def test_validates_md_csv_json_against_raw_collection(
        self, tmp_path: Path
    ) -> None:
        md_path = tmp_path / "research.md"
        csv_path = tmp_path / "prices.csv"
        json_path = tmp_path / "config.json"

        md_path.write_text(
            "# Research\n\nPrediction market dynamics in Q1 2026.", encoding="utf-8"
        )
        csv_path.write_text(
            "date,price\n2026-01-01,0.72\n2026-01-02,0.68\n", encoding="utf-8"
        )
        json_path.write_text(
            json.dumps({"model": "kalshi-v2", "threshold": 0.65}), encoding="utf-8"
        )

        collection = asyncio.run(
            ingest_documents_async(
                [str(md_path), str(csv_path), str(json_path)], "integration-test"
            )
        )

        items = collection["items"]
        assert isinstance(items, list)
        assert len(items) == 3
        assert collection["status"] == "raw"

        schemas = load_schemas(SCHEMAS_DIR)
        result = validate_artifact(schemas, "raw-collection.json", collection)
        assert result.valid is True, (
            "Schema validation failed:\n" + "\n".join(result.errors)
        )

    def test_validates_html_ingestion_content_stripped_schema_valid(
        self, tmp_path: Path
    ) -> None:
        html_path = tmp_path / "article.html"
        html_path.write_text(
            "<html><head><title>Market Report</title></head><body>"
            "<h1>Q1 Report</h1><p>Kalshi volumes increased 40% YoY.</p>"
            "</body></html>",
            encoding="utf-8",
        )

        collection = asyncio.run(
            ingest_documents_async([str(html_path)], "html-integ")
        )
        schemas = load_schemas(SCHEMAS_DIR)
        result = validate_artifact(schemas, "raw-collection.json", collection)

        assert result.valid is True, f"Validation errors: {', '.join(result.errors)}"

        items = collection["items"]
        assert isinstance(items, list)
        item = items[0]
        content = item["content"]
        assert isinstance(content, str)
        assert "Q1 Report" in content
        assert "<h1>" not in content
        metadata = item["metadata"]
        assert isinstance(metadata, dict)
        assert metadata["raw_html"]

    def test_validates_jupyter_notebook_ingestion(self, tmp_path: Path) -> None:
        notebook = {
            "nbformat": 4,
            "cells": [
                {
                    "cell_type": "markdown",
                    "source": ["# Kalshi Strategy\n", "Analysis of YES contracts."],
                },
                {
                    "cell_type": "code",
                    "source": ["import kalshi_api\n", "client = kalshi_api.Client()"],
                },
            ],
        }
        nb_path = tmp_path / "strategy.ipynb"
        nb_path.write_text(json.dumps(notebook), encoding="utf-8")

        collection = asyncio.run(
            ingest_documents_async([str(nb_path)], "notebook-integ")
        )
        schemas = load_schemas(SCHEMAS_DIR)
        result = validate_artifact(schemas, "raw-collection.json", collection)

        assert result.valid is True, f"Validation errors: {', '.join(result.errors)}"

        items = collection["items"]
        assert isinstance(items, list)
        item = items[0]
        content = item["content"]
        assert isinstance(content, str)
        assert "Kalshi Strategy" in content
        assert "import kalshi_api" in content
        metadata = item["metadata"]
        assert isinstance(metadata, dict)
        assert metadata["file_type"] == "notebook"


# ── Disk-first ingestion response shape ─────────────────────


class TestDiskFirstResponseShape:
    def test_response_envelope_has_artifact_path_and_no_artifact_field(
        self, tmp_path: Path
    ) -> None:
        md_path = tmp_path / "doc1.md"
        csv_path = tmp_path / "doc2.csv"
        txt_path = tmp_path / "doc3.txt"

        md_path.write_text(
            "# Document One\n\nResearch findings on prediction markets.",
            encoding="utf-8",
        )
        csv_path.write_text(
            "symbol,price,volume\nBTC,60000,1200\nETH,3200,800\nSOL,120,500\n",
            encoding="utf-8",
        )
        txt_path.write_text(
            "Plain text document with analysis of market trends and patterns.",
            encoding="utf-8",
        )

        collection = asyncio.run(
            ingest_documents_async(
                [str(md_path), str(csv_path), str(txt_path)], "disk-first-test"
            )
        )

        # Replicate the disk-first pattern from handleIngestDocuments.
        raw_dir = tmp_path / "collections" / "raw"
        raw_dir.mkdir(parents=True, exist_ok=True)
        collection_id = collection["collection_id"]
        assert isinstance(collection_id, str)
        artifact_path = raw_dir / f"{collection_id}.json"
        artifact_path.write_text(
            json.dumps(collection, indent=2, ensure_ascii=False), encoding="utf-8"
        )

        source_runs = collection["source_runs"]
        assert isinstance(source_runs, list)
        items = collection["items"]
        assert isinstance(items, list)
        sources = [
            r["path"]
            for r in source_runs
            if isinstance(r.get("path"), str)
        ]
        envelope: dict[str, object] = {
            "status": "ok",
            "data": {
                "collection_id": collection_id,
                "item_count": len(items),
                "sources": sources,
                "artifact_path": str(artifact_path),
            },
            "next_step": (
                f'Call pipeline_register_artifact with file_path="{artifact_path}" '
                "to register this collection."
            ),
        }

        data = envelope["data"]
        assert isinstance(data, dict)
        # artifact_path is present and is a string.
        assert isinstance(data["artifact_path"], str)
        assert len(data["artifact_path"]) > 0, "artifact_path must be non-empty"

        # No artifact field in the response data.
        assert data.get("artifact") is None, "response must not contain artifact field"
        assert "artifact" not in data, "response data must not have artifact key"

    def test_json_serialized_response_envelope_is_under_10kb(
        self, tmp_path: Path
    ) -> None:
        md_path = tmp_path / "large1.md"
        csv_path = tmp_path / "large2.csv"
        txt_path = tmp_path / "large3.txt"

        md_path.write_text(
            "# Large Document\n\n" + "Analysis paragraph. " * 100, encoding="utf-8"
        )
        csv_path.write_text(
            "col1,col2,col3\n" + "val1,val2,val3\n" * 50, encoding="utf-8"
        )
        txt_path.write_text(
            "Detailed market analysis report. " * 80, encoding="utf-8"
        )

        collection = asyncio.run(
            ingest_documents_async(
                [str(md_path), str(csv_path), str(txt_path)], "size-test"
            )
        )
        items = collection["items"]
        assert isinstance(items, list)
        assert len(items) == 3

        raw_dir = tmp_path / "collections" / "raw"
        raw_dir.mkdir(parents=True, exist_ok=True)
        collection_id = collection["collection_id"]
        assert isinstance(collection_id, str)
        artifact_path = raw_dir / f"{collection_id}.json"
        artifact_path.write_text(
            json.dumps(collection, indent=2, ensure_ascii=False), encoding="utf-8"
        )

        source_runs = collection["source_runs"]
        assert isinstance(source_runs, list)
        sources = [
            r["path"]
            for r in source_runs
            if isinstance(r.get("path"), str)
        ]
        envelope: dict[str, object] = {
            "status": "ok",
            "data": {
                "collection_id": collection_id,
                "item_count": len(items),
                "sources": sources,
                "artifact_path": str(artifact_path),
            },
            "next_step": (
                f'Call pipeline_register_artifact with file_path="{artifact_path}" '
                "to register this collection."
            ),
        }

        serialized = json.dumps(envelope, indent=2, ensure_ascii=False)
        size_bytes = len(serialized.encode("utf-8"))
        assert size_bytes < 10_000, (
            f"Response envelope must be under 10KB, got {size_bytes} bytes"
        )

    def test_artifact_path_points_to_valid_json_containing_collection(
        self, tmp_path: Path
    ) -> None:
        md_path = tmp_path / "verify1.md"
        txt_path = tmp_path / "verify2.txt"
        csv_path = tmp_path / "verify3.csv"

        md_path.write_text(
            "# Verification Test\n\nContent for disk-first verification.",
            encoding="utf-8",
        )
        txt_path.write_text("Plain text content for verification.", encoding="utf-8")
        csv_path.write_text("name,value\nalpha,1\nbeta,2\n", encoding="utf-8")

        collection = asyncio.run(
            ingest_documents_async(
                [str(md_path), str(txt_path), str(csv_path)], "verify-test"
            )
        )

        raw_dir = tmp_path / "collections" / "raw"
        raw_dir.mkdir(parents=True, exist_ok=True)
        collection_id = collection["collection_id"]
        assert isinstance(collection_id, str)
        artifact_path = raw_dir / f"{collection_id}.json"
        artifact_path.write_text(
            json.dumps(collection, indent=2, ensure_ascii=False), encoding="utf-8"
        )

        # The file exists.
        assert os.path.exists(artifact_path), (
            f"Artifact file must exist at {artifact_path}"
        )

        # The file contains valid JSON.
        raw_content = artifact_path.read_text(encoding="utf-8")
        parsed = json.loads(raw_content)

        # The parsed JSON matches the collection.
        assert parsed["collection_id"] == collection["collection_id"]
        assert parsed["status"] == "raw"
        assert isinstance(parsed["items"], list)
        assert len(parsed["items"]) == 3


# ── enumerateDir ─────────────────────────────────────────────


class TestEnumerateDir:
    def test_recursively_expands_supported_files_excludes_unsupported_and_dot_dirs(
        self, tmp_path: Path
    ) -> None:
        # Layout mirrors the Node test:
        #   <tmp>/vault/
        #     note.md          ← supported
        #     readme.txt       ← supported
        #     binary.bin       ← unsupported (no EXT_MAP entry)
        #     sub/deep.yaml     ← supported (nested subdir)
        #     .obsidian/settings.json  ← dot-dir — excluded
        #     index/corpus.md   ← COLD store sentinel — excluded
        vault_dir = tmp_path / "vault"
        sub_dir = vault_dir / "sub"
        dot_dir = vault_dir / ".obsidian"
        index_dir = vault_dir / "index"
        sub_dir.mkdir(parents=True, exist_ok=True)
        dot_dir.mkdir(parents=True, exist_ok=True)
        index_dir.mkdir(parents=True, exist_ok=True)

        (vault_dir / "note.md").write_text("# Note", encoding="utf-8")
        (vault_dir / "readme.txt").write_text("readme", encoding="utf-8")
        (vault_dir / "binary.bin").write_bytes(b"\x00\x01\x02")
        (sub_dir / "deep.yaml").write_text("key: value", encoding="utf-8")
        (dot_dir / "settings.json").write_text("{}", encoding="utf-8")
        (index_dir / "corpus.md").write_text("# Corpus", encoding="utf-8")

        found = sorted(enumerate_dir(str(vault_dir)))

        # Should include: note.md, readme.txt, sub/deep.yaml — exactly 3.
        assert len(found) == 3, (
            f"Expected 3 supported files, got {len(found)}: {', '.join(found)}"
        )
        assert any(
            p.endswith("note.md") for p in found
        ), "note.md should be included"
        assert any(
            p.endswith("readme.txt") for p in found
        ), "readme.txt should be included"
        assert any(
            "sub" in p and p.endswith("deep.yaml") for p in found
        ), "sub/deep.yaml should be included"
        # Exclusions.
        assert not any(
            p.endswith(".bin") for p in found
        ), "binary.bin must be excluded (unsupported extension)"
        assert not any(
            ".obsidian" in p for p in found
        ), ".obsidian dot-dir must be excluded"
        assert not any(
            os.path.join("index", "corpus.md") in p for p in found
        ), "index/ must be excluded (COLD store sentinel)"

    def test_returns_empty_array_for_directory_with_no_supported_files(
        self, tmp_path: Path
    ) -> None:
        empty_dir = tmp_path / "empty-vault"
        bin_dir = empty_dir / "bins"
        bin_dir.mkdir(parents=True, exist_ok=True)
        (empty_dir / "data.bin").write_bytes(b"\x00")
        (bin_dir / "other.exe").write_bytes(b"\x00")

        found = enumerate_dir(str(empty_dir))
        assert len(found) == 0, (
            "Should return empty array when no supported files exist"
        )
