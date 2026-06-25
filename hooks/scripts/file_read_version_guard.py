#!/usr/bin/env python3
"""Block reads of files with outdated version tags in their paths — PreToolUse hook."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from hooks.lib.common import load_config, read_stdin  # noqa: E402

_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = Path(
    os.environ.get("PIPELINE_RUNTIME_CONFIG")
    or (_ROOT / "hooks" / "runtime-config.json")
)


def normalize_version_patterns(version: str) -> list[str]:
    """Normalize a version string to comparable forms.

    ``"v0.3.0"`` -> ``["v0.3.0", "v0-3-0", "0.3.0", "0-3-0"]``.
    """
    stripped = version[1:] if version.startswith("v") else version
    dotted = stripped.replace("-", ".")
    dashed = stripped.replace(".", "-")
    patterns: set[str] = {f"v{dotted}", f"v{dashed}", dotted, dashed}
    return list(patterns)


def main() -> None:
    raw_input = read_stdin()

    try:
        default: dict[str, Any] = {
            "file_read_version_guard": {
                "present_version_in_development": "",
                "previous_versions": [],
            }
        }
        config = {**default, **load_config(CONFIG_PATH)}

        guard_config: dict[str, Any] = config.get("file_read_version_guard") or {}
        current_version = guard_config.get("present_version_in_development") or ""
        previous_versions: list[Any] = guard_config.get("previous_versions") or []

        if not current_version or len(previous_versions) == 0:
            # No version guard configured — allow all reads.
            sys.stdout.write(json.dumps({"permissionDecision": "allow"}))
            sys.exit(0)

        hook_input: Any = json.loads(raw_input or "{}")
        tool_input: dict[str, Any] = hook_input.get("tool_input") or {}
        file_path = tool_input.get("file_path") or tool_input.get("path") or ""

        if not file_path:
            sys.stdout.write(json.dumps({"permissionDecision": "allow"}))
            sys.exit(0)

        # Normalize first via os.path.normpath, THEN case-fold and unify
        # slashes. Any remaining '..' segment after normalization is rejected
        # — this closes the bypass where a crafted relative path could encode
        # an old-version read while appearing to contain the current version
        # string.
        path_normalized = os.path.normpath(file_path)
        if ".." in path_normalized:
            sys.stdout.write(
                json.dumps(
                    {
                        "permissionDecision": "deny",
                        "deny_reason": (
                            "file-read-version-guard: path contains "
                            "unresolved '..' after normalization — refusing "
                            f'to read "{file_path}"'
                        ),
                    }
                )
            )
            sys.exit(0)
        normalized_path = path_normalized.lower().replace("\\", "/")

        # Check if the file path contains any outdated version patterns.
        for old_version in previous_versions:
            for pattern in normalize_version_patterns(old_version):
                if pattern.lower() in normalized_path:
                    # Check it's not actually the current version.
                    is_current_version = any(
                        cp.lower() in normalized_path
                        for cp in normalize_version_patterns(current_version)
                    )
                    if not is_current_version:
                        sys.stdout.write(
                            json.dumps(
                                {
                                    "permissionDecision": "deny",
                                    "deny_reason": (
                                        "File path contains outdated version "
                                        f'"{old_version}". Current version in '
                                        f'development is "{current_version}". '
                                        "Read the current version file "
                                        "instead."
                                    ),
                                }
                            )
                        )
                        sys.exit(0)

        sys.stdout.write(json.dumps({"permissionDecision": "allow"}))
    except Exception:
        sys.stdout.write(json.dumps({"permissionDecision": "allow"}))


if __name__ == "__main__":
    main()
