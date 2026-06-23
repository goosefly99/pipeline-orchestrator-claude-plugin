from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import start


class StartPyTests(unittest.TestCase):
    def test_missing_runtime_files_reports_relative_sentinels(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            missing = start.missing_runtime_files(root)

            self.assertIn(
                Path("node_modules/@modelcontextprotocol/sdk/dist/esm/server/index.js"),
                missing,
            )
            self.assertIn(Path("node_modules/smol-toml/package.json"), missing)

    def test_missing_runtime_files_returns_empty_when_sentinels_exist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for relative_path in start.RUNTIME_FILES:
                path = root / relative_path
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("", encoding="utf-8")

            self.assertEqual(start.missing_runtime_files(root), [])

    def test_ensure_zod_package_manifest_creates_compat_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            start.ensure_zod_package_manifest(root)

            package_json = root / "node_modules" / "zod" / "package.json"
            payload = json.loads(package_json.read_text(encoding="utf-8"))
            self.assertEqual(payload["name"], "zod")
            self.assertEqual(payload["exports"]["."]["import"], "./v4/classic/index.js")
            self.assertEqual(payload["exports"]["./mini"]["require"], "./v4/mini/index.cjs")

    def test_dry_run_payload_describes_python_launcher_node_core(self) -> None:
        payload = start.dry_run_payload(start.PLUGIN_ROOT)

        self.assertEqual(payload["runtime"], "python-launcher-node-core")
        self.assertTrue(str(payload["command"][-1]).endswith("server.ts"))
        self.assertIsInstance(payload["missing_runtime_files"], list)


if __name__ == "__main__":
    unittest.main()
