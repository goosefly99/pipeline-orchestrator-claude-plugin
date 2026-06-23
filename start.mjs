import { execSync } from 'node:child_process';
import { existsSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { ensureZodPackageManifest } from '../_shared/ensure-zod-package.mjs';

const __dirname = dirname(fileURLToPath(import.meta.url));
const runtimeFiles = [
  join(__dirname, 'node_modules', '@modelcontextprotocol', 'sdk', 'dist', 'esm', 'server', 'index.js'),
  join(__dirname, 'node_modules', '@modelcontextprotocol', 'sdk', 'dist', 'esm', 'server', 'stdio.js'),
  join(__dirname, 'node_modules', '@modelcontextprotocol', 'sdk', 'dist', 'esm', 'types.js'),
  join(__dirname, 'node_modules', 'ajv', 'package.json'),
  join(__dirname, 'node_modules', 'ajv-formats', 'package.json'),
  join(__dirname, 'node_modules', 'smol-toml', 'package.json'),
];

if (runtimeFiles.some((filePath) => !existsSync(filePath))) {
  console.error('[pipeline-orchestrator] Installing dependencies...');
  try {
    execSync('npm ci --silent --no-audit --no-fund', {
      cwd: __dirname,
      stdio: ['ignore', 'ignore', 'inherit'],
    });
  } catch (e) {
    console.error('[pipeline-orchestrator] npm ci failed:', e.message);
    process.exit(1);
  }
}

ensureZodPackageManifest(__dirname);

await import('./server.ts');
