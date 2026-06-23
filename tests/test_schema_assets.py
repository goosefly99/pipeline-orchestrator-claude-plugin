"""Asset-integrity for the verbatim-copied JSON schemas + pipeline TOMLs (T0.3).

T0.2 relocated the Node tree under ``legacy-node/``. T0.3 copies its static
schema/TOML assets up into the Python package (``src/pipeline_orchestrator/``)
so later milestones (M2/M3) can consume them. The copies must be **byte-identical
verbatim** of the legacy originals (spec §3.1 lines 126-127 + line 588; R3 §6 =
the 12 schemas). One deliberate relocation: ``hooks-config.toml`` lives under
``hooks/`` in the Node tree but ships under ``pipeline/`` in the Python layout.

These tests assert: (1) the 12 schemas exist + are valid JSON + are exactly the
expected set; (2) the 3 TOMLs exist + parse with stdlib ``tomllib``; (3) every
copied asset is byte-identical to its legacy-node original (the verbatim
invariant).
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

LEGACY_SCHEMAS_DIR = REPO_ROOT / "legacy-node" / "pipeline" / "schemas"
LEGACY_PIPELINE_DIR = REPO_ROOT / "legacy-node" / "pipeline"
LEGACY_HOOKS_DIR = REPO_ROOT / "legacy-node" / "hooks"

PKG_SCHEMAS_DIR = REPO_ROOT / "src" / "pipeline_orchestrator" / "schemas"
PKG_PIPELINE_DIR = REPO_ROOT / "src" / "pipeline_orchestrator" / "pipeline"

EXPECTED_SCHEMAS: frozenset[str] = frozenset(
    {
        "codebase-requirements.json",
        "curated-collection.json",
        "debate-transcript.json",
        "design-spec.json",
        "kb-query-log.json",
        "knowledge-overview.json",
        "pipeline-run.json",
        "raw-collection.json",
        "research-manifest.json",
        "scaffold-document.json",
        "scaffold-manifest.json",
        "validation-report.json",
    }
)

EXPECTED_TOMLS: frozenset[str] = frozenset(
    {"pipeline.toml", "quality-gates.toml", "hooks-config.toml"}
)


def test_exactly_twelve_schemas_with_expected_names() -> None:
    """Exactly the 12 expected ``*.json`` schema files exist in the package."""
    present = {p.name for p in PKG_SCHEMAS_DIR.glob("*.json")}
    assert present == EXPECTED_SCHEMAS
    assert len(EXPECTED_SCHEMAS) == 12


@pytest.mark.parametrize("name", sorted(EXPECTED_SCHEMAS))
def test_schema_is_valid_json(name: str) -> None:
    """Each copied schema parses as valid JSON."""
    path = PKG_SCHEMAS_DIR / name
    assert path.is_file()
    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)


@pytest.mark.parametrize("name", sorted(EXPECTED_SCHEMAS))
def test_schema_is_byte_identical_to_legacy(name: str) -> None:
    """Each copied schema is byte-identical to its legacy-node original."""
    original = (LEGACY_SCHEMAS_DIR / name).read_bytes()
    copied = (PKG_SCHEMAS_DIR / name).read_bytes()
    assert copied == original


def test_exactly_three_tomls_with_expected_names() -> None:
    """Exactly the 3 expected ``*.toml`` files exist under the pipeline dir."""
    present = {p.name for p in PKG_PIPELINE_DIR.glob("*.toml")}
    assert present == EXPECTED_TOMLS
    assert len(EXPECTED_TOMLS) == 3


@pytest.mark.parametrize("name", sorted(EXPECTED_TOMLS))
def test_toml_parses_with_tomllib(name: str) -> None:
    """Each copied TOML parses cleanly with stdlib ``tomllib``."""
    path = PKG_PIPELINE_DIR / name
    assert path.is_file()
    with path.open("rb") as fh:
        parsed = tomllib.load(fh)
    assert isinstance(parsed, dict)


@pytest.mark.parametrize("name", sorted(EXPECTED_TOMLS))
def test_toml_is_byte_identical_to_legacy(name: str) -> None:
    """Each copied TOML is byte-identical to its legacy-node original.

    ``pipeline.toml`` and ``quality-gates.toml`` come from ``legacy-node/pipeline/``;
    ``hooks-config.toml`` is the deliberate ``hooks/`` -> ``pipeline/`` relocation,
    so its original lives in ``legacy-node/hooks/``.
    """
    is_hooks = name == "hooks-config.toml"
    legacy_dir = LEGACY_HOOKS_DIR if is_hooks else LEGACY_PIPELINE_DIR
    original = (legacy_dir / name).read_bytes()
    copied = (PKG_PIPELINE_DIR / name).read_bytes()
    assert copied == original
