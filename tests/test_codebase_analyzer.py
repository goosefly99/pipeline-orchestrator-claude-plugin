"""Port of ``legacy-node/tests/codebase-analyzer.test.ts`` (40 cases).

Byte-exact behavioral parity for ``pipeline_orchestrator.codebase_analyzer``:
the directory walk (:func:`walk_directory_tree`), manifest detection
(:func:`detect_manifest`), the four manifest parsers + ``parse_error`` surfacing
(:func:`parse_manifest`), directive extraction (:func:`read_agents_directives`),
convention detection (:func:`detect_conventions`), and the top-level
:func:`analyze_codebase` entry incl. its edge cases and structural invariants.

The Node ``describe``/``it`` blocks map to pytest classes/methods 1:1 (40 parity
cases). Node ``mkdtempSync``/``rmSync`` temp dirs map to the pytest ``tmp_path``
fixture; ``writeFixture`` maps to :func:`_write_fixture`. The
``analyzeCodebase`` result is a plain dict (the JSON artifact), so the TS
attribute access ``result.package.name`` becomes ``result["package"]["name"]``;
the typed helper results are frozen dataclasses, so ``result.manifest`` etc.
stay attribute access. The random-suffix id / ISO ``analyzed_date`` are never
asserted as literals — only prefix / determinism / distinctness checks, exactly
as the Node suite does.

The "real pipeline-mcp project" case resolves the Node test's ``__dirname/..``
(i.e. the original ``legacy-node/`` tree, which still carries the unmodified
``package.json`` named ``pipeline-mcp``, its ``tsconfig.json``,
``package-lock.json``, ``tests/`` subdir, root ``AGENTS.md``, the
``@modelcontextprotocol/sdk`` dependency, and the ``typescript`` dev dep) — the
faithful analogue of the original fixture (the pure-Python repo root carries a
``pyproject.toml`` named ``pipeline-orchestrator`` instead, which would not
satisfy the original assertions).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from pipeline_orchestrator.codebase_analyzer import (
    analyze_codebase,
    detect_conventions,
    detect_manifest,
    parse_manifest,
    read_agents_directives,
    walk_directory_tree,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
LEGACY_NODE_DIR = REPO_ROOT / "legacy-node"


# ── helpers ───────────────────────────────────────────────────


def _write_fixture(base: Path, rel_path: str, content: str) -> None:
    """``writeFixture`` analogue: mkdir -p the parent, write UTF-8 ``content``."""
    full = base / rel_path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(content, encoding="utf-8")


# ── walkDirectoryTree ─────────────────────────────────────────


class TestWalkDirectoryTree:
    def test_returns_significant_directories_relative_to_root(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "src").mkdir(parents=True, exist_ok=True)
        (tmp_path / "src" / "lib").mkdir(parents=True, exist_ok=True)
        (tmp_path / "tests").mkdir(parents=True, exist_ok=True)
        _write_fixture(tmp_path, "src/index.ts", "export {}")
        _write_fixture(tmp_path, "src/lib/util.ts", "export {}")
        _write_fixture(tmp_path, "tests/foo.test.ts", "")

        dirs = walk_directory_tree(str(tmp_path))
        assert "src" in dirs, f"expected 'src' in {dirs}"
        assert "src/lib" in dirs, f"expected 'src/lib' in {dirs}"
        assert "tests" in dirs, f"expected 'tests' in {dirs}"

    def test_excludes_node_modules(self, tmp_path: Path) -> None:
        (tmp_path / "node_modules" / "some-pkg").mkdir(parents=True, exist_ok=True)
        _write_fixture(tmp_path, "node_modules/some-pkg/index.js", "")

        dirs = walk_directory_tree(str(tmp_path))
        assert not any(d.startswith("node_modules") for d in dirs), (
            f"node_modules should be excluded: {dirs}"
        )

    def test_excludes_hidden_dirs_pycache_dist(self, tmp_path: Path) -> None:
        (tmp_path / ".git").mkdir(parents=True, exist_ok=True)
        (tmp_path / ".venv").mkdir(parents=True, exist_ok=True)
        (tmp_path / "__pycache__").mkdir(parents=True, exist_ok=True)
        (tmp_path / "dist").mkdir(parents=True, exist_ok=True)
        (tmp_path / "src").mkdir(parents=True, exist_ok=True)
        _write_fixture(tmp_path, ".git/HEAD", "ref: refs/heads/main")
        _write_fixture(tmp_path, "dist/bundle.js", "")
        _write_fixture(tmp_path, "src/main.ts", "")

        dirs = walk_directory_tree(str(tmp_path))
        assert not any(d.startswith(".git") for d in dirs), ".git should be excluded"
        assert not any(d.startswith(".venv") for d in dirs), ".venv should be excluded"
        assert not any(d.startswith("__pycache__") for d in dirs), (
            "__pycache__ should be excluded"
        )
        assert not any(d.startswith("dist") for d in dirs), "dist should be excluded"
        assert "src" in dirs, "src should be included"

    def test_returns_empty_for_flat_directory_with_only_files(
        self, tmp_path: Path
    ) -> None:
        _write_fixture(tmp_path, "index.ts", "export {}")
        _write_fixture(tmp_path, "package.json", "{}")

        dirs = walk_directory_tree(str(tmp_path))
        assert dirs == []


# ── detectManifest ────────────────────────────────────────────


class TestDetectManifest:
    def test_detects_package_json_typescript_with_npm(self, tmp_path: Path) -> None:
        _write_fixture(
            tmp_path,
            "package.json",
            json.dumps({"name": "my-pkg", "version": "1.0.0"}),
        )
        _write_fixture(tmp_path, "tsconfig.json", json.dumps({"compilerOptions": {}}))
        _write_fixture(
            tmp_path, "package-lock.json", json.dumps({"lockfileVersion": 3})
        )

        result = detect_manifest(str(tmp_path))
        assert result.manifest == "package.json"
        assert result.language == "typescript"
        assert result.package_manager == "npm"

    def test_detects_pyproject_toml_python(self, tmp_path: Path) -> None:
        _write_fixture(
            tmp_path,
            "pyproject.toml",
            '[project]\nname = "my-project"\nversion = "0.1.0"\n',
        )

        result = detect_manifest(str(tmp_path))
        assert result.manifest == "pyproject.toml"
        assert result.language == "python"

    def test_detects_cargo_toml_rust_with_cargo(self, tmp_path: Path) -> None:
        _write_fixture(
            tmp_path,
            "Cargo.toml",
            '[package]\nname = "my-crate"\nversion = "0.1.0"\n',
        )

        result = detect_manifest(str(tmp_path))
        assert result.manifest == "Cargo.toml"
        assert result.language == "rust"
        assert result.package_manager == "cargo"

    def test_detects_pnpm_via_pnpm_lock_yaml(self, tmp_path: Path) -> None:
        _write_fixture(tmp_path, "package.json", json.dumps({"name": "my-pkg"}))
        _write_fixture(tmp_path, "pnpm-lock.yaml", "lockfileVersion: 9.0\n")

        result = detect_manifest(str(tmp_path))
        assert result.manifest == "package.json"
        assert result.package_manager == "pnpm"

    def test_returns_unknown_for_no_manifest(self, tmp_path: Path) -> None:
        _write_fixture(tmp_path, "README.md", "# Hello")

        result = detect_manifest(str(tmp_path))
        assert result.manifest is None
        assert result.language == "unknown"
        assert result.package_manager is None


# ── parseManifest ─────────────────────────────────────────────


class TestParseManifest:
    def test_extracts_name_runtime_deps_from_package_json(
        self, tmp_path: Path
    ) -> None:
        pkg = {
            "name": "test-pkg",
            "engines": {"node": ">=20"},
            "dependencies": {"express": "^4.18.0", "lodash": "^4.17.21"},
            "devDependencies": {"typescript": "^5.0.0", "jest": "^29.0.0"},
            "scripts": {"test": "jest"},
        }
        _write_fixture(tmp_path, "package.json", json.dumps(pkg))

        result = parse_manifest(str(tmp_path), "package.json")
        assert result.name == "test-pkg"
        assert result.runtime == ">=20"
        assert any(d.startswith("express@") for d in result.runtime_deps), (
            "expected express dep"
        )
        assert any(d.startswith("typescript@") for d in result.dev_deps), (
            "expected typescript dev dep"
        )
        assert result.test_script == "jest"

    def test_extracts_name_runtime_deps_from_pyproject_toml(
        self, tmp_path: Path
    ) -> None:
        content = """[project]
