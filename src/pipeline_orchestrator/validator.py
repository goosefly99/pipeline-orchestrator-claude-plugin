"""JSON-Schema artifact validator (port of ``validator.ts``; spec §7.5).

Byte-exact behavioral port of the Node ``validator.ts`` module. The Node side
uses Ajv 2020 (``allErrors: true, strict: false``) + ``ajv-formats``; this port
uses :class:`jsonschema.Draft202012Validator` with a
:class:`jsonschema.FormatChecker` so ``format`` keywords (``date-time`` etc.)
actually validate rather than being silent annotations.

Surface mirrors the Node module 1:1:

* :func:`load_schemas` — list ``*.json`` files in a dir and ``json.loads`` each
  into ``{filename: schema}`` (mirrors ``readdirSync(...).filter(.json)``).
* :func:`validate_artifact` — resolve the schema by filename, compile a
  ``Draft202012Validator`` **once per ``schema_file``** (module-level
  ``_VALIDATOR_CACHE``, mirroring Ajv's ``validatorCache`` Map), collect **all**
  errors via ``iter_errors`` (the ``allErrors: true`` equivalent), and return a
  :class:`ValidationResult` ``{valid, errors}``.

``strict: false`` in the Node Ajv config means unknown keywords are ignored;
jsonschema does that by default, so we deliberately do **not** call
``check_schema`` / enable any strict meta-validation.

Error-string format mirrors the Node code as closely as the two libraries allow.
Node builds ``f"{instancePath}: {message} ({JSON.stringify(params)})"`` where
``instancePath`` is a JSON Pointer (``/items/0/name``, ``''`` -> ``/`` at root)
and ``params`` is Ajv's keyword-specific param object. jsonschema does not expose
Ajv's ``params`` shape, so we reproduce the ``"{pointer}: {message}"`` core
faithfully and append a best-effort, Ajv-``params``-flavored suffix built from
the jsonschema error's keyword + keyword value (``({"<validator>": <value>})``).
The five ported test cases do not pin exact strings — they assert valid/invalid
and a non-empty error list on invalid — so this suffix is informational only.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jsonschema import Draft202012Validator, FormatChecker

if TYPE_CHECKING:
    from collections.abc import Iterable

    from jsonschema.exceptions import ValidationError

# A schema map: filename -> parsed schema object (mirrors the Node ``SchemaMap``).
SchemaMap = dict[str, dict[str, Any]]


@dataclass(frozen=True)
class ValidationResult:
    """Result of validating an artifact (mirrors the Node ``ValidationResult``).

    ``valid`` is true iff ``errors`` is empty; ``errors`` collects **every**
    validation failure (the Ajv ``allErrors: true`` equivalent), one readable
    string per error.
    """

    valid: bool
    errors: list[str] = field(default_factory=list)


# Module-level cache of compiled validators, keyed by ``schema_file`` — mirrors
# the Node module-level ``validatorCache`` Map. Compile once, reuse thereafter.
_VALIDATOR_CACHE: dict[str, Draft202012Validator] = {}


def load_schemas(schemas_dir: str | Path) -> SchemaMap:
    """Load every ``*.json`` schema in ``schemas_dir`` into ``{filename: schema}``.

    Mirrors the Node ``loadSchemas``: list the directory, keep only ``.json``
    files, read + ``json.loads`` each. The map key is the bare filename (e.g.
    ``raw-collection.json``), matching the Node ``schemas[file]`` keying.
    """
    directory = Path(schemas_dir)
    schemas: SchemaMap = {}
    for path in sorted(directory.glob("*.json")):
        schemas[path.name] = json.loads(path.read_text(encoding="utf-8"))
    return schemas


def _instance_pointer(error: ValidationError) -> str:
    """Build an Ajv-``instancePath``-style JSON Pointer for a jsonschema error.

    Ajv's ``instancePath`` is a JSON Pointer like ``/items/0/name`` and the empty
    string at the document root; the Node code maps ``'' -> '/'``. jsonschema's
    ``absolute_path`` is the deque of instance path components, so we join them
    into a pointer and fall back to ``'/'`` at the root.
    """
    parts = list(error.absolute_path)
    if not parts:
        return "/"
    return "".join(f"/{part}" for part in parts)


def _format_error(error: ValidationError) -> str:
    """Render one jsonschema error as ``"{pointer}: {message}{params}"``.

    The ``{params}`` suffix is the best-effort Ajv-``params`` analogue described
    in the module docstring: ``({"<validator>": <validator_value>})`` built from
    the failing keyword and its schema value, JSON-serialized. It is omitted when
    the keyword value is not JSON-serializable.
    """
    pointer = _instance_pointer(error)
    message = error.message or "unknown error"
    suffix = ""
    if error.validator is not None:
        try:
            params = json.dumps({str(error.validator): error.validator_value})
        except (TypeError, ValueError):
            params = ""
        if params:
            suffix = f" ({params})"
    return f"{pointer}: {message}{suffix}"


def validate_artifact(
    schemas: SchemaMap,
    schema_file: str,
    artifact: object,
) -> ValidationResult:
    """Validate ``artifact`` against ``schemas[schema_file]`` (spec §7.5).

    Mirrors the Node ``validateArtifact``:

    * Raise when ``schema_file`` is absent — with the byte-exact Node message
      ``Schema "{schema_file}" not found. Available: {keys}`` (keys joined by
      ``", "`` in insertion order). The exception type is :class:`ValueError`,
      matching the sibling ``storage.py`` ``_resolve_dir`` idiom (which raises
      ``ValueError`` with the parallel ``Unknown storage key "..." Available:
      ...`` message for the same kind of bad-lookup). The Node code throws a
      plain ``Error``; ``ValueError`` is the closest faithful analogue and keeps
      the message intact (satisfying the test's ``/not found/i`` match).
    * Compile a ``Draft202012Validator(schema, format_checker=FormatChecker())``
      once per ``schema_file`` (module-level cache).
    * Collect **all** errors via ``iter_errors`` (Ajv ``allErrors: true``).
    * ``valid = (len(errors) == 0)``.
    """
    if schema_file not in schemas:
        available = ", ".join(schemas.keys())
        raise ValueError(f'Schema "{schema_file}" not found. Available: {available}')

    validator = _VALIDATOR_CACHE.get(schema_file)
    if validator is None:
        validator = Draft202012Validator(
            schemas[schema_file], format_checker=FormatChecker()
        )
        _VALIDATOR_CACHE[schema_file] = validator

    found_errors: Iterable[ValidationError] = validator.iter_errors(artifact)
    errors = [_format_error(error) for error in found_errors]
    return ValidationResult(valid=len(errors) == 0, errors=errors)
