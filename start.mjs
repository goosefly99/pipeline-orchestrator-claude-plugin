import { execSync } from 'node:child_process';
import { existsSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));

if (!existsSync(join(__dirname, 'node_modules'))) {
  console.error('[pipeline-orchestrator] Installing dependencies...');
  try {
    execSync('npm install --silent --no-audit --no-fund', {
      cwd: __dirname,
      stdio: ['ignore', 'ignore', 'inherit'],
    });
  } catch (e) {
    console.error('[pipeline-orchestrator] npm install failed:', e.message);
    process.exit(1);
  }
}

await import('./server.ts');