name = "my-project"
requires-python = ">=3.11"
dependencies = [
  "requests>=2.28.0",
  "pydantic>=2.0.0",
]

[tool.uv.dev-dependencies]
dev = [
  "pytest>=7.0.0",
  "mypy>=1.0.0",
]
"""
        _write_fixture(tmp_path, "pyproject.toml", content)

        result = parse_manifest(str(tmp_path), "pyproject.toml")
        assert result.name == "my-project"
        assert result.runtime == ">=3.11"
        assert any("requests" in d for d in result.runtime_deps), (
            "expected requests dep"
        )
        assert any("pytest" in d for d in result.dev_deps), "expected pytest dev dep"

    def test_extracts_name_deps_from_cargo_toml(self, tmp_path: Path) -> None:
        content = """[package]
name = "my-crate"
version = "0.1.0"
edition = "2021"

[dependencies]
serde = { version = "1.0", features = ["derive"] }
tokio = "1.0"

[dev-dependencies]
criterion = "0.5"
"""
        _write_fixture(tmp_path, "Cargo.toml", content)

        result = parse_manifest(str(tmp_path), "Cargo.toml")
        assert result.name == "my-crate"
        assert any("serde" in d for d in result.runtime_deps), (
            f"expected serde dep: {result.runtime_deps}"
        )
        assert any("criterion" in d for d in result.dev_deps), (
            f"expected criterion dev dep: {result.dev_deps}"
        )

    def test_extracts_deps_from_go_mod_with_multiple_require_blocks(
        self, tmp_path: Path
    ) -> None:
        content = (
            "module github.com/example/multi-require\n"
            "\n"
            "go 1.21\n"
            "\n"
            "require (\n"
            "\tgithub.com/stretchr/testify v1.8.4\n"
            "\tgithub.com/gorilla/mux v1.8.0\n"
            ")\n"
            "\n"
            "require (\n"
            "\tgolang.org/x/text v0.14.0\n"
            "\tgolang.org/x/net v0.20.0\n"
            ")\n"
        )
        _write_fixture(tmp_path, "go.mod", content)

        result = parse_manifest(str(tmp_path), "go.mod")
        assert result.name == "github.com/example/multi-require"
        assert result.runtime == "go 1.21"
        # Dependencies from the first require block
        assert any("stretchr/testify" in d for d in result.runtime_deps), (
            f"expected stretchr/testify dep: {result.runtime_deps}"
        )
        assert any("gorilla/mux" in d for d in result.runtime_deps), (
            f"expected gorilla/mux dep: {result.runtime_deps}"
        )
        # Dependencies from the second require block
        assert any("golang.org/x/text" in d for d in result.runtime_deps), (
            f"expected golang.org/x/text dep: {result.runtime_deps}"
        )
        assert any("golang.org/x/net" in d for d in result.runtime_deps), (
            f"expected golang.org/x/net dep: {result.runtime_deps}"
        )
        # Verify total count: 4 deps across 2 blocks
        assert len(result.runtime_deps) == 4, (
            f"expected 4 deps total, got {len(result.runtime_deps)}: "
            f"{result.runtime_deps}"
        )


class TestParseManifestParseErrorSurfacing:
    def test_valid_package_json_has_no_parse_error(self, tmp_path: Path) -> None:
        _write_fixture(tmp_path, "package.json", json.dumps({"name": "ok"}))
        result = parse_manifest(str(tmp_path), "package.json")
        assert result.parse_error is None
        assert result.name == "ok"

    def test_reports_parse_error_for_malformed_json_package_json(
        self, tmp_path: Path
    ) -> None:
        _write_fixture(
            tmp_path, "package.json", '{ "name": "broken", invalid json here'
        )
        result = parse_manifest(str(tmp_path), "package.json")
        assert result.parse_error, "expected parse_error to be set"
        assert "package.json" in result.parse_error, (
            f"expected error to mention package.json: {result.parse_error}"
        )
        assert result.name == ""

    def test_reports_parse_error_when_package_json_does_not_exist(
        self, tmp_path: Path
    ) -> None:
        # no package.json written
        result = parse_manifest(str(tmp_path), "package.json")
        assert result.parse_error, "expected parse_error to be set for missing file"
        assert "package.json" in result.parse_error.lower()

    def test_reports_parse_error_when_pyproject_toml_does_not_exist(
        self, tmp_path: Path
    ) -> None:
        result = parse_manifest(str(tmp_path), "pyproject.toml")
        assert result.parse_error, "expected parse_error to be set"
        assert "pyproject.toml" in result.parse_error

    def test_reports_parse_error_when_cargo_toml_does_not_exist(
        self, tmp_path: Path
    ) -> None:
        result = parse_manifest(str(tmp_path), "Cargo.toml")
        assert result.parse_error, "expected parse_error to be set"
        assert "Cargo.toml" in result.parse_error

    def test_reports_parse_error_when_go_mod_does_not_exist(
        self, tmp_path: Path
    ) -> None:
        result = parse_manifest(str(tmp_path), "go.mod")
        assert result.parse_error, "expected parse_error to be set"
        assert "go.mod" in result.parse_error

    def test_unknown_manifest_type_returns_parse_error_describing_reason(
        self, tmp_path: Path
    ) -> None:
        result = parse_manifest(str(tmp_path), "composer.json")
        assert result.parse_error, "expected parse_error for unknown manifest"
        assert "composer.json" in result.parse_error
        assert result.name == ""

    def test_analyze_codebase_handles_malformed_manifest_gracefully(
        self, tmp_path: Path
    ) -> None:
        _write_fixture(tmp_path, "package.json", "{ invalid json")
        result = analyze_codebase(str(tmp_path))
        assert result["package"]["name"] == "", (  # type: ignore[index]
            "expected empty name when manifest parse fails"
        )


# ── readAgentsDirectives ──────────────────────────────────────


class TestReadAgentsDirectives:
    def test_reads_agents_md_at_multiple_directory_levels(
        self, tmp_path: Path
    ) -> None:
        _write_fixture(
            tmp_path,
            "AGENTS.md",
            "# Root Directives\n\n- Use TypeScript\n- Follow strict mode\n",
        )
        (tmp_path / "src").mkdir(parents=True, exist_ok=True)
        _write_fixture(
            tmp_path, "src/AGENTS.md", "# Src Directives\n\n- Keep modules small\n"
        )

        directives = read_agents_directives(str(tmp_path))
        assert len(directives) >= 2, (
            f"expected at least 2 directive files: {directives}"
        )
        files = [d.file for d in directives]
        assert any(f.endswith("AGENTS.md") and "src" not in f for f in files), (
            "expected root AGENTS.md"
        )
        assert any("src" in f for f in files), "expected src AGENTS.md"

        all_directives = [d for entry in directives for d in entry.directives]
        assert any("TypeScript" in d for d in all_directives), (
            "expected TypeScript directive"
        )

    def test_reads_claude_md_as_well(self, tmp_path: Path) -> None:
        _write_fixture(
            tmp_path,
            "CLAUDE.md",
            "# Claude Instructions\n\n- Always write tests\n- Use ESM imports\n",
        )

        directives = read_agents_directives(str(tmp_path))
        assert len(directives) >= 1, "expected at least 1 directive file"
        all_directives = [d for entry in directives for d in entry.directives]
        assert any("write tests" in d for d in all_directives), (
            "expected 'write tests' directive"
        )

    def test_strips_markdown_bold_italic_formatting_from_directives(
        self, tmp_path: Path
    ) -> None:
        _write_fixture(
            tmp_path,
            "AGENTS.md",
            "# Directives\n\n- **Never** use var declarations\n"
            "- Use *strict* mode\n- Check `tsconfig.json` settings\n",
        )

        directives = read_agents_directives(str(tmp_path))
        all_directives = [d for entry in directives for d in entry.directives]
        # Bold should be stripped (** removed but text preserved)
        assert any("Never" in d and "**" not in d for d in all_directives), (
            "bold markers should be stripped"
        )
        # Italic should be stripped
        assert any("strict" in d and "*strict*" not in d for d in all_directives), (
            "italic markers should be stripped"
        )

    def test_returns_empty_for_no_directive_files(self, tmp_path: Path) -> None:
        _write_fixture(tmp_path, "README.md", "# My Project\n")
        _write_fixture(tmp_path, "package.json", "{}")

        directives = read_agents_directives(str(tmp_path))
        assert directives == []

    def test_skips_node_modules_when_scanning(self, tmp_path: Path) -> None:
        (tmp_path / "node_modules" / "some-pkg").mkdir(parents=True, exist_ok=True)
        _write_fixture(
            tmp_path, "node_modules/some-pkg/AGENTS.md", "- Inside node_modules\n"
        )
        _write_fixture(tmp_path, "AGENTS.md", "- Root directive\n")

        directives = read_agents_directives(str(tmp_path))
        # Should find root AGENTS.md but not node_modules one. Compare the path
        # RELATIVE to the root: the pytest ``tmp_path`` name itself contains the
        # substring "node_modules" (the test name), so an absolute-path
        # ``"node_modules" in f`` check would false-trip — the Node ``mkdtemp``
        # path never contains it. The real invariant is that no discovered file
        # sits UNDER a ``node_modules`` directory component.
        rel_files = [
            os.path.relpath(d.file, str(tmp_path)).replace("\\", "/")
            for d in directives
        ]
        assert not any("node_modules/" in f for f in rel_files), (
            f"should not include node_modules AGENTS.md: {rel_files}"
        )


# ── detectConventions ─────────────────────────────────────────


class TestDetectConventions:
    def test_detects_esm_imports_in_typescript_files(self, tmp_path: Path) -> None:
        _write_fixture(
            tmp_path,
            "package.json",
            json.dumps({"name": "test", "type": "module"}),
        )
        _write_fixture(
            tmp_path,
            "src/index.ts",
            "import { foo } from './foo.ts'\n"
            "import type { Bar } from './bar.ts'\nexport { foo }\n",
        )

        result = detect_conventions(str(tmp_path), "typescript")
        assert result.import_style == "esm"

    def test_detects_type_annotations_in_typescript(self, tmp_path: Path) -> None:
        _write_fixture(tmp_path, "package.json", json.dumps({"name": "test"}))
        _write_fixture(
            tmp_path,
            "src/util.ts",
            "export function greet(name: string): string {\n"
            "  return `Hello, ${name}`\n}\n",
        )

        result = detect_conventions(str(tmp_path), "typescript")
        assert result.type_annotations is True

    def test_detects_pytest_from_python_dev_deps(self, tmp_path: Path) -> None:
        content = """[project]
