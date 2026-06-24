r"""Codebase analyzer (port of ``legacy-node/codebase-analyzer.ts``).

Byte-exact behavioral port of the Node ``codebase-analyzer.ts`` module backing
the eventual ``pipeline_analyze_codebase`` tool (the FastMCP seam lands later in
T6.4 — this module registers nothing). Covers the static directory walk
(:func:`walk_directory_tree`), manifest detection in priority order
(:func:`detect_manifest`), the four manifest parsers (:func:`parse_manifest` →
package.json / pyproject.toml / Cargo.toml / go.mod, each surfacing a
``parse_error`` on malformed/missing input), ``AGENTS.md`` / ``CLAUDE.md``
directive extraction (:func:`read_agents_directives`), per-language convention
detection (:func:`detect_conventions`), and the top-level entry
(:func:`analyze_codebase`).

Parity notes:

* **Typed intermediates are frozen dataclasses; the artifact is a plain dict.**
  The four helper functions return typed, sole-produced results
  (:class:`ManifestDetection`, :class:`ManifestParsed`,
  :class:`ConventionDetection`, :class:`AgentsDirective`) — frozen dataclasses,
  matching the §6 sibling guidance. The top-level :func:`analyze_codebase`
  result, by contrast, is the JSON ``codebase-requirements`` artifact, so it is
  built as a plain insertion-ordered ``dict`` mirroring the TS object-literal
  key order EXACTLY (like the ``ingest.py`` / ``web_search.py`` collections),
  keeping ``json.dumps(result, indent=2, ensure_ascii=False)`` byte-equal to the
  Node ``JSON.stringify(result, null, 2)``.

* **Undefined-key-drop.** The TS ``analyzeCodebase`` uses the
  ``...(x !== null && { key: x })`` spread idiom: when the source is ``null``
  the key is OMITTED from the object (and so absent from the JSON), NOT emitted
  as ``null``. The port reproduces this by conditionally inserting the key into
  the dict only when the source value is not ``None``. ``ManifestParsed`` mirrors
  the TS ``parse_error?`` optional the same way: the key is present ONLY when an
  error occurred (matching ``JSON.stringify`` dropping the ``undefined`` field,
  and the test's ``parse_error === undefined`` assertion).

* **Nullish-coalescing, not falsy.** Every TS ``?? default`` / ``!== null`` /
  ``typeof === 'string'`` check becomes an explicit ``is None`` / ``isinstance``
  test, never ``or`` — a present-but-falsy value (``""``, ``0``, ``False``,
  ``[]``) survives exactly as in the TS.

* **``cr-`` id.** ``createHash('sha256').update(`${path}:${dateDay}`)`` →
  ``hashlib.sha256(f"{path}:{date_day}".encode()).hexdigest()``; ``cr-`` +
  first 8 hex chars. ``analyzed_date`` = ``new Date().toISOString()``
  (millisecond ISO 8601, reused from :func:`ingest._now_iso`); ``date_day`` is
  the first 10 chars (``YYYY-MM-DD``), giving day-stable ids.

* **Filesystem.** Node ``readdirSync`` / ``statSync`` / ``readFileSync`` /
  ``existsSync`` → ``os.listdir`` / ``os.stat`` / ``open(...).read`` /
  ``os.path.exists``. Errors are swallowed exactly where the TS ``try/catch``
  swallows them (unreadable dirs/files are skipped; a nonexistent root yields an
  empty walk). ``path.relative(root, p).replace(/\\/g, '/')`` →
  ``os.path.relpath`` with backslashes normalized to forward slashes (a no-op on
  POSIX, faithful on Windows). ``path.extname`` → ``os.path.splitext(...)[1]``.

* **Regex fidelity.** The TS ``String.match`` (no ``g``) searches anywhere and
  returns capture groups → ``re.search``; the ``/m`` flag → :data:`re.MULTILINE`;
  ``[\s\S]`` (match-anything incl. newlines) is kept verbatim rather than relying
  on :data:`re.DOTALL`. The ``g``/``mg`` ``matchAll`` loops → ``re.finditer``.

* **MCP-stdio safety.** Nothing here writes to stdout at import or runtime.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
from dataclasses import dataclass, field

from .ingest import _now_iso

# ── Typed result dataclasses (sole-produced; §6 frozen-dataclass guidance) ──


@dataclass(frozen=True)
class ManifestDetection:
    """Detected manifest, language, and package manager (TS ``ManifestDetection``)."""

    manifest: str | None
    language: str
    package_manager: str | None


@dataclass(frozen=True)
class ManifestParsed:
    """Parsed manifest contents (TS ``ManifestParsed``).

    ``parse_error`` mirrors the TS optional ``parse_error?``: it is ``None``
    (absent, the TS ``undefined``) on success and a message string on failure.
    The JSON artifact omits the key entirely when it is ``None`` (undefined-drop).
    """

    name: str
    runtime: str | None
    runtime_deps: list[str]
    dev_deps: list[str]
    entry_points: list[str]
    test_script: str | None
    parse_error: str | None = None


def _empty_manifest(parse_error: str | None = None) -> ManifestParsed:
    """The blank :class:`ManifestParsed` (TS ``emptyManifest``)."""
    return ManifestParsed(
        name="",
        runtime=None,
        runtime_deps=[],
        dev_deps=[],
        entry_points=[],
        test_script=None,
        parse_error=parse_error,
    )


@dataclass(frozen=True)
class AgentsDirective:
    """A directive file + its extracted bullet directives (TS ``AgentsDirective``)."""

    file: str
    scope: str
    directives: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ConventionDetection:
    """Detected source conventions (TS ``ConventionDetection``).

    ``import_style`` is one of ``"esm"`` / ``"commonjs"`` / ``"unknown"``.
    """

    import_style: str
    type_annotations: bool
    test_framework: str | None
    src_root: str | None
    test_root: str | None


# ── Constants ────────────────────────────────────────────────

EXCLUDED_DIRS: frozenset[str] = frozenset(
    {
        "node_modules",
        ".git",
        ".svn",
        ".hg",
        "dist",
        "build",
        "out",
        "__pycache__",
        ".venv",
        "venv",
        ".env",
        ".tox",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "target",
        ".next",
        ".nuxt",
        "coverage",
        ".nyc_output",
    }
)

MANIFEST_PRIORITY: list[str] = [
    "package.json",
    "pyproject.toml",
    "Cargo.toml",
    "go.mod",
]


# ── Internal helpers ─────────────────────────────────────────


def _normalize_path(root: str, full_path: str) -> str:
    """``relative(root, p).replace(/\\/g, '/')`` (TS ``normalizePath``)."""
    return os.path.relpath(full_path, root).replace("\\", "/")


def _is_excluded(name: str) -> bool:
    """``EXCLUDED_DIRS.has(name) || name.startsWith('.')`` (TS ``isExcluded``)."""
    return name in EXCLUDED_DIRS or name.startswith(".")


def _list_dir(path: str) -> list[str] | None:
    """``readdirSync(path)`` or ``None`` on error (TS try/catch return).

    Node's ``fs.readdirSync`` (libuv ``scandir``) returns entries sorted by
    byte/code-point order; Python's ``os.listdir`` returns them in arbitrary
    filesystem order. ``sorted`` restores byte-exact array parity (UTF-8 byte
    order == Python str code-point order) for ``directory_tree``,
    ``agents_directives``, and the convention source-file sample.
    """
    try:
        return sorted(os.listdir(path))
    except OSError:
        return None


def _collect_source_files(
    root: str, extensions: list[str], max_files: int
) -> list[str]:
    """Collect up to ``max_files`` files with the given extensions.

    Skips excluded directories; swallows unreadable dirs/entries exactly where
    the TS ``try/catch`` does.
    """
    results: list[str] = []

    def walk(directory: str) -> None:
        if len(results) >= max_files:
            return
        entries = _list_dir(directory)
        if entries is None:
            return
        for entry in entries:
            if len(results) >= max_files:
                return
            full = os.path.join(directory, entry)
            try:
                st = os.stat(full)
            except OSError:
                continue
            if stat.S_ISDIR(st.st_mode):
                if not _is_excluded(entry):
                    walk(full)
            elif stat.S_ISREG(st.st_mode):
                ext = os.path.splitext(entry)[1]
                if ext in extensions:
                    results.append(full)

    walk(root)
    return results


def _collect_import_lines(files: list[str]) -> list[str]:
    """Extract import/require lines from source files (TS ``collectImportLines``)."""
    lines: list[str] = []
    for file in files:
        try:
            with open(file, encoding="utf-8") as fh:
                content = fh.read()
        except OSError:
            continue
        for line in content.split("\n"):
            trimmed = line.strip()
            if (
                trimmed.startswith("import ")
                or trimmed.startswith("from ")
                or trimmed.startswith("require(")
                or "require('" in trimmed
                or 'require("' in trimmed
            ):
                lines.append(trimmed)
    return lines


_BULLET_RE = re.compile(r"^[-*]\s+(.+)$")
_EMDASH_PREFIX_RE = re.compile(r"^—\s*")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_ITALIC_STAR_RE = re.compile(r"\*(.+?)\*")
_ITALIC_USCORE_RE = re.compile(r"_(.+?)_")
_INLINE_CODE_RE = re.compile(r"`(.+?)`")


def _extract_bullet_directives(content: str) -> list[str]:
    """Parse markdown bullets and strip inline formatting.

    Handles ``-`` and ``*`` list markers; strips an em-dash prefix, then
    bold/italic/underscore-italic/inline-code, then trims; drops empties.
    """
    directives: list[str] = []
    for line in content.split("\n"):
        trimmed = line.strip()
        match = _BULLET_RE.match(trimmed)
        if match:
            text = match.group(1)
            text = _EMDASH_PREFIX_RE.sub("", text)
            text = _BOLD_RE.sub(r"\1", text)
            text = _ITALIC_STAR_RE.sub(r"\1", text)
            text = _ITALIC_USCORE_RE.sub(r"\1", text)
            text = _INLINE_CODE_RE.sub(r"\1", text)
            text = text.strip()
            if len(text) > 0:
                directives.append(text)
    return directives


# ── walkDirectoryTree ────────────────────────────────────────


def walk_directory_tree(root: str) -> list[str]:
    """Recursively walk ``root``, returning relative subdir paths.

    Skips excluded dirs (the :data:`EXCLUDED_DIRS` set + any dot-prefixed name).
    Returns forward-slashed paths relative to ``root``; an unreadable/nonexistent
    root yields ``[]``.
    """
    results: list[str] = []

    def walk(directory: str) -> None:
        entries = _list_dir(directory)
        if entries is None:
            return
        for entry in entries:
            if _is_excluded(entry):
                continue
            full = os.path.join(directory, entry)
            try:
                st = os.stat(full)
            except OSError:
                continue
            if stat.S_ISDIR(st.st_mode):
                results.append(_normalize_path(root, full))
                walk(full)

    walk(root)
    return results


# ── detectManifest ───────────────────────────────────────────


def detect_manifest(root: str) -> ManifestDetection:
    """Detect manifest, language, and package manager (TS ``detectManifest``)."""
    for manifest in MANIFEST_PRIORITY:
        if os.path.exists(os.path.join(root, manifest)):
            if manifest == "package.json":
                language = (
                    "typescript"
                    if os.path.exists(os.path.join(root, "tsconfig.json"))
                    else "javascript"
                )
                package_manager: str | None = "npm"
                if os.path.exists(os.path.join(root, "pnpm-lock.yaml")):
                    package_manager = "pnpm"
                elif os.path.exists(os.path.join(root, "yarn.lock")):
                    package_manager = "yarn"
                elif os.path.exists(
                    os.path.join(root, "bun.lockb")
                ) or os.path.exists(os.path.join(root, "bun.lock")):
                    package_manager = "bun"
                elif os.path.exists(os.path.join(root, "package-lock.json")):
                    package_manager = "npm"
                return ManifestDetection(
                    manifest=manifest,
                    language=language,
                    package_manager=package_manager,
                )

            if manifest == "pyproject.toml":
                py_pm: str | None = None
                if os.path.exists(os.path.join(root, "uv.lock")):
                    py_pm = "uv"
                elif os.path.exists(os.path.join(root, "poetry.lock")):
                    py_pm = "poetry"
                elif os.path.exists(os.path.join(root, "Pipfile.lock")):
                    py_pm = "pipenv"
                return ManifestDetection(
                    manifest=manifest, language="python", package_manager=py_pm
                )

            if manifest == "Cargo.toml":
                return ManifestDetection(
                    manifest=manifest, language="rust", package_manager="cargo"
                )

            if manifest == "go.mod":
                return ManifestDetection(
                    manifest=manifest, language="go", package_manager="go"
                )

    return ManifestDetection(manifest=None, language="unknown", package_manager=None)


# ── parseManifest ────────────────────────────────────────────


def parse_manifest(root: str, manifest: str) -> ManifestParsed:
    """Parse the detected manifest (TS ``parseManifest``)."""
    manifest_path = os.path.join(root, manifest)

    if manifest == "package.json":
        return _parse_package_json(manifest_path)
    if manifest == "pyproject.toml":
        return _parse_pyproject_toml(manifest_path)
    if manifest == "Cargo.toml":
        return _parse_cargo_toml(manifest_path)
    if manifest == "go.mod":
        return _parse_go_mod(manifest_path)

    return _empty_manifest(f'No parser registered for manifest "{manifest}"')


def _read_text(path: str) -> str:
    """Read ``path`` as UTF-8 (TS ``readFileSync(path, 'utf8')``)."""
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _parse_package_json(file_path: str) -> ManifestParsed:
    """Parse ``package.json`` (TS ``parsePackageJson``)."""
    import json

    try:
        raw = _read_text(file_path)
        pkg = json.loads(raw)
    except OSError as err:
        return _empty_manifest(
            f'Failed to parse package.json at "{file_path}": {_err_msg(err)}'
        )
    except json.JSONDecodeError as err:
        return _empty_manifest(
            f'Failed to parse package.json at "{file_path}": {_err_msg(err)}'
        )

    if not isinstance(pkg, dict):
        pkg = {}

    name = pkg["name"] if isinstance(pkg.get("name"), str) else ""

    runtime: str | None = None
    engines = pkg.get("engines")
    if isinstance(engines, dict):
        node = engines.get("node")
        # TS: ``if (engines.node) runtime = engines.node`` — truthy check (a
        # falsy ``""`` would not set runtime). Reproduce the truthiness here.
        if node:
            runtime = node

    runtime_deps = _deps_to_list(pkg.get("dependencies"))
    dev_deps = _deps_to_list(pkg.get("devDependencies"))

    entry_points: list[str] = []
    if isinstance(pkg.get("main"), str):
        entry_points.append(pkg["main"])
    if isinstance(pkg.get("bin"), str):
        entry_points.append(pkg["bin"])

    test_script: str | None = None
    scripts = pkg.get("scripts")
    if isinstance(scripts, dict):
        test = scripts.get("test")
        # TS: ``if (scripts.test) test_script = scripts.test`` — truthy check.
        if test:
            test_script = test

    return ManifestParsed(
        name=name,
        runtime=runtime,
        runtime_deps=runtime_deps,
        dev_deps=dev_deps,
        entry_points=entry_points,
        test_script=test_script,
    )


def _deps_to_list(deps: object) -> list[str]:
    """``Object.entries(deps).map([n, v] => `${n}@${v}`)`` (TS ``depsToList``)."""
    if not isinstance(deps, dict):
        return []
    return [f"{name}@{version}" for name, version in deps.items()]


_PYPROJECT_NAME_RE = re.compile(
    r"^\[project\][\s\S]*?^name\s*=\s*[\"']([^\"']+)[\"']", re.MULTILINE
)
_REQUIRES_PYTHON_RE = re.compile(r"requires-python\s*=\s*[\"']([^\"']+)[\"']")
_PYPROJECT_DEPS_RE = re.compile(
    r"^\[project\][\s\S]*?^dependencies\s*=\s*\[([^\]]*)\]", re.MULTILINE
)
_UV_DEV_RE = re.compile(
    r"\[tool\.uv\.dev-dependencies\]\s*\ndev\s*=\s*\[([^\]]*)\]", re.MULTILINE
)
_UV_DEV_ALT_RE = re.compile(r"\[tool\.uv\][\s\S]*?dev\s*=\s*\[([^\]]*)\]", re.MULTILINE)
_POETRY_DEV_RE = re.compile(
    r"\[tool\.poetry\.dev-dependencies\]([\s\S]*?)(?=\[|$)", re.MULTILINE
)
_PROJECT_SCRIPTS_RE = re.compile(r"\[project\.scripts\]([\s\S]*?)(?=\[|$)")
_SCRIPT_ENTRY_RE = re.compile(r"^\s*\S+\s*=\s*[\"']([^\"']+)[\"']", re.MULTILINE)
_TOML_QUOTED_RE = re.compile(r"[\"']([^\"']+)[\"']")
_TOML_KV_RE = re.compile(r"^\s*(\S+)\s*=\s*[\"']([^\"']+)[\"']")


def _parse_pyproject_toml(file_path: str) -> ManifestParsed:
    """Parse ``pyproject.toml`` (TS ``parsePyprojectToml``)."""
    try:
        content = _read_text(file_path)
    except OSError as err:
        return _empty_manifest(
            f'Failed to read pyproject.toml at "{file_path}": {_err_msg(err)}'
        )

    name_match = _PYPROJECT_NAME_RE.search(content)
    name = name_match.group(1) if name_match else ""

    runtime_match = _REQUIRES_PYTHON_RE.search(content)
    runtime = runtime_match.group(1) if runtime_match else None

    runtime_deps = _extract_toml_string_array(content, _PYPROJECT_DEPS_RE)

    dev_deps: list[str] = []
    uv_dev_match = _UV_DEV_RE.search(content)
    if uv_dev_match:
        dev_deps = _parse_toml_string_list(uv_dev_match.group(1))
    else:
        uv_dev_alt = _UV_DEV_ALT_RE.search(content)
        if uv_dev_alt:
            dev_deps = _parse_toml_string_list(uv_dev_alt.group(1))
        if len(dev_deps) == 0:
            poetry_dev_match = _POETRY_DEV_RE.search(content)
            if poetry_dev_match:
                dev_deps = _extract_toml_key_value_deps(poetry_dev_match.group(1))

    entry_points: list[str] = []
    scripts_match = _PROJECT_SCRIPTS_RE.search(content)
    if scripts_match:
        for m in _SCRIPT_ENTRY_RE.finditer(scripts_match.group(1)):
            entry_points.append(m.group(1))

    return ManifestParsed(
        name=name,
        runtime=runtime,
        runtime_deps=runtime_deps,
        dev_deps=dev_deps,
        entry_points=entry_points,
        test_script=None,
    )


def _extract_toml_string_array(content: str, pattern: re.Pattern[str]) -> list[str]:
    """Match ``pattern``; ``parseTomlStringList`` (TS ``extractTomlStringArray``)."""
    match = pattern.search(content)
    if not match:
        return []
    return _parse_toml_string_list(match.group(1))


def _parse_toml_string_list(raw: str) -> list[str]:
    """Pull quoted strings out of a TOML array body (TS ``parseTomlStringList``)."""
    return [m.group(1).strip() for m in _TOML_QUOTED_RE.finditer(raw)]


def _extract_toml_key_value_deps(section: str) -> list[str]:
    """``name = "version"`` → ``nameversion`` (TS ``extractTomlKeyValueDeps``)."""
    results: list[str] = []
    for line in section.split("\n"):
        match = _TOML_KV_RE.match(line)
        if match:
            results.append(f"{match.group(1)}{match.group(2)}")
    return results


_CARGO_NAME_RE = re.compile(
    r"^\[package\][\s\S]*?^name\s*=\s*[\"']([^\"']+)[\"']", re.MULTILINE
)
_CARGO_SECTION_SPLIT_RE = re.compile(r"(?=^\[)", re.MULTILINE)
_CARGO_SIMPLE_RE = re.compile(r"^([a-zA-Z0-9_-]+)\s*=\s*[\"']([^\"']+)[\"']")
_CARGO_TABLE_RE = re.compile(r"^([a-zA-Z0-9_-]+)\s*=\s*\{")
_CARGO_VERSION_RE = re.compile(r"version\s*=\s*[\"']([^\"']+)[\"']")


def _parse_cargo_toml(file_path: str) -> ManifestParsed:
    """Parse ``Cargo.toml`` (TS ``parseCargoToml``)."""
    try:
        content = _read_text(file_path)
    except OSError as err:
        return _empty_manifest(
            f'Failed to read Cargo.toml at "{file_path}": {_err_msg(err)}'
        )

    name_match = _CARGO_NAME_RE.search(content)
    name = name_match.group(1) if name_match else ""

    sections = _CARGO_SECTION_SPLIT_RE.split(content)

    dep_section = next(
        (s for s in sections if s.startswith("[dependencies]")), ""
    )
    runtime_deps = _extract_cargo_deps_from_section(dep_section)

    dev_dep_section = next(
        (s for s in sections if s.startswith("[dev-dependencies]")), ""
    )
    dev_deps = _extract_cargo_deps_from_section(dev_dep_section)

    return ManifestParsed(
        name=name,
        runtime=None,
        runtime_deps=runtime_deps,
        dev_deps=dev_deps,
        entry_points=[],
        test_script=None,
    )


def _extract_cargo_deps_from_section(section: str) -> list[str]:
    """Pull Cargo deps from a section (TS ``extractCargoDepsFromSection``)."""
    results: list[str] = []
    if not section:
        return results

    lines = section.split("\n")[1:]
    for line in lines:
        trimmed = line.strip()
        if not trimmed or trimmed.startswith("#") or trimmed.startswith("["):
            continue
        simple_match = _CARGO_SIMPLE_RE.match(trimmed)
        table_match = _CARGO_TABLE_RE.match(trimmed)
        if simple_match:
            results.append(f"{simple_match.group(1)}@{simple_match.group(2)}")
        elif table_match:
            version_match = _CARGO_VERSION_RE.search(trimmed)
            if version_match:
                results.append(f"{table_match.group(1)}@{version_match.group(1)}")
            else:
                results.append(table_match.group(1))
    return results


_GO_MODULE_RE = re.compile(r"^module\s+(\S+)", re.MULTILINE)
_GO_VERSION_RE = re.compile(r"^go\s+(\S+)", re.MULTILINE)
_GO_SINGLE_REQUIRE_RE = re.compile(r"^require\s+(\S+)\s+(\S+)$", re.MULTILINE)
_GO_BLOCK_REQUIRE_RE = re.compile(r"^require\s*\(([\s\S]*?)\)", re.MULTILINE)
_WHITESPACE_SPLIT_RE = re.compile(r"\s+")


def _parse_go_mod(file_path: str) -> ManifestParsed:
    """Parse ``go.mod`` (TS ``parseGoMod``)."""
    try:
        content = _read_text(file_path)
    except OSError as err:
        return _empty_manifest(
            f'Failed to read go.mod at "{file_path}": {_err_msg(err)}'
        )

    module_match = _GO_MODULE_RE.search(content)
    name = module_match.group(1) if module_match else ""

    go_match = _GO_VERSION_RE.search(content)
    runtime = f"go {go_match.group(1)}" if go_match else None

    runtime_deps: list[str] = []

    for match in _GO_SINGLE_REQUIRE_RE.finditer(content):
        runtime_deps.append(f"{match.group(1)}@{match.group(2)}")

    for block_match in _GO_BLOCK_REQUIRE_RE.finditer(content):
        for line in block_match.group(1).split("\n"):
            trimmed = line.strip()
            if not trimmed or trimmed.startswith("//"):
                continue
            parts = _WHITESPACE_SPLIT_RE.split(trimmed)
            if len(parts) >= 2:
                runtime_deps.append(f"{parts[0]}@{parts[1]}")

    return ManifestParsed(
        name=name,
        runtime=runtime,
        runtime_deps=runtime_deps,
        dev_deps=[],
        entry_points=[],
        test_script="go test ./...",
    )


def _err_msg(err: BaseException) -> str:
    """``err instanceof Error ? err.message : String(err)`` (TS error coercion).

    Python exceptions always carry a message via ``str(err)``; this mirrors the
    TS ``.message`` extraction so the surfaced ``parse_error`` reads naturally.
    """
    return str(err)


# ── readAgentsDirectives ─────────────────────────────────────

_DIRECTIVE_FILENAMES: frozenset[str] = frozenset({"AGENTS.md", "CLAUDE.md"})


def read_agents_directives(root: str) -> list[AgentsDirective]:
    """Find ``AGENTS.md`` / ``CLAUDE.md`` files and extract directives.

    Walks the tree (skipping excluded dirs), reads each directive file, strips
    markdown formatting from its bullets, and records the file path + scope (the
    directory relative to ``root``, ``.`` for the root). Unreadable files are
    skipped.
    """
    results: list[AgentsDirective] = []

    def walk(directory: str) -> None:
        entries = _list_dir(directory)
        if entries is None:
            return
        for entry in entries:
            full = os.path.join(directory, entry)
            try:
                st = os.stat(full)
            except OSError:
                continue
            if stat.S_ISDIR(st.st_mode):
                if not _is_excluded(entry):
                    walk(full)
            elif stat.S_ISREG(st.st_mode) and entry in _DIRECTIVE_FILENAMES:
                try:
                    with open(full, encoding="utf-8") as fh:
                        content = fh.read()
                except OSError:
                    continue
                directives = _extract_bullet_directives(content)
                scope = _normalize_path(root, os.path.dirname(full)) or "."
                results.append(
                    AgentsDirective(file=full, scope=scope, directives=directives)
                )

    walk(root)
    return results


# ── detectConventions ────────────────────────────────────────

_PY_TYPE_ANNOTATION_RE = re.compile(r"def\s+\w+\([^)]*:\s*\w+")


def detect_conventions(root: str, language: str) -> ConventionDetection:
    """Detect import style / types / framework (TS ``detectConventions``)."""
    import json

    import_style = "unknown"
    type_annotations = False
    test_framework: str | None = None
    src_root: str | None = None
    test_root: str | None = None

    dirs = walk_directory_tree(root)
    if "src" in dirs:
        src_root = "src"
    if "lib" in dirs and not src_root:
        src_root = "lib"
    if "tests" in dirs:
        test_root = "tests"
    if "test" in dirs and not test_root:
        test_root = "test"

    if language in ("typescript", "javascript"):
        pkg_path = os.path.join(root, "package.json")
        if os.path.exists(pkg_path):
            try:
                pkg = json.loads(_read_text(pkg_path))
            except (OSError, json.JSONDecodeError):
                pkg = None
            if isinstance(pkg, dict):
                if pkg.get("type") == "module":
                    import_style = "esm"
                elif pkg.get("type") == "commonjs":
                    import_style = "commonjs"

                scripts = pkg.get("scripts")
                if isinstance(scripts, dict):
                    # TS: ``scripts.test ?? ''`` — nullish, so a present falsy
                    # value survives; but ``scripts.test`` here is a string or
                    # absent, so the absent → '' default is the only case.
                    raw_test = scripts.get("test")
                    test_script = raw_test if raw_test is not None else ""
                    if not isinstance(test_script, str):
                        test_script = ""
                    if (
                        "node --test" in test_script
                        or "node:test" in test_script
                    ):
                        test_framework = "node:test"
                    elif "jest" in test_script:
                        test_framework = "jest"
                    elif "vitest" in test_script:
                        test_framework = "vitest"
                    elif "mocha" in test_script:
                        test_framework = "mocha"

        extensions = (
            [".ts", ".tsx"] if language == "typescript" else [".js", ".mjs", ".cjs"]
        )
        files = _collect_source_files(root, extensions, 20)
        import_lines = _collect_import_lines(files)

        if import_style == "unknown" and len(import_lines) > 0:
            esm_count = sum(1 for line in import_lines if line.startswith("import "))
            cjs_count = sum(1 for line in import_lines if "require(" in line)
            if esm_count > cjs_count:
                import_style = "esm"
            elif cjs_count > 0:
                import_style = "commonjs"

        if language == "typescript":
            type_annotations = True

        if not test_framework:
            test_files = _collect_source_files(root, [".ts", ".js", ".mjs"], 10)
            test_imports = _collect_import_lines(test_files)
            if any("node:test" in line for line in test_imports):
                test_framework = "node:test"
            elif any(
                "jest" in line or "@jest" in line for line in test_imports
            ):
                test_framework = "jest"
            elif any("vitest" in line for line in test_imports):
                test_framework = "vitest"

    if language == "python":
        pyproject_path = os.path.join(root, "pyproject.toml")
        if os.path.exists(pyproject_path):
            try:
                content = _read_text(pyproject_path)
            except OSError:
                content = ""
            if "pytest" in content:
                test_framework = "pytest"
            elif "unittest" in content:
                test_framework = "unittest"

        files = _collect_source_files(root, [".py"], 20)
        import_lines = _collect_import_lines(files)
        if any(
            line.startswith("import ") or line.startswith("from ")
            for line in import_lines
        ):
            import_style = "esm"
        if len(files) > 0:
            for file in files[:5]:
                try:
                    with open(file, encoding="utf-8") as fh:
                        content = fh.read()
                except OSError:
                    continue
                if _PY_TYPE_ANNOTATION_RE.search(content):
                    type_annotations = True
                    break

    if language == "rust":
        import_style = "esm"
        type_annotations = True
        files = _collect_source_files(root, [".rs"], 10)
        for file in files[:5]:
            try:
                with open(file, encoding="utf-8") as fh:
                    content = fh.read()
            except OSError:
                continue
            if "#[test]" in content or "#[cfg(test)]" in content:
                test_framework = "cargo-test"
                break

    return ConventionDetection(
        import_style=import_style,
        type_annotations=type_annotations,
        test_framework=test_framework,
        src_root=src_root,
        test_root=test_root,
    )


# ── analyzeCodebase ──────────────────────────────────────────


def analyze_codebase(codebase_path: str) -> dict[str, object]:
    """Build the codebase-requirements artifact (TS ``analyzeCodebase``).

    Returns a plain insertion-ordered ``dict`` whose key order mirrors the TS
    object literal EXACTLY (so ``json.dumps(..., indent=2, ensure_ascii=False)``
    is byte-equal to the Node ``JSON.stringify(..., null, 2)``). The TS
    ``...(x !== null && { key: x })`` spreads are reproduced by conditionally
    inserting the key only when the source value is not ``None`` (undefined-drop).
    """
    analyzed_date = _now_iso()

    date_day = analyzed_date[:10]
    digest = hashlib.sha256(f"{codebase_path}:{date_day}".encode()).hexdigest()
    requirements_id = f"cr-{digest[:8]}"

    directory_tree = walk_directory_tree(codebase_path)
    manifest = detect_manifest(codebase_path)
    parsed = (
        parse_manifest(codebase_path, manifest.manifest)
        if manifest.manifest
        else _empty_manifest()
    )
    agents_directives = read_agents_directives(codebase_path)
    conventions = detect_conventions(codebase_path, manifest.language)

    package: dict[str, object] = {
        "name": parsed.name,
        "language": manifest.language,
    }
    if parsed.runtime is not None:
        package["runtime"] = parsed.runtime
    if manifest.package_manager is not None:
        package["package_manager"] = manifest.package_manager
    if manifest.manifest is not None:
        package["manifest"] = manifest.manifest
    package["entry_points"] = parsed.entry_points

    structure: dict[str, object] = {}
    if conventions.src_root is not None:
        structure["src_root"] = conventions.src_root
    if conventions.test_root is not None:
        structure["test_root"] = conventions.test_root
    structure["directory_tree"] = directory_tree

    testing: dict[str, object] = {}
    if conventions.test_framework is not None:
        testing["framework"] = conventions.test_framework

    return {
        "requirements_id": requirements_id,
        "codebase_path": codebase_path,
        "analyzed_date": analyzed_date,
        "package": package,
        "structure": structure,
        "conventions": {
            "import_style": conventions.import_style,
            "type_annotations": (
                "required" if conventions.type_annotations else "optional"
            ),
        },
        "dependencies": {
            "runtime": parsed.runtime_deps,
            "dev": parsed.dev_deps,
        },
        "agents_directives": [
            {
                "file": d.file,
                "scope": d.scope,
                "directives": d.directives,
            }
            for d in agents_directives
        ],
        "testing": testing,
    }
