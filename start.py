#!/usr/bin/env python3
"""Python entrypoint for the pipeline-orchestrator MCP server.

The orchestration core is still the TypeScript server. This launcher makes the
plugin install/start path Python-native while preserving stdio behavior by
`exec`-ing the existing Node process after dependency checks.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Iterable


PLUGIN_ROOT = Path(__file__).resolve().parent

RUNTIME_FILES = (
    Path("node_modules/@modelcontextprotocol/sdk/dist/esm/server/index.js"),
    Path("node_modules/@modelcontextprotocol/sdk/dist/esm/server/stdio.js"),
    Path("node_modules/@modelcontextprotocol/sdk/dist/esm/types.js"),
    Path("node_modules/ajv/package.json"),
    Path("node_modules/ajv-formats/package.json"),
    Path("node_modules/pdf-parse/package.json"),
    Path("node_modules/smol-toml/package.json"),
)

ZOD_PACKAGE_JSON = {
    "name": "zod",
    "private": True,
    "type": "module",
    "exports": {
        ".": {
            "import": "./v4/classic/index.js",
            "require": "./v4/classic/index.cjs",
        },
        "./v3": {
            "import": "./v4/classic/index.js",
            "require": "./v4/classic/index.cjs",
        },
        "./v4": {
            "import": "./v4/classic/index.js",
            "require": "./v4/classic/index.cjs",
        },
        "./v4/classic": {
            "import": "./v4/classic/index.js",
            "require": "./v4/classic/index.cjs",
        },
        "./v4/core": {
            "import": "./v4/core/index.js",
            "require": "./v4/core/index.cjs",
        },
        "./v4-mini": {
            "import": "./v4/mini/index.js",
            "require": "./v4/mini/index.cjs",
        },
        "./v4/mini": {
            "import": "./v4/mini/index.js",
            "require": "./v4/mini/index.cjs",
        },
        "./mini": {
            "import": "./v4/mini/index.js",
            "require": "./v4/mini/index.cjs",
        },
    },
}


def missing_runtime_files(plugin_root: Path) -> list[Path]:
    """Return dependency sentinel files absent from the plugin checkout."""
    return [path for path in RUNTIME_FILES if not (plugin_root / path).exists()]


def require_executable(name: str) -> str:
    executable = shutil.which(name)
    if executable is None:
        raise RuntimeError(f'required executable "{name}" was not found on PATH')
    return executable


def install_dependencies(plugin_root: Path) -> None:
    """Install the Node dependencies needed by the TypeScript core."""
    npm = require_executable("npm")
    command = [npm, "ci", "--silent", "--no-audit", "--no-fund"]
    try:
        subprocess.run(command, cwd=plugin_root, check=True)
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"npm ci failed with exit code {exc.returncode}") from exc


def ensure_zod_package_manifest(plugin_root: Path) -> None:
    """Mirror the Node bootstrap's zod package manifest compatibility shim."""
    package_json_path = plugin_root / "node_modules" / "zod" / "package.json"
    if package_json_path.exists():
        return

    package_json_path.parent.mkdir(parents=True, exist_ok=True)
    package_json_path.write_text(
        json.dumps(ZOD_PACKAGE_JSON, indent=2) + "\n",
        encoding="utf-8",
    )


def ensure_runtime(plugin_root: Path) -> None:
    if missing_runtime_files(plugin_root):
        print("[pipeline-orchestrator] Installing dependencies...", file=sys.stderr)
        install_dependencies(plugin_root)
    ensure_zod_package_manifest(plugin_root)


def node_server_command(plugin_root: Path) -> list[str]:
    node = require_executable("node")
    return [node, str(plugin_root / "server.ts")]


def dry_run_payload(plugin_root: Path) -> dict[str, object]:
    command = node_server_command(plugin_root)
    return {
        "plugin_root": str(plugin_root),
        "runtime": "python-launcher-node-core",
        "command": command,
        "missing_runtime_files": [str(path) for path in missing_runtime_files(plugin_root)],
    }


def main(argv: Iterable[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args == ["--dry-run"]:
        print(json.dumps(dry_run_payload(PLUGIN_ROOT), indent=2))
        return 0
    if args:
        print(f"usage: {Path(sys.argv[0]).name} [--dry-run]", file=sys.stderr)
        return 2

    ensure_runtime(PLUGIN_ROOT)
    command = node_server_command(PLUGIN_ROOT)
    os.execv(command[0], command)
    return 127


if __name__ == "__main__":
    raise SystemExit(main())