name = "my-project"
requires-python = ">=3.11"
dependencies = []

[tool.uv.dev-dependencies]
dev = [
  "pytest>=7.0.0",
]
"""
        _write_fixture(tmp_path, "pyproject.toml", content)
        _write_fixture(
            tmp_path,
            "tests/test_foo.py",
            "def test_basic():\n    assert 1 + 1 == 2\n",
        )

        result = detect_conventions(str(tmp_path), "python")
        assert result.test_framework == "pytest"

    def test_detects_node_test_from_package_json_test_script(
        self, tmp_path: Path
    ) -> None:
        pkg = {
            "name": "test-pkg",
            "scripts": {"test": "node --test tests/*.test.ts"},
        }
        _write_fixture(tmp_path, "package.json", json.dumps(pkg))
        _write_fixture(
            tmp_path,
            "tests/foo.test.ts",
            "import { describe, it } from 'node:test'\n",
        )

        result = detect_conventions(str(tmp_path), "typescript")
        assert result.test_framework == "node:test"


# ── analyzeCodebase — edge cases ──────────────────────────────


class TestAnalyzeCodebaseEdgeCases:
    def test_handles_a_directory_with_no_manifest_gracefully(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "src").mkdir()
        _write_fixture(tmp_path, "src/main.py", 'print("hello")')

        result = analyze_codebase(str(tmp_path))

        assert str(result["requirements_id"]).startswith("cr-")
        assert result["package"]["language"] == "unknown"  # type: ignore[index]
        assert "manifest" not in result["package"]  # type: ignore[operator]
        assert "src" in result["structure"]["directory_tree"]  # type: ignore[index]
        assert result["dependencies"]["runtime"] == []  # type: ignore[index]

    def test_handles_an_empty_directory(self, tmp_path: Path) -> None:
        result = analyze_codebase(str(tmp_path))

        assert str(result["requirements_id"]).startswith("cr-")
        assert result["structure"]["directory_tree"] == []  # type: ignore[index]
        assert result["agents_directives"] == []
        assert result["dependencies"]["runtime"] == []  # type: ignore[index]

    def test_produces_same_requirements_id_for_two_calls_same_dir_same_day(
        self, tmp_path: Path
    ) -> None:
        _write_fixture(tmp_path, "package.json", json.dumps({"name": "pkg"}))

        result1 = analyze_codebase(str(tmp_path))
        result2 = analyze_codebase(str(tmp_path))

        # ID is deterministic per path+day, so both calls return the same id
        assert result1["requirements_id"] == result2["requirements_id"]

    def test_does_not_throw_on_nonexistent_path_returns_valid_result(
        self, tmp_path: Path
    ) -> None:
        bogus_path = os.path.join(
            str(tmp_path), "definitely-not-a-real-directory-xyz-789"
        )
        result = analyze_codebase(bogus_path)

        # Must still produce the full structural shape
        assert str(result["requirements_id"]).startswith("cr-")
        assert result["codebase_path"] == bogus_path
        assert result["package"]["language"] == "unknown"  # type: ignore[index]
        assert "manifest" not in result["package"]  # type: ignore[operator]
        assert result["structure"]["directory_tree"] == []  # type: ignore[index]
        assert result["agents_directives"] == []
        assert result["dependencies"]["runtime"] == []  # type: ignore[index]

    def test_does_not_throw_when_given_a_path_pointing_at_a_file(
        self, tmp_path: Path
    ) -> None:
        _write_fixture(tmp_path, "not-a-dir.txt", "plain file")
        file_path = os.path.join(str(tmp_path), "not-a-dir.txt")
        result = analyze_codebase(file_path)

        assert str(result["requirements_id"]).startswith("cr-")
        assert result["codebase_path"] == file_path
        assert result["structure"]["directory_tree"] == []  # type: ignore[index]

    def test_all_results_satisfy_structural_invariants(self, tmp_path: Path) -> None:
        _write_fixture(
            tmp_path, "package.json", json.dumps({"name": "invariant-test"})
        )
        result = analyze_codebase(str(tmp_path))

        # Required top-level fields
        assert (
            isinstance(result["requirements_id"], str)
            and len(result["requirements_id"]) > 0
        )
        assert (
            isinstance(result["analyzed_date"], str)
            and len(result["analyzed_date"]) > 0
        )
        assert (
            isinstance(result["codebase_path"], str)
            and len(result["codebase_path"]) > 0
        )

        # package shape
        package = result["package"]
        assert isinstance(package, dict) and package is not None
        assert isinstance(package["language"], str)
        assert isinstance(package["name"], str)
        assert isinstance(package["entry_points"], list)

        # dependencies shape — required array fields are always arrays
        dependencies = result["dependencies"]
        assert isinstance(dependencies, dict) and dependencies is not None
        assert isinstance(dependencies["runtime"], list)
        assert isinstance(dependencies["dev"], list)

        # agents_directives is always an array
        assert isinstance(result["agents_directives"], list)

        # structure.directory_tree is always an array of strings
        structure = result["structure"]
        assert isinstance(structure, dict)
        assert isinstance(structure["directory_tree"], list)
        for entry in structure["directory_tree"]:
            assert isinstance(entry, str), (
                f"directory_tree entry must be string: {entry}"
            )

        # conventions is an object
        conventions = result["conventions"]
        assert isinstance(conventions, dict) and conventions is not None

    def test_generates_distinct_ids_for_distinct_paths(self, tmp_path: Path) -> None:
        root_a = tmp_path / "a"
        root_b = tmp_path / "b"
        root_a.mkdir()
        root_b.mkdir()
        a = analyze_codebase(str(root_a))
        b = analyze_codebase(str(root_b))
        assert a["requirements_id"] != b["requirements_id"], (
            "different paths should produce different ids"
        )

    def test_analyzes_the_real_pipeline_mcp_project_correctly(self) -> None:
        pipeline_mcp_dir = str(LEGACY_NODE_DIR)

        result = analyze_codebase(pipeline_mcp_dir)

        assert str(result["requirements_id"]).startswith("cr-")
        assert result["package"]["name"] == "pipeline-mcp"  # type: ignore[index]
        # No tsconfig.json at root would mean 'javascript'; legacy-node has one,
        # so 'typescript' — the Node assertion allows either JS/TS.
        language = result["package"]["language"]  # type: ignore[index]
        assert language in ("javascript", "typescript"), (
            f"Expected a JS/TS language, got: {language}"
        )
        assert result["package"]["manifest"] == "package.json"  # type: ignore[index]
        assert "tests" in result["structure"]["directory_tree"]  # type: ignore[index]
        # AGENTS.md exists at project root, so directives should be captured
        agents_directives = result["agents_directives"]
        assert isinstance(agents_directives, list)
        assert len(agents_directives) > 0, (
            "expected AGENTS.md directives to be captured"
        )
        testing = result["testing"]
        assert isinstance(testing, dict)
        framework = testing.get("framework")
        assert framework is not None and (
            "node" in framework or "test" in framework
        ), f"Expected node:test framework, got: {framework}"
        runtime_deps = result["dependencies"]["runtime"]  # type: ignore[index]
        assert any("@modelcontextprotocol" in d for d in runtime_deps)
        dev_deps = result["dependencies"]["dev"]  # type: ignore[index]
        assert any("typescript" in d for d in dev_deps)


# ── detectConventions — Python with absolute imports ──────────


class TestDetectConventionsPythonAbsoluteImports:
    def test_correctly_identifies_type_annotations_in_python_source(
        self, tmp_path: Path
    ) -> None:
        _write_fixture(
            tmp_path,
            "pyproject.toml",
            "\n[project]\n"
            'name = "my-pkg"\n'
            'requires-python = ">=3.12"\n'
            'dependencies = ["aiohttp"]\n',
        )
        _write_fixture(
            tmp_path,
            "src/main.py",
            "\nfrom my_pkg.utils import helper\n"
            "from my_pkg.models import Model\n"
            "import os\n"
            "\n"
            "def helper(name: str) -> str:\n"
            "    return name\n",
        )
        _write_fixture(
            tmp_path,
            "src/utils.py",
            "\ndef helper() -> str:\n    return \"hello\"\n",
        )
        conventions = detect_conventions(str(tmp_path), "python")
        assert conventions.type_annotations is True, (
            f"Expected type_annotations to be true, got: "
            f"{conventions.type_annotations}"
        )


# ── analyzeCodebase (integration) ─────────────────────────────


class TestAnalyzeCodebaseIntegration:
    def test_produces_valid_requirements_for_typescript_fixture(
        self, tmp_path: Path
    ) -> None:
        pkg = {
            "name": "my-app",
            "version": "1.0.0",
            "type": "module",
            "engines": {"node": ">=20"},
            "dependencies": {"@modelcontextprotocol/sdk": "^1.0.0"},
            "devDependencies": {"typescript": "^5.0.0"},
            "scripts": {"test": "node --test tests/*.test.ts"},
        }
        _write_fixture(tmp_path, "package.json", json.dumps(pkg))
        _write_fixture(
            tmp_path,
            "tsconfig.json",
            json.dumps({"compilerOptions": {"strict": True}}),
        )
        _write_fixture(
            tmp_path,
            "src/index.ts",
            "import { Server } from '@modelcontextprotocol/sdk'\n"
            "export function main(): void {}\n",
        )
        _write_fixture(
            tmp_path,
            "tests/index.test.ts",
            "import { describe, it } from 'node:test'\n",
        )
        _write_fixture(
            tmp_path,
            "AGENTS.md",
            "# Project Rules\n\n- Use strict TypeScript\n"
            "- Write tests for all exports\n",
        )

        result = analyze_codebase(str(tmp_path))

        # Must have required fields
        assert result["requirements_id"], "should have a requirements_id"
        assert result["analyzed_date"], "should have analyzed_date"
        assert result["codebase_path"] == str(tmp_path)
        assert result["package"]["manifest"] == "package.json"  # type: ignore[index]
        assert result["package"]["language"] == "typescript"  # type: ignore[index]
        assert result["package"]["name"] == "my-app"  # type: ignore[index]
        assert result["conventions"]["type_annotations"] == "required"  # type: ignore[index]
        assert isinstance(result["agents_directives"], list)
        assert isinstance(result["structure"]["directory_tree"], list)  # type: ignore[index]


# ── Additive regression tests (NOT part of the 40 parity cases) ───────
#
# These are author-added integrity checks beyond the oracle's 40, guarding the
# two recurring blocking defects: ``ensure_ascii=False`` JSON parity and the
# undefined-key-drop on absent optional fields.


class TestAdditiveRegression:
    def test_additive_json_dumps_preserves_raw_utf8_ensure_ascii_false(
        self, tmp_path: Path
    ) -> None:
        """ADDITIVE: a non-ASCII directive survives JSON round-trip un-escaped.

        ``JSON.stringify`` emits raw UTF-8; ``json.dumps(..., ensure_ascii=False)``
        must match. A ``\\uXXXX`` escape in the serialized output would be a
        divergence.
        """
        _write_fixture(
            tmp_path,
            "AGENTS.md",
            "# Régles\n\n- Utiliser le café ☕ pour rester éveillé\n",
        )
        result = analyze_codebase(str(tmp_path))
        serialized = json.dumps(result, indent=2, ensure_ascii=False)
        assert "café ☕" in serialized
        assert "\\u" not in serialized

    def test_additive_absent_optionals_are_omitted_not_null(
        self, tmp_path: Path
    ) -> None:
        """ADDITIVE: undefined-key-drop — absent optionals are OMITTED, not null.

        With no manifest, ``runtime`` / ``package_manager`` / ``manifest`` and
        ``src_root`` / ``test_root`` / ``testing.framework`` must be absent keys
        (mirroring ``JSON.stringify`` dropping the spread-omitted properties),
        never serialized as JSON ``null``.
        """
        result = analyze_codebase(str(tmp_path))
        package = result["package"]
        assert isinstance(package, dict)
        assert "runtime" not in package
        assert "package_manager" not in package
        assert "manifest" not in package
        structure = result["structure"]
        assert isinstance(structure, dict)
        assert "src_root" not in structure
        assert "test_root" not in structure
        testing = result["testing"]
        assert isinstance(testing, dict)
        assert "framework" not in testing
        # And nothing serialized as null.
        serialized = json.dumps(result, ensure_ascii=False)
        assert ": null" not in serialized
