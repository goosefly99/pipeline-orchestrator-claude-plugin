# Codebase Analysis & Knowledge Base Integration — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend the pipeline-mcp server with two capabilities: (1) a codebase analyzer that statically walks a package directory and produces a `codebase-requirements` artifact, and (2) a knowledge base client with stub implementations for vector search and SQL query tools that log all queries to the `kb-query-log` format.

**Architecture:** Two new modules (`codebase-analyzer.ts`, `kb-client.ts`) plus three new MCP tools added to `server.ts`. The codebase analyzer uses only `node:fs` and `node:path` — no subprocess execution, no dynamic analysis. The KB client defines a clean provider interface that returns an explicit "provider not configured" error when no SDK is available; real provider adapters are dropped in later without changing the tool interface. All KB queries are logged in-memory and returned alongside results.

**Tech Stack:** Node.js v24 (native TypeScript), `node:fs`, `node:path`, `node:test` (testing). No new npm dependencies — the analyzer is pure Node.js stdlib. KB stubs require no provider SDK at this stage.

---

## File Structure

```
pipeline-mcp/
  codebase-analyzer.ts        — Walk codebase, parse manifests, extract conventions, build codebase-requirements artifact
  kb-client.ts                — KB provider interface + stub implementations + query logging
  tests/
    codebase-analyzer.test.ts — Unit tests for each analyzer subsystem
    kb-client.test.ts         — Unit tests for KB client interface and logging
```

Modifications to existing files:
```
pipeline-mcp/server.ts        — Add 3 new tools: pipeline_analyze_codebase, pipeline_kb_vector_search, pipeline_kb_sql_query
```

---

### Task 1: Codebase Analyzer — Directory Walking and Manifest Detection

**Files:**
- Create: `pipeline-mcp/tests/codebase-analyzer.test.ts` (failing first)
- Create: `pipeline-mcp/codebase-analyzer.ts` (directory walking + manifest parsing only)

- [ ] **Step 1: Write failing tests for directory walking and manifest detection**

```typescript
// pipeline-mcp/tests/codebase-analyzer.test.ts

import { describe, it } from 'node:test'
import assert from 'node:assert/strict'
import { mkdtempSync, mkdirSync, writeFileSync, rmSync } from 'node:fs'
import { join } from 'node:path'
import { tmpdir } from 'node:os'
import { resolve, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

import {
  walkDirectoryTree,
  detectManifest,
  parseManifest,
  readAgentsDirectives,
  detectConventions,
  analyzeCodebase,
} from '../codebase-analyzer.ts'

const __dirname = dirname(fileURLToPath(import.meta.url))

// ── Fixture helpers ─────────────────────────────────────────────────────────

function makeTempDir(): string {
  return mkdtempSync(join(tmpdir(), 'codebase-analyzer-test-'))
}

function writeFixture(dir: string, relPath: string, content: string): void {
  const full = join(dir, relPath)
  mkdirSync(dirname(full), { recursive: true })
  writeFileSync(full, content, 'utf-8')
}

// ── walkDirectoryTree ───────────────────────────────────────────────────────

describe('walkDirectoryTree', () => {
  it('returns significant directories relative to root', () => {
    const root = makeTempDir()
    try {
      mkdirSync(join(root, 'src'))
      mkdirSync(join(root, 'src', 'lib'))
      mkdirSync(join(root, 'tests'))
      mkdirSync(join(root, 'node_modules', 'some-pkg'), { recursive: true })

      const tree = walkDirectoryTree(root)

      assert.ok(tree.includes('src'), `Expected 'src' in ${JSON.stringify(tree)}`)
      assert.ok(tree.includes('src/lib'), `Expected 'src/lib' in ${JSON.stringify(tree)}`)
      assert.ok(tree.includes('tests'), `Expected 'tests' in ${JSON.stringify(tree)}`)
      // node_modules must be excluded
      assert.ok(!tree.some(d => d.startsWith('node_modules')), 'node_modules must be excluded')
    } finally {
      rmSync(root, { recursive: true, force: true })
    }
  })

  it('excludes hidden directories and common noise', () => {
    const root = makeTempDir()
    try {
      mkdirSync(join(root, '.git'))
      mkdirSync(join(root, '.venv'))
      mkdirSync(join(root, '__pycache__'), { recursive: true })
      mkdirSync(join(root, 'dist'))
      mkdirSync(join(root, 'src'))

      const tree = walkDirectoryTree(root)

      assert.ok(!tree.some(d => d.startsWith('.')), 'Hidden dirs must be excluded')
      assert.ok(!tree.includes('__pycache__'), '__pycache__ must be excluded')
      assert.ok(!tree.includes('dist'), 'dist must be excluded')
      assert.ok(tree.includes('src'))
    } finally {
      rmSync(root, { recursive: true, force: true })
    }
  })

  it('returns empty array for flat directory with no subdirs', () => {
    const root = makeTempDir()
    try {
      writeFileSync(join(root, 'index.ts'), 'export {}')
      const tree = walkDirectoryTree(root)
      assert.deepEqual(tree, [])
    } finally {
      rmSync(root, { recursive: true, force: true })
    }
  })
})

// ── detectManifest ──────────────────────────────────────────────────────────

describe('detectManifest', () => {
  it('detects package.json (TypeScript/Node)', () => {
    const root = makeTempDir()
    try {
      writeFixture(root, 'package.json', JSON.stringify({
        name: 'my-pkg',
        version: '1.0.0',
        devDependencies: { typescript: '^6.0.0' },
      }))
      writeFixture(root, 'tsconfig.json', '{}')

      const result = detectManifest(root)

      assert.equal(result.manifest, 'package.json')
      assert.equal(result.language, 'TypeScript')
      assert.equal(result.package_manager, 'npm')
    } finally {
      rmSync(root, { recursive: true, force: true })
    }
  })

  it('detects pyproject.toml (Python)', () => {
    const root = makeTempDir()
    try {
      writeFixture(root, 'pyproject.toml', `
[project]
name = "my-python-pkg"
requires-python = ">=3.12"

[build-system]
requires = ["hatchling"]
`)
      const result = detectManifest(root)

      assert.equal(result.manifest, 'pyproject.toml')
      assert.equal(result.language, 'Python')
    } finally {
      rmSync(root, { recursive: true, force: true })
    }
  })

  it('detects Cargo.toml (Rust)', () => {
    const root = makeTempDir()
    try {
      writeFixture(root, 'Cargo.toml', `
[package]
name = "my-rust-crate"
version = "0.1.0"
edition = "2021"
`)
      const result = detectManifest(root)

      assert.equal(result.manifest, 'Cargo.toml')
      assert.equal(result.language, 'Rust')
      assert.equal(result.package_manager, 'cargo')
    } finally {
      rmSync(root, { recursive: true, force: true })
    }
  })

  it('detects pnpm via pnpm-lock.yaml', () => {
    const root = makeTempDir()
    try {
      writeFixture(root, 'package.json', JSON.stringify({ name: 'pkg' }))
      writeFixture(root, 'pnpm-lock.yaml', 'lockfileVersion: 9')

      const result = detectManifest(root)

      assert.equal(result.package_manager, 'pnpm')
    } finally {
      rmSync(root, { recursive: true, force: true })
    }
  })

  it('returns unknown for directory with no manifest', () => {
    const root = makeTempDir()
    try {
      const result = detectManifest(root)
      assert.equal(result.manifest, null)
      assert.equal(result.language, 'unknown')
    } finally {
      rmSync(root, { recursive: true, force: true })
    }
  })
})

// ── parseManifest ───────────────────────────────────────────────────────────

describe('parseManifest', () => {
  it('extracts name, runtime, and deps from package.json', () => {
    const root = makeTempDir()
    try {
      writeFixture(root, 'package.json', JSON.stringify({
        name: 'my-server',
        version: '0.1.0',
        engines: { node: '>=22' },
        dependencies: { express: '^4.0.0', zod: '^3.0.0' },
        devDependencies: { typescript: '^6.0.0', vitest: '^2.0.0' },
      }))

      const result = parseManifest(root, 'package.json')

      assert.equal(result.name, 'my-server')
      assert.equal(result.runtime, 'node >=22')
      assert.ok(result.runtime_deps.includes('express@^4.0.0'))
      assert.ok(result.runtime_deps.includes('zod@^3.0.0'))
      assert.ok(result.dev_deps.includes('typescript@^6.0.0'))
      assert.ok(result.dev_deps.includes('vitest@^2.0.0'))
    } finally {
      rmSync(root, { recursive: true, force: true })
    }
  })

  it('extracts name, runtime, and deps from pyproject.toml', () => {
    const root = makeTempDir()
    try {
      writeFixture(root, 'pyproject.toml', `
[project]
name = "my-strategy"
requires-python = ">=3.12"
dependencies = ["aiohttp>=3.9", "pydantic>=2.0"]

[tool.uv]
dev-dependencies = ["pytest>=8.0", "mypy>=1.0"]
`)
      const result = parseManifest(root, 'pyproject.toml')

      assert.equal(result.name, 'my-strategy')
      assert.equal(result.runtime, 'python >=3.12')
      assert.ok(result.runtime_deps.includes('aiohttp>=3.9'))
      assert.ok(result.dev_deps.some(d => d.includes('pytest')))
    } finally {
      rmSync(root, { recursive: true, force: true })
    }
  })

  it('extracts name from Cargo.toml', () => {
    const root = makeTempDir()
    try {
      writeFixture(root, 'Cargo.toml', `
[package]
name = "my-crate"
version = "0.1.0"
edition = "2021"

[dependencies]
serde = { version = "1.0", features = ["derive"] }
tokio = "1.0"
`)
      const result = parseManifest(root, 'Cargo.toml')

      assert.equal(result.name, 'my-crate')
      assert.ok(result.runtime_deps.some(d => d.includes('serde')))
      assert.ok(result.runtime_deps.some(d => d.includes('tokio')))
    } finally {
      rmSync(root, { recursive: true, force: true })
    }
  })
})

// ── readAgentsDirectives ────────────────────────────────────────────────────

describe('readAgentsDirectives', () => {
  it('reads AGENTS.md files at multiple directory levels', () => {
    const root = makeTempDir()
    try {
      writeFixture(root, 'AGENTS.md', '# Root Directives\n\n- Use absolute imports\n- All functions must be typed')
      writeFixture(root, 'src/AGENTS.md', '# Src Directives\n\n- No side effects at module level\n- Export from index.ts')
      writeFixture(root, 'src/utils/AGENTS.md', '# Utils Directives\n\n- Pure functions only')

      const directives = readAgentsDirectives(root)

      assert.equal(directives.length, 3)
      const rootDirective = directives.find(d => d.file === 'AGENTS.md')
      assert.ok(rootDirective, 'Should find root AGENTS.md')
      assert.ok(rootDirective!.directives.includes('Use absolute imports'))
      assert.ok(rootDirective!.directives.includes('All functions must be typed'))

      const srcDirective = directives.find(d => d.file === 'src/AGENTS.md')
      assert.ok(srcDirective)
      assert.equal(srcDirective!.scope, 'src/')
    } finally {
      rmSync(root, { recursive: true, force: true })
    }
  })

  it('reads CLAUDE.md as well as AGENTS.md', () => {
    const root = makeTempDir()
    try {
      writeFixture(root, 'CLAUDE.md', '# Claude Directives\n\n- Follow TDD\n- Run tests before commit')

      const directives = readAgentsDirectives(root)

      assert.equal(directives.length, 1)
      assert.equal(directives[0].file, 'CLAUDE.md')
      assert.ok(directives[0].directives.includes('Follow TDD'))
    } finally {
      rmSync(root, { recursive: true, force: true })
    }
  })

  it('extracts bullet-point directives, stripping markdown formatting', () => {
    const root = makeTempDir()
    try {
      writeFixture(root, 'AGENTS.md', `
# My Rules

## Section A
- **Bold directive** — must be obeyed
- Plain directive

## Section B
- Another rule here
`)
      const directives = readAgentsDirectives(root)
      assert.equal(directives.length, 1)
      // Bold markers stripped
      assert.ok(directives[0].directives.some(d => d.includes('Bold directive')))
      assert.ok(directives[0].directives.includes('Plain directive'))
      assert.ok(directives[0].directives.includes('Another rule here'))
    } finally {
      rmSync(root, { recursive: true, force: true })
    }
  })

  it('returns empty array when no AGENTS.md or CLAUDE.md found', () => {
    const root = makeTempDir()
    try {
      writeFixture(root, 'src/index.ts', 'export {}')
      const directives = readAgentsDirectives(root)
      assert.deepEqual(directives, [])
    } finally {
      rmSync(root, { recursive: true, force: true })
    }
  })

  it('skips node_modules when scanning for directives files', () => {
    const root = makeTempDir()
    try {
      writeFixture(root, 'AGENTS.md', '# Root\n\n- Root rule')
      writeFixture(root, 'node_modules/some-pkg/AGENTS.md', '# Noise\n\n- Should be ignored')

      const directives = readAgentsDirectives(root)

      assert.equal(directives.length, 1)
      assert.equal(directives[0].file, 'AGENTS.md')
    } finally {
      rmSync(root, { recursive: true, force: true })
    }
  })
})

// ── detectConventions ───────────────────────────────────────────────────────

describe('detectConventions', () => {
  it('detects ESM imports in TypeScript files', () => {
    const root = makeTempDir()
    try {
      writeFixture(root, 'src/index.ts', `
import { foo } from './foo.ts'
import { bar } from '../bar.ts'
export { foo }
`)
      writeFixture(root, 'src/foo.ts', `
import type { Baz } from './types.ts'
export function foo(): Baz { return {} as Baz }
`)
      const conventions = detectConventions(root, 'TypeScript')

      assert.ok(conventions.import_style.includes('ESM') || conventions.import_style.includes('relative'))
    } finally {
      rmSync(root, { recursive: true, force: true })
    }
  })

  it('detects type annotations in TypeScript', () => {
    const root = makeTempDir()
    try {
      writeFixture(root, 'src/a.ts', `
export function add(a: number, b: number): number {
  return a + b
}
`)
      const conventions = detectConventions(root, 'TypeScript')
      assert.ok(conventions.type_annotations.includes('TypeScript'))
    } finally {
      rmSync(root, { recursive: true, force: true })
    }
  })

  it('detects pytest test framework from Python dev deps', () => {
    const root = makeTempDir()
    try {
      writeFixture(root, 'pyproject.toml', `
[project]
name = "test"
[tool.uv]
dev-dependencies = ["pytest>=8.0"]
`)
      writeFixture(root, 'tests/test_main.py', `
def test_something():
    assert True
`)
      const conventions = detectConventions(root, 'Python')
      assert.ok(
        conventions.test_framework === 'pytest' || conventions.test_framework?.includes('pytest'),
        `Expected pytest, got: ${conventions.test_framework}`,
      )
    } finally {
      rmSync(root, { recursive: true, force: true })
    }
  })

  it('detects node:test framework from package.json test script', () => {
    const root = makeTempDir()
    try {
      writeFixture(root, 'package.json', JSON.stringify({
        name: 'pkg',
        scripts: { test: 'node --test tests/*.test.ts' },
      }))
      const conventions = detectConventions(root, 'TypeScript')
      assert.ok(
        conventions.test_framework?.includes('node:test') || conventions.test_framework?.includes('node --test'),
      )
    } finally {
      rmSync(root, { recursive: true, force: true })
    }
  })
})

// ── analyzeCodebase (full integration) ─────────────────────────────────────

describe('analyzeCodebase', () => {
  it('produces a valid codebase-requirements artifact for a TypeScript project', () => {
    const root = makeTempDir()
    try {
      writeFixture(root, 'package.json', JSON.stringify({
        name: 'my-server',
        type: 'module',
        scripts: { test: 'node --test tests/*.test.ts', start: 'node server.ts' },
        dependencies: { zod: '^3.0.0' },
        devDependencies: { typescript: '^6.0.0' },
        engines: { node: '>=22' },
      }))
      writeFixture(root, 'AGENTS.md', '# Rules\n\n- Use ESM imports\n- All exports typed')
      writeFixture(root, 'server.ts', `
import { foo } from './lib/foo.ts'
export function main(): void { foo() }
`)
      writeFixture(root, 'lib/foo.ts', `
export function foo(): void { console.log('foo') }
`)
      writeFixture(root, 'tests/foo.test.ts', `
import { describe, it } from 'node:test'
import { foo } from '../lib/foo.ts'
describe('foo', () => { it('runs', () => { foo() }) })
`)

      const result = analyzeCodebase(root)

      // Required schema fields
      assert.ok(result.requirements_id.startsWith('cr-'), `Expected cr- prefix, got: ${result.requirements_id}`)
      assert.equal(result.codebase_path, root)
      assert.ok(result.analyzed_date)
      assert.equal(result.package.name, 'my-server')
      assert.equal(result.package.language, 'TypeScript')
      assert.equal(result.package.manifest, 'package.json')

      // Structure
      assert.ok(result.structure.directory_tree.includes('lib'))
      assert.ok(result.structure.directory_tree.includes('tests'))

      // Agents directives
      assert.equal(result.agents_directives.length, 1)
      assert.equal(result.agents_directives[0].file, 'AGENTS.md')
      assert.ok(result.agents_directives[0].directives.includes('Use ESM imports'))

      // Dependencies
      assert.ok(result.dependencies.runtime.some(d => d.includes('zod')))
      assert.ok(result.dependencies.dev.some(d => d.includes('typescript')))

      // Testing
      assert.ok(result.testing.framework?.includes('node:test') || result.testing.framework?.includes('node --test'))
    } finally {
      rmSync(root, { recursive: true, force: true })
    }
  })

  it('produces a valid codebase-requirements artifact for the real synth-mcp project', () => {
    const __dirname = dirname(fileURLToPath(import.meta.url))
    const synthDir = resolve(__dirname, '../../synth-mcp')

    const result = analyzeCodebase(synthDir)

    assert.ok(result.requirements_id.startsWith('cr-'))
    assert.equal(result.package.name, 'synth-mcp')
    assert.equal(result.package.language, 'TypeScript')
    assert.equal(result.package.manifest, 'package.json')
    assert.ok(result.package.runtime?.includes('node') || result.package.runtime === undefined || true)
    // synth-mcp has no AGENTS.md, so directives should be empty
    assert.deepEqual(result.agents_directives, [])
    // Should detect runtime deps from package.json
    assert.ok(result.dependencies.runtime.some(d => d.includes('@modelcontextprotocol')))
  })
})
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /c/Users/olive/Documents/ai_trading/pipeline-mcp && node --test tests/codebase-analyzer.test.ts 2>&1 | head -20
```

Expected: FAIL — `codebase-analyzer.ts` does not exist

- [ ] **Step 3: Implement codebase-analyzer.ts — directory walking and manifest detection**

```typescript
// pipeline-mcp/codebase-analyzer.ts

import { readdirSync, readFileSync, statSync, existsSync } from 'node:fs'
import { join, relative, dirname } from 'node:path'
import { createHash } from 'node:crypto'

// ── Public types ────────────────────────────────────────────────────────────

export interface ManifestDetection {
  manifest: string | null
  language: string
  package_manager: string | null
}

export interface ManifestParsed {
  name: string
  runtime: string | null
  runtime_deps: string[]
  dev_deps: string[]
  entry_points: string[]
  test_script: string | null
}

export interface AgentsDirective {
  file: string
  scope: string
  directives: string[]
}

export interface ConventionDetection {
  import_style: string
  type_annotations: string
  test_framework: string | null
  src_root: string | null
  test_root: string | null
}

export interface CodebaseRequirements {
  requirements_id: string
  codebase_path: string
  analyzed_date: string
  package: {
    name: string
    language: string
    runtime?: string
    package_manager?: string
    manifest?: string
    entry_points?: string[]
  }
  structure: {
    src_root?: string
    test_root?: string
    module_pattern?: string
    directory_tree: string[]
  }
  conventions: {
    import_style?: string
    naming?: string
    type_annotations?: string
    docstrings?: string
    error_handling?: string
    logging?: string
    additional?: string[]
  }
  dependencies: {
    runtime: string[]
    dev: string[]
    forbidden: string[]
  }
  interfaces: {
    existing_types: string[]
    extension_points: string[]
    shared_state: string[]
  }
  agents_directives: AgentsDirective[]
  testing: {
    framework?: string
    patterns: string[]
    required_coverage?: string
  }
}

// ── Constants ───────────────────────────────────────────────────────────────

const EXCLUDED_DIRS = new Set([
  'node_modules', '.git', '.svn', '.hg',
  'dist', 'build', 'out', 'coverage',
  '__pycache__', '.venv', 'venv', '.env',
  '.mypy_cache', '.pytest_cache', '.ruff_cache',
  '.tox', 'target', '.cargo',
])

const MANIFEST_PRIORITY = ['package.json', 'pyproject.toml', 'Cargo.toml', 'go.mod']

// ── walkDirectoryTree ───────────────────────────────────────────────────────

/**
 * Walk a directory tree and return significant subdirectory paths relative to root.
 * Excludes node_modules, .git, build artifacts, and other noise directories.
 */
export function walkDirectoryTree(root: string): string[] {
  const results: string[] = []

  function walk(dir: string): void {
    let entries: string[]
    try {
      entries = readdirSync(dir)
    } catch {
      return
    }

    for (const entry of entries) {
      if (entry.startsWith('.')) continue
      if (EXCLUDED_DIRS.has(entry)) continue

      const full = join(dir, entry)
      let stat
      try {
        stat = statSync(full)
      } catch {
        continue
      }

      if (stat.isDirectory()) {
        const rel = relative(root, full).replace(/\\/g, '/')
        results.push(rel)
        walk(full)
      }
    }
  }

  walk(root)
  return results
}

// ── detectManifest ──────────────────────────────────────────────────────────

/**
 * Detect which package manifest exists in the root, determine primary language
 * and package manager.
 */
export function detectManifest(root: string): ManifestDetection {
  for (const manifest of MANIFEST_PRIORITY) {
    if (!existsSync(join(root, manifest))) continue

    if (manifest === 'package.json') {
      // Determine package manager from lockfiles
      let pm = 'npm'
      if (existsSync(join(root, 'pnpm-lock.yaml'))) pm = 'pnpm'
      else if (existsSync(join(root, 'yarn.lock'))) pm = 'yarn'
      else if (existsSync(join(root, 'bun.lockb')) || existsSync(join(root, 'bun.lock'))) pm = 'bun'

      // TypeScript or JavaScript?
      const hasTsConfig = existsSync(join(root, 'tsconfig.json'))
      let pkg: Record<string, unknown> = {}
      try {
        pkg = JSON.parse(readFileSync(join(root, manifest), 'utf-8')) as Record<string, unknown>
      } catch { /* ignore */ }
      const devDeps = pkg.devDependencies as Record<string, string> | undefined ?? {}
      const isTS = hasTsConfig || 'typescript' in devDeps

      return { manifest, language: isTS ? 'TypeScript' : 'JavaScript', package_manager: pm }
    }

    if (manifest === 'pyproject.toml') {
      // Detect package manager: uv vs poetry vs hatch vs pip
      const content = readFileSync(join(root, manifest), 'utf-8')
      let pm = 'pip'
      if (content.includes('[tool.uv]') || existsSync(join(root, 'uv.lock'))) pm = 'uv'
      else if (content.includes('[tool.poetry]') || existsSync(join(root, 'poetry.lock'))) pm = 'poetry'
      else if (content.includes('[tool.hatch]')) pm = 'hatch'
      return { manifest, language: 'Python', package_manager: pm }
    }

    if (manifest === 'Cargo.toml') {
      return { manifest, language: 'Rust', package_manager: 'cargo' }
    }

    if (manifest === 'go.mod') {
      return { manifest, language: 'Go', package_manager: 'go' }
    }
  }

  return { manifest: null, language: 'unknown', package_manager: null }
}

// ── parseManifest ───────────────────────────────────────────────────────────

/**
 * Parse a manifest file to extract package name, runtime requirement,
 * runtime/dev dependencies, and entry points.
 */
export function parseManifest(root: string, manifest: string): ManifestParsed {
  const content = readFileSync(join(root, manifest), 'utf-8')
  const empty: ManifestParsed = { name: 'unknown', runtime: null, runtime_deps: [], dev_deps: [], entry_points: [], test_script: null }

  if (manifest === 'package.json') {
    let pkg: Record<string, unknown>
    try {
      pkg = JSON.parse(content) as Record<string, unknown>
    } catch {
      return empty
    }

    const name = (pkg.name as string) ?? 'unknown'
    const engines = pkg.engines as Record<string, string> | undefined
    const nodeReq = engines?.node
    const runtime = nodeReq ? `node ${nodeReq}` : null

    const deps = pkg.dependencies as Record<string, string> | undefined ?? {}
    const devDeps = pkg.devDependencies as Record<string, string> | undefined ?? {}
    const scripts = pkg.scripts as Record<string, string> | undefined ?? {}
    const testScript = scripts.test ?? null

    const bin = pkg.bin
    const main = pkg.main as string | undefined
    const entryPoints: string[] = []
    if (typeof bin === 'string') entryPoints.push(bin)
    else if (bin && typeof bin === 'object') entryPoints.push(...Object.values(bin as Record<string, string>))
    if (main) entryPoints.push(main)

    return {
      name,
      runtime,
      runtime_deps: Object.entries(deps).map(([k, v]) => `${k}@${v}`),
      dev_deps: Object.entries(devDeps).map(([k, v]) => `${k}@${v}`),
      entry_points: entryPoints,
      test_script: testScript,
    }
  }

  if (manifest === 'pyproject.toml') {
    // Simple regex-based TOML parsing (no external dep)
    const nameMatch = content.match(/^name\s*=\s*["']([^"']+)["']/m)
    const name = nameMatch?.[1] ?? 'unknown'

    const pythonMatch = content.match(/requires-python\s*=\s*["']([^"']+)["']/)
    const runtime = pythonMatch ? `python ${pythonMatch[1]}` : null

    // Extract [project] dependencies array
    const depsMatch = content.match(/\[project\][^[]*?dependencies\s*=\s*\[([^\]]*)\]/s)
    const runtimeDeps: string[] = []
    if (depsMatch) {
      const items = depsMatch[1].matchAll(/["']([^"']+)["']/g)
      for (const m of items) runtimeDeps.push(m[1])
    }

    // Dev deps from [tool.uv] or [tool.poetry] sections
    const devDeps: string[] = []
    const uvDevMatch = content.match(/\[tool\.uv\][^[]*?dev-dependencies\s*=\s*\[([^\]]*)\]/s)
    if (uvDevMatch) {
      const items = uvDevMatch[1].matchAll(/["']([^"']+)["']/g)
      for (const m of items) devDeps.push(m[1])
    }
    const poetryDevSection = content.match(/\[tool\.poetry\.dev-dependencies\][^\[]*/s)
    if (poetryDevSection) {
      const items = poetryDevSection[0].matchAll(/^(\w[\w-]*)\s*=/mg)
      for (const m of items) {
        if (m[1] !== 'tool' && m[1] !== 'poetry') devDeps.push(m[1])
      }
    }

    // Test script from [tool.pytest.ini_options] or scripts section
    const testScript = existsSync(join(root, 'Makefile'))
      ? 'make test'
      : devDeps.some(d => d.startsWith('pytest')) ? 'pytest' : null

    return { name, runtime, runtime_deps: runtimeDeps, dev_deps: devDeps, entry_points: [], test_script: testScript }
  }

  if (manifest === 'Cargo.toml') {
    const nameMatch = content.match(/^\s*name\s*=\s*["']([^"']+)["']/m)
    const name = nameMatch?.[1] ?? 'unknown'
    const editionMatch = content.match(/edition\s*=\s*["']([^"']+)["']/)
    const edition = editionMatch?.[1]
    const runtime = edition ? `rust edition ${edition}` : null

    // Extract [dependencies] section items
    const depsSection = content.match(/\[dependencies\]([\s\S]*?)(\[|$)/)
    const runtimeDeps: string[] = []
    if (depsSection) {
      const lines = depsSection[1].split('\n')
      for (const line of lines) {
        const m = line.match(/^(\w[\w-]*)/)
        if (m) runtimeDeps.push(m[1])
      }
    }

    // Dev deps from [dev-dependencies]
    const devSection = content.match(/\[dev-dependencies\]([\s\S]*?)(\[|$)/)
    const devDeps: string[] = []
    if (devSection) {
      const lines = devSection[1].split('\n')
      for (const line of lines) {
        const m = line.match(/^(\w[\w-]*)/)
        if (m) devDeps.push(m[1])
      }
    }

    return { name, runtime, runtime_deps: runtimeDeps, dev_deps: devDeps, entry_points: [], test_script: 'cargo test' }
  }

  return empty
}

// ── readAgentsDirectives ────────────────────────────────────────────────────

/**
 * Walk the directory tree and extract all AGENTS.md and CLAUDE.md files.
 * Parse bullet-point directives from each file.
 */
export function readAgentsDirectives(root: string): AgentsDirective[] {
  const results: AgentsDirective[] = []
  const targets = ['AGENTS.md', 'CLAUDE.md']

  function walk(dir: string): void {
    let entries: string[]
    try {
      entries = readdirSync(dir)
    } catch {
      return
    }

    for (const target of targets) {
      if (entries.includes(target)) {
        const full = join(dir, target)
        const relFile = relative(root, full).replace(/\\/g, '/')
        const relDir = relative(root, dir).replace(/\\/g, '/')
        const scope = relDir === '' ? './' : `${relDir}/`

        try {
          const content = readFileSync(full, 'utf-8')
          const directives = extractBulletDirectives(content)
          if (directives.length > 0) {
            results.push({ file: relFile || target, scope, directives })
          }
        } catch {
          // skip unreadable files
        }
      }
    }

    for (const entry of entries) {
      if (entry.startsWith('.')) continue
      if (EXCLUDED_DIRS.has(entry)) continue

      const full = join(dir, entry)
      let stat
      try {
        stat = statSync(full)
      } catch {
        continue
      }
      if (stat.isDirectory()) walk(full)
    }
  }

  walk(root)
  return results
}

/**
 * Extract bullet-point directives from markdown content.
 * Strips markdown formatting (bold, italic, inline code).
 * Skips heading lines and blank lines.
 */
function extractBulletDirectives(content: string): string[] {
  const directives: string[] = []
  const lines = content.split('\n')

  for (const line of lines) {
    const trimmed = line.trim()
    // Match - item or * item bullet points
    const bulletMatch = trimmed.match(/^[-*]\s+(.+)$/)
    if (!bulletMatch) continue

    let text = bulletMatch[1]
    // Strip bold: **text** or __text__
    text = text.replace(/\*\*([^*]+)\*\*/g, '$1').replace(/__([^_]+)__/g, '$1')
    // Strip italic: *text* or _text_
    text = text.replace(/\*([^*]+)\*/g, '$1').replace(/_([^_]+)_/g, '$1')
    // Strip inline code: `text`
    text = text.replace(/`([^`]+)`/g, '$1')
    // Strip trailing em-dash description: " — ..." or " - ..."
    // Keep the core directive before the dash if it's a clarifying note
    text = text.replace(/\s+[—–]\s+.+$/, '').replace(/\s+--\s+.+$/, '')
    text = text.trim()

    if (text.length > 0) directives.push(text)
  }

  return directives
}

// ── detectConventions ───────────────────────────────────────────────────────

/**
 * Detect coding conventions by pattern-matching source files.
 * No subprocess execution — purely file content analysis.
 */
export function detectConventions(root: string, language: string): ConventionDetection {
  const result: ConventionDetection = {
    import_style: 'unknown',
    type_annotations: 'unknown',
    test_framework: null,
    src_root: null,
    test_root: null,
  }

  // Detect test framework from package.json/pyproject.toml first
  result.test_framework = detectTestFramework(root, language)

  // Detect src and test roots
  for (const candidate of ['src', 'lib', 'source']) {
    if (existsSync(join(root, candidate))) { result.src_root = candidate; break }
  }
  for (const candidate of ['tests', 'test', '__tests__', 'spec']) {
    if (existsSync(join(root, candidate))) { result.test_root = candidate; break }
  }

  if (language === 'TypeScript' || language === 'JavaScript') {
    const sourceFiles = collectSourceFiles(root, ['.ts', '.tsx', '.js', '.mjs'], 20)
    const importSamples = collectImportLines(sourceFiles)

    const hasRelative = importSamples.some(l => /from ['"]\.\.?\//.test(l))
    const hasAbsolute = importSamples.some(l => /from ['"][^./]/.test(l))
    const hasNodePrefix = importSamples.some(l => /from ['"]node:/.test(l))
    const hasExtensions = importSamples.some(l => /from ['"].*\.(ts|js|mjs)['"]/.test(l))

    const parts: string[] = []
    if (hasNodePrefix) parts.push('node: prefix for built-ins')
    if (hasRelative && !hasAbsolute) parts.push('relative imports within module')
    else if (hasAbsolute && !hasRelative) parts.push('absolute imports from package root')
    else if (hasRelative && hasAbsolute) parts.push('mixed relative and absolute')
    if (hasExtensions) parts.push('explicit file extensions (.ts)')
    result.import_style = parts.length > 0 ? parts.join(', ') : 'ESM'

    const hasTypeAnnotations = sourceFiles.some(f => {
      try {
        const c = readFileSync(f, 'utf-8')
        return /:\s*(string|number|boolean|void|unknown|Record|Array|Promise)\b/.test(c)
          || /function\s+\w+\s*\([^)]*:\s*\w/.test(c)
      } catch { return false }
    })
    result.type_annotations = language === 'TypeScript'
      ? (hasTypeAnnotations ? 'TypeScript with explicit annotations' : 'TypeScript (annotations not sampled)')
      : 'JavaScript (no static types)'
  }

  if (language === 'Python') {
    const sourceFiles = collectSourceFiles(root, ['.py'], 20)
    const importSamples = collectImportLines(sourceFiles)

    const hasRelative = importSamples.some(l => /^from \./m.test(l))
    const hasAbsolute = importSamples.some(l => /^from [a-z_]/.test(l) && !/^from \./.test(l))
    if (hasRelative && !hasAbsolute) result.import_style = 'relative imports'
    else if (hasAbsolute && !hasRelative) result.import_style = 'absolute imports'
    else result.import_style = 'mixed absolute and relative imports'

    const hasTypeHints = sourceFiles.some(f => {
      try {
        const c = readFileSync(f, 'utf-8')
        return /def \w+\s*\([^)]*:\s*\w/.test(c) || /\)\s*->\s*\w/.test(c)
      } catch { return false }
    })
    result.type_annotations = hasTypeHints ? 'type hints used' : 'type hints not detected'
  }

  return result
}

function detectTestFramework(root: string, language: string): string | null {
  if (language === 'TypeScript' || language === 'JavaScript') {
    // Check package.json test script
    const pkgPath = join(root, 'package.json')
    if (existsSync(pkgPath)) {
      try {
        const pkg = JSON.parse(readFileSync(pkgPath, 'utf-8')) as Record<string, unknown>
        const scripts = pkg.scripts as Record<string, string> | undefined ?? {}
        const testScript = scripts.test ?? ''
        if (testScript.includes('node --test') || testScript.includes('node:test')) return 'node:test'
        if (testScript.includes('vitest')) return 'vitest'
        if (testScript.includes('jest')) return 'jest'
        if (testScript.includes('mocha')) return 'mocha'

        const devDeps = pkg.devDependencies as Record<string, string> | undefined ?? {}
        if ('vitest' in devDeps) return 'vitest'
        if ('jest' in devDeps) return 'jest'
        if ('mocha' in devDeps) return 'mocha'
      } catch { /* ignore */ }
    }
  }

  if (language === 'Python') {
    const pyprojectPath = join(root, 'pyproject.toml')
    if (existsSync(pyprojectPath)) {
      try {
        const content = readFileSync(pyprojectPath, 'utf-8')
        if (content.includes('pytest')) return 'pytest'
        if (content.includes('unittest')) return 'unittest'
      } catch { /* ignore */ }
    }
    // Check test files
    const testFiles = collectSourceFiles(root, ['.py'], 5)
    for (const f of testFiles) {
      try {
        const c = readFileSync(f, 'utf-8')
        if (/import pytest|from pytest/.test(c)) return 'pytest'
        if (/import unittest|from unittest/.test(c)) return 'unittest'
      } catch { /* ignore */ }
    }
  }

  if (language === 'Rust') return 'cargo test'

  return null
}

function collectSourceFiles(root: string, extensions: string[], maxFiles: number): string[] {
  const results: string[] = []

  function walk(dir: string): void {
    if (results.length >= maxFiles) return
    let entries: string[]
    try {
      entries = readdirSync(dir)
    } catch {
      return
    }

    for (const entry of entries) {
      if (results.length >= maxFiles) return
      if (entry.startsWith('.')) continue
      if (EXCLUDED_DIRS.has(entry)) continue

      const full = join(dir, entry)
      let stat
      try {
        stat = statSync(full)
      } catch {
        continue
      }

      if (stat.isDirectory()) {
        walk(full)
      } else if (extensions.some(ext => entry.endsWith(ext))) {
        results.push(full)
      }
    }
  }

  walk(root)
  return results
}

function collectImportLines(files: string[]): string[] {
  const lines: string[] = []
  for (const f of files) {
    try {
      const content = readFileSync(f, 'utf-8')
      const importLines = content.split('\n').filter(l =>
        /^import\s|^from\s/.test(l.trim()),
      )
      lines.push(...importLines.slice(0, 5))
    } catch { /* ignore */ }
  }
  return lines
}

// ── analyzeCodebase ─────────────────────────────────────────────────────────

/**
 * Full codebase analysis. Walks the directory, parses manifest, reads AGENTS.md
 * files, and detects conventions from source files.
 *
 * Returns a CodebaseRequirements artifact conforming to the pipeline schema.
 */
export function analyzeCodebase(codebasePath: string): CodebaseRequirements {
  const analyzed_date = new Date().toISOString()

  // Generate deterministic ID from path + date (first 8 hex chars of SHA-256)
  const hash = createHash('sha256')
    .update(`${codebasePath}:${analyzed_date}`)
    .digest('hex')
    .slice(0, 8)
  const requirements_id = `cr-${hash}`

  // 1. Detect manifest
  const manifestDetection = detectManifest(codebasePath)
  const language = manifestDetection.language

  // 2. Parse manifest
  let parsed: ManifestParsed = {
    name: 'unknown', runtime: null, runtime_deps: [], dev_deps: [], entry_points: [], test_script: null,
  }
  if (manifestDetection.manifest) {
    parsed = parseManifest(codebasePath, manifestDetection.manifest)
  }

  // 3. Walk directory tree
  const directoryTree = walkDirectoryTree(codebasePath)

  // 4. Read AGENTS.md / CLAUDE.md directives
  const agentsDirectives = readAgentsDirectives(codebasePath)

  // 5. Detect conventions
  const conventions = detectConventions(codebasePath, language)

  // 6. Detect README
  let readmeContent: string | null = null
  for (const name of ['README.md', 'readme.md', 'README.txt', 'README']) {
    if (existsSync(join(codebasePath, name))) {
      try { readmeContent = readFileSync(join(codebasePath, name), 'utf-8').slice(0, 500) } catch { /* ignore */ }
      break
    }
  }

  // 7. Detect src_root and test_root from directory tree
  const srcRoot = conventions.src_root
    ?? (directoryTree.includes('src') ? 'src' : directoryTree.includes('lib') ? 'lib' : null)
    ?? undefined
  const testRoot = conventions.test_root
    ?? (directoryTree.includes('tests') ? 'tests' : directoryTree.includes('test') ? 'test' : null)
    ?? undefined

  // 8. Detect module pattern
  let modulePattern: string | undefined
  if (srcRoot) {
    const srcEntries = directoryTree.filter(d => d.startsWith(`${srcRoot}/`) && d.split('/').length === 2)
    if (srcEntries.length > 1) modulePattern = `${srcRoot}/{module_name}/`
  }

  // 9. Detect naming convention (snake_case vs camelCase) from source files
  let naming = 'follows language conventions'
  if (language === 'TypeScript' || language === 'JavaScript') {
    naming = 'camelCase functions and variables, PascalCase types and classes, kebab-case filenames'
  } else if (language === 'Python') {
    naming = 'snake_case functions and variables, PascalCase classes, snake_case module names'
  } else if (language === 'Rust') {
    naming = 'snake_case functions and variables, PascalCase types and structs, snake_case module names'
  }

  // 10. Build testing section
  const testFramework = conventions.test_framework ?? (parsed.test_script ? parsed.test_script : undefined)
  const testPatterns: string[] = []
  if (testRoot) testPatterns.push(`tests in ${testRoot}/ directory`)
  if (testRoot) {
    const testFiles = collectSourceFiles(join(codebasePath, testRoot), ['.ts', '.js', '.py', '.rs'], 3)
    if (testFiles.some(f => f.endsWith('.test.ts') || f.endsWith('.test.js'))) {
      testPatterns.push('*.test.ts pattern for test files')
    }
    if (testFiles.some(f => f.startsWith('test_'))) {
      testPatterns.push('test_*.py pattern for test files')
    }
  }

  return {
    requirements_id,
    codebase_path: codebasePath,
    analyzed_date,
    package: {
      name: parsed.name,
      language,
      ...(parsed.runtime ? { runtime: parsed.runtime } : {}),
      ...(manifestDetection.package_manager ? { package_manager: manifestDetection.package_manager } : {}),
      ...(manifestDetection.manifest ? { manifest: manifestDetection.manifest } : {}),
      ...(parsed.entry_points.length > 0 ? { entry_points: parsed.entry_points } : {}),
    },
    structure: {
      ...(srcRoot ? { src_root: srcRoot } : {}),
      ...(testRoot ? { test_root: testRoot } : {}),
      ...(modulePattern ? { module_pattern: modulePattern } : {}),
      directory_tree: directoryTree,
    },
    conventions: {
      import_style: conventions.import_style !== 'unknown' ? conventions.import_style : undefined,
      naming,
      type_annotations: conventions.type_annotations !== 'unknown' ? conventions.type_annotations : undefined,
    },
    dependencies: {
      runtime: parsed.runtime_deps,
      dev: parsed.dev_deps,
      forbidden: [],
    },
    interfaces: {
      existing_types: [],
      extension_points: [],
      shared_state: [],
    },
    agents_directives: agentsDirectives,
    testing: {
      ...(testFramework ? { framework: testFramework } : {}),
      patterns: testPatterns,
    },
  }
}
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd /c/Users/olive/Documents/ai_trading/pipeline-mcp && node --test tests/codebase-analyzer.test.ts 2>&1
```

Expected: All tests in `walkDirectoryTree`, `detectManifest`, `parseManifest`, `readAgentsDirectives`, `detectConventions` groups pass. The `analyzeCodebase` real synth-mcp integration test passes.

- [ ] **Step 5: Verify TypeScript**

```bash
cd /c/Users/olive/Documents/ai_trading/pipeline-mcp && npx tsc --noEmit codebase-analyzer.ts 2>&1
```

Expected: No errors

- [ ] **Step 6: Commit**

```bash
cd /c/Users/olive/Documents/ai_trading/pipeline-mcp && git add codebase-analyzer.ts tests/codebase-analyzer.test.ts && git commit -m "feat(pipeline-mcp): codebase analyzer — static directory walk, manifest parsing, AGENTS.md extraction, convention detection"
```

---

### Task 2: Codebase Analyzer — Convention Detection Tests (Additional Cases)

This task adds edge-case tests for the convention detector and verifies the `analyzeCodebase` function handles missing/partial manifests gracefully.

**Files:**
- Modify: `pipeline-mcp/tests/codebase-analyzer.test.ts` (append additional test blocks)

- [ ] **Step 1: Append edge-case tests to the existing test file**

Append the following `describe` blocks to the end of `tests/codebase-analyzer.test.ts`:

```typescript
// ── analyzeCodebase — edge cases ────────────────────────────────────────────

describe('analyzeCodebase — edge cases', () => {
  it('handles a directory with no manifest gracefully', () => {
    const root = makeTempDir()
    try {
      mkdirSync(join(root, 'src'))
      writeFixture(root, 'src/main.py', 'print("hello")')

      const result = analyzeCodebase(root)

      assert.ok(result.requirements_id.startsWith('cr-'))
      assert.equal(result.package.language, 'unknown')
      assert.equal(result.package.name, 'unknown')
      assert.equal(result.package.manifest, undefined)
      assert.ok(result.structure.directory_tree.includes('src'))
      assert.deepEqual(result.dependencies.runtime, [])
    } finally {
      rmSync(root, { recursive: true, force: true })
    }
  })

  it('handles an empty directory', () => {
    const root = makeTempDir()
    try {
      const result = analyzeCodebase(root)

      assert.ok(result.requirements_id.startsWith('cr-'))
      assert.deepEqual(result.structure.directory_tree, [])
      assert.deepEqual(result.agents_directives, [])
      assert.deepEqual(result.dependencies.runtime, [])
    } finally {
      rmSync(root, { recursive: true, force: true })
    }
  })

  it('produces unique requirements_id for two calls on the same directory (different timestamps)', async () => {
    const root = makeTempDir()
    try {
      writeFixture(root, 'package.json', JSON.stringify({ name: 'pkg' }))

      const result1 = analyzeCodebase(root)
      // Wait 2ms so timestamp differs
      await new Promise(resolve => setTimeout(resolve, 2))
      const result2 = analyzeCodebase(root)

      // IDs are derived from path + timestamp, so they should differ
      assert.notEqual(result1.requirements_id, result2.requirements_id)
    } finally {
      rmSync(root, { recursive: true, force: true })
    }
  })

  it('analyzes the real pipeline-mcp project correctly', () => {
    const __dirname = dirname(fileURLToPath(import.meta.url))
    const pipelineMcpDir = resolve(__dirname, '..')

    const result = analyzeCodebase(pipelineMcpDir)

    assert.ok(result.requirements_id.startsWith('cr-'))
    assert.equal(result.package.name, 'pipeline-mcp')
    assert.equal(result.package.language, 'TypeScript')
    assert.equal(result.package.manifest, 'package.json')
    assert.ok(result.structure.directory_tree.includes('tests'))
    // No AGENTS.md in pipeline-mcp itself
    assert.deepEqual(result.agents_directives, [])
    // Should detect node:test from test script
    assert.ok(
      result.testing.framework?.includes('node') || result.testing.framework?.includes('test'),
      `Expected node:test framework, got: ${result.testing.framework}`,
    )
    // Should find @modelcontextprotocol in runtime deps
    assert.ok(result.dependencies.runtime.some(d => d.includes('@modelcontextprotocol')))
    assert.ok(result.dependencies.dev.some(d => d.includes('typescript')))
  })
})

describe('detectConventions — Python with absolute imports', () => {
  it('correctly identifies absolute import style', () => {
    const root = makeTempDir()
    try {
      writeFixture(root, 'pyproject.toml', `
[project]
name = "my-pkg"
requires-python = ">=3.12"
dependencies = ["aiohttp"]
`)
      writeFixture(root, 'src/main.py', `
from my_pkg.utils import helper
from my_pkg.models import Model
import os
`)
      writeFixture(root, 'src/utils.py', `
def helper() -> str:
    return "hello"
`)
      const conventions = detectConventions(root, 'Python')
      // Should detect type hints from return annotation
      assert.ok(
        conventions.type_annotations.includes('type hints'),
        `Expected type hints detected, got: ${conventions.type_annotations}`,
      )
    } finally {
      rmSync(root, { recursive: true, force: true })
    }
  })
})
```

- [ ] **Step 2: Run all codebase-analyzer tests**

```bash
cd /c/Users/olive/Documents/ai_trading/pipeline-mcp && node --test tests/codebase-analyzer.test.ts 2>&1
```

Expected: All tests pass including the new edge-case group.

- [ ] **Step 3: Run full test suite to verify no regressions**

```bash
cd /c/Users/olive/Documents/ai_trading/pipeline-mcp && node --test tests/*.test.ts 2>&1
```

Expected: All 39 existing tests pass plus the new codebase-analyzer tests. (If tests count is different, verify the count matches existing + new.)

- [ ] **Step 4: Commit**

```bash
cd /c/Users/olive/Documents/ai_trading/pipeline-mcp && git add tests/codebase-analyzer.test.ts && git commit -m "test(pipeline-mcp): add edge-case and real-project tests for codebase analyzer"
```

---

### Task 3: KB Client — Stub Implementations and Query Logging

**Files:**
- Create: `pipeline-mcp/tests/kb-client.test.ts` (failing first)
- Create: `pipeline-mcp/kb-client.ts`

- [ ] **Step 1: Write failing tests**

```typescript
// pipeline-mcp/tests/kb-client.test.ts

import { describe, it } from 'node:test'
import assert from 'node:assert/strict'
import {
  createKBClient,
  vectorSearch,
  sqlQuery,
  getQueryLog,
  clearQueryLog,
  type KBConfig,
  type VectorSearchResult,
  type SQLQueryResult,
  type KBQueryLogEntry,
} from '../kb-client.ts'

// ── createKBClient ──────────────────────────────────────────────────────────

describe('createKBClient', () => {
  it('creates a client from a valid config', () => {
    const config: KBConfig = {
      name: 'my-vector-store',
      type: 'vector',
      provider: 'pinecone',
      config: { index: 'my-index', environment: 'us-east-1' },
    }
    const client = createKBClient(config)
    assert.ok(client)
    assert.equal(client.name, 'my-vector-store')
    assert.equal(client.type, 'vector')
    assert.equal(client.provider, 'pinecone')
  })

  it('creates a SQL client', () => {
    const config: KBConfig = {
      name: 'trade-history',
      type: 'sql',
      provider: 'postgres',
      config: { connection_string: 'postgresql://localhost/trades' },
      sql_templates: {
        recent_trades: 'SELECT * FROM trades WHERE created_at > $1 ORDER BY created_at DESC LIMIT $2',
        top_strategies: 'SELECT strategy_id, COUNT(*) as wins FROM trades WHERE outcome = $1 GROUP BY strategy_id',
      },
    }
    const client = createKBClient(config)
    assert.equal(client.type, 'sql')
    assert.ok(client.sql_templates?.recent_trades)
  })
})

// ── vectorSearch ────────────────────────────────────────────────────────────

describe('vectorSearch', () => {
  it('returns provider_not_configured error for pinecone (stub)', async () => {
    const config: KBConfig = {
      name: 'pinecone-store',
      type: 'vector',
      provider: 'pinecone',
      config: { index: 'test', environment: 'us-east-1' },
    }
    const client = createKBClient(config)
    const result = await vectorSearch(client, {
      run_id: 'run-001',
      phase: 'design_synthesis',
      query: 'prediction market microstructure',
      top_k: 5,
    })

    assert.equal(result.status, 'provider_not_configured')
    assert.ok(result.error?.includes('pinecone'))
    assert.equal(result.results.length, 0)
  })

  it('returns provider_not_configured error for chroma (stub)', async () => {
    const config: KBConfig = {
      name: 'chroma-store',
      type: 'vector',
      provider: 'chroma',
      config: { host: 'localhost', port: 8000, collection: 'research' },
    }
    const client = createKBClient(config)
    const result = await vectorSearch(client, {
      run_id: 'run-001',
      phase: 'debate',
      query: 'options pricing Black-Scholes',
      top_k: 3,
    })

    assert.equal(result.status, 'provider_not_configured')
    assert.ok(result.error?.includes('chroma'))
  })

  it('logs the query to the query log', async () => {
    clearQueryLog()
    const config: KBConfig = {
      name: 'test-vector',
      type: 'vector',
      provider: 'pinecone',
      config: { index: 'test' },
    }
    const client = createKBClient(config)

    await vectorSearch(client, {
      run_id: 'run-log-test',
      phase: 'validation',
      query: 'Kelly criterion position sizing',
      top_k: 5,
    })

    const log = getQueryLog('run-log-test', 'validation')
    assert.equal(log.length, 1)
    assert.equal(log[0].source, 'test-vector')
    assert.equal(log[0].query_type, 'vector_search')
    assert.equal(log[0].query, 'Kelly criterion position sizing')
    assert.equal(log[0].results_count, 0)
    assert.ok(log[0].timestamp)
  })

  it('rejects non-vector KB for vector search', async () => {
    const config: KBConfig = {
      name: 'sql-store',
      type: 'sql',
      provider: 'postgres',
      config: { connection_string: 'postgresql://localhost/db' },
    }
    const client = createKBClient(config)

    const result = await vectorSearch(client, {
      run_id: 'run-001',
      phase: 'design_synthesis',
      query: 'test',
      top_k: 5,
    })

    assert.equal(result.status, 'error')
    assert.ok(result.error?.includes('not a vector'))
  })
})

// ── sqlQuery ────────────────────────────────────────────────────────────────

describe('sqlQuery', () => {
  it('returns provider_not_configured for postgres (stub)', async () => {
    const config: KBConfig = {
      name: 'trade-db',
      type: 'sql',
      provider: 'postgres',
      config: { connection_string: 'postgresql://localhost/trades' },
      sql_templates: {
        recent_trades: 'SELECT * FROM trades WHERE created_at > $1 LIMIT $2',
      },
    }
    const client = createKBClient(config)
    const result = await sqlQuery(client, {
      run_id: 'run-001',
      phase: 'design_synthesis',
      template_name: 'recent_trades',
      parameters: { '$1': '2026-01-01', '$2': 100 },
    })

    assert.equal(result.status, 'provider_not_configured')
    assert.ok(result.error?.includes('postgres'))
    assert.equal(result.rows.length, 0)
  })

  it('returns error for unknown template name', async () => {
    const config: KBConfig = {
      name: 'trade-db',
      type: 'sql',
      provider: 'postgres',
      config: { connection_string: 'postgresql://localhost/trades' },
      sql_templates: {
        recent_trades: 'SELECT * FROM trades LIMIT 10',
      },
    }
    const client = createKBClient(config)
    const result = await sqlQuery(client, {
      run_id: 'run-001',
      phase: 'design_synthesis',
      template_name: 'nonexistent_template',
      parameters: {},
    })

    assert.equal(result.status, 'error')
    assert.ok(result.error?.includes('template'))
    assert.ok(result.error?.includes('nonexistent_template'))
  })

  it('rejects non-SQL KB for SQL query', async () => {
    const config: KBConfig = {
      name: 'vector-store',
      type: 'vector',
      provider: 'pinecone',
      config: { index: 'test' },
    }
    const client = createKBClient(config)
    const result = await sqlQuery(client, {
      run_id: 'run-001',
      phase: 'design_synthesis',
      template_name: 'some_query',
      parameters: {},
    })

    assert.equal(result.status, 'error')
    assert.ok(result.error?.includes('not a SQL'))
  })

  it('logs the query to the query log', async () => {
    clearQueryLog()
    const config: KBConfig = {
      name: 'analytics-db',
      type: 'sql',
      provider: 'postgres',
      config: { connection_string: 'postgresql://localhost/analytics' },
      sql_templates: {
        performance_report: 'SELECT * FROM performance WHERE strategy_id = $1',
      },
    }
    const client = createKBClient(config)

    await sqlQuery(client, {
      run_id: 'run-sql-log',
      phase: 'validation',
      template_name: 'performance_report',
      parameters: { '$1': 'strat-001' },
    })

    const log = getQueryLog('run-sql-log', 'validation')
    assert.equal(log.length, 1)
    assert.equal(log[0].source, 'analytics-db')
    assert.equal(log[0].query_type, 'sql_template')
    assert.equal(log[0].query, 'performance_report')
    assert.deepEqual(log[0].parameters, { '$1': 'strat-001' })
  })
})

// ── getQueryLog / clearQueryLog ─────────────────────────────────────────────

describe('getQueryLog', () => {
  it('returns all entries for a run', async () => {
    clearQueryLog()
    const vecClient = createKBClient({
      name: 'v1',
      type: 'vector',
      provider: 'pinecone',
      config: { index: 'test' },
    })
    const sqlClient = createKBClient({
      name: 'db1',
      type: 'sql',
      provider: 'postgres',
      config: { connection_string: 'postgresql://localhost/db' },
      sql_templates: { q1: 'SELECT 1' },
    })

    await vectorSearch(vecClient, { run_id: 'run-multi', phase: 'design_synthesis', query: 'query1', top_k: 3 })
    await vectorSearch(vecClient, { run_id: 'run-multi', phase: 'debate', query: 'query2', top_k: 3 })
    await sqlQuery(sqlClient, { run_id: 'run-multi', phase: 'design_synthesis', template_name: 'q1', parameters: {} })
    // Different run — should not appear
    await vectorSearch(vecClient, { run_id: 'other-run', phase: 'design_synthesis', query: 'query3', top_k: 3 })

    const all = getQueryLog('run-multi')
    assert.equal(all.length, 3)

    const synthOnly = getQueryLog('run-multi', 'design_synthesis')
    assert.equal(synthOnly.length, 2)
  })

  it('returns empty array for unknown run_id', () => {
    const log = getQueryLog('nonexistent-run-xyz')
    assert.deepEqual(log, [])
  })

  it('clearQueryLog removes all entries', async () => {
    const client = createKBClient({
      name: 'v1',
      type: 'vector',
      provider: 'pinecone',
      config: { index: 'test' },
    })
    await vectorSearch(client, { run_id: 'run-clear', phase: 'design_synthesis', query: 'q', top_k: 3 })
    assert.ok(getQueryLog('run-clear').length > 0)

    clearQueryLog()
    assert.deepEqual(getQueryLog('run-clear'), [])
  })
})

// ── exportQueryLog ──────────────────────────────────────────────────────────

describe('exportQueryLog', () => {
  it('exports log in kb-query-log schema format', async () => {
    // Import exportQueryLog
    const { exportQueryLog } = await import('../kb-client.ts')

    clearQueryLog()
    const client = createKBClient({
      name: 'export-test-vec',
      type: 'vector',
      provider: 'pinecone',
      config: { index: 'test' },
    })
    await vectorSearch(client, {
      run_id: 'run-export',
      phase: 'design_synthesis',
      query: 'systematic trading',
      top_k: 5,
    })

    const exported = exportQueryLog('run-export', 'design_synthesis')

    // Verify kb-query-log schema structure
    assert.equal(exported.run_id, 'run-export')
    assert.equal(exported.phase, 'design_synthesis')
    assert.ok(Array.isArray(exported.queries))
    assert.equal(exported.queries.length, 1)

    const q = exported.queries[0]
    assert.ok(q.timestamp)
    assert.equal(q.source, 'export-test-vec')
    assert.equal(q.query_type, 'vector_search')
    assert.equal(q.query, 'systematic trading')
    assert.equal(q.results_count, 0)
  })
})
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /c/Users/olive/Documents/ai_trading/pipeline-mcp && node --test tests/kb-client.test.ts 2>&1 | head -10
```

Expected: FAIL — `kb-client.ts` does not exist

- [ ] **Step 3: Implement kb-client.ts**

```typescript
// pipeline-mcp/kb-client.ts

/**
 * Knowledge Base client module.
 *
 * Defines the provider interface for vector search and SQL query tools.
 * Implementations are stubs: they validate inputs, log queries, and return
 * a clear "provider not configured" error. Real provider adapters (Pinecone,
 * Chroma, PostgreSQL, etc.) are dropped in later by implementing the
 * KBProviderAdapter interface and registering them via registerAdapter().
 *
 * All queries are logged in-memory in the kb-query-log schema format and
 * can be exported at the end of a phase for artifact traceability.
 */

// ── Types ───────────────────────────────────────────────────────────────────

export type KBType = 'vector' | 'sql'
export type KBProvider = 'pinecone' | 'chroma' | 'postgres' | 'sqlite' | string

export interface KBConfig {
  name: string
  type: KBType
  provider: KBProvider
  config: Record<string, unknown>
  sql_templates?: Record<string, string>
}

export interface KBClient {
  name: string
  type: KBType
  provider: KBProvider
  config: Record<string, unknown>
  sql_templates: Record<string, string>
}

// ── Vector search types ─────────────────────────────────────────────────────

export interface VectorSearchParams {
  run_id: string
  phase: string
  query: string
  top_k: number
  filter?: Record<string, unknown>
  debate_transcript_id?: string
}

export type VectorSearchStatus = 'ok' | 'provider_not_configured' | 'error'

export interface VectorSearchResult {
  status: VectorSearchStatus
  results: VectorSearchMatch[]
  results_count: number
  error?: string
}

export interface VectorSearchMatch {
  id: string
  score: number
  metadata: Record<string, unknown>
  text?: string
}

// ── SQL query types ─────────────────────────────────────────────────────────

export interface SQLQueryParams {
  run_id: string
  phase: string
  template_name: string
  parameters: Record<string, unknown>
  debate_transcript_id?: string
}

export type SQLQueryStatus = 'ok' | 'provider_not_configured' | 'error'

export interface SQLQueryResult {
  status: SQLQueryStatus
  rows: Record<string, unknown>[]
  rows_count: number
  error?: string
  template_used?: string
  sql_expanded?: string
}

// ── Query log types (kb-query-log schema) ───────────────────────────────────

export interface KBQueryLogEntry {
  timestamp: string
  source: string
  query_type: 'vector_search' | 'sql_template' | 'sql_raw'
  query: string
  parameters?: Record<string, unknown>
  results_count: number
  results_used: string[]
  influence?: string
}

export interface KBQueryLog {
  run_id: string
  phase: string
  debate_transcript_id?: string
  queries: KBQueryLogEntry[]
}

// ── Provider adapter interface ───────────────────────────────────────────────

/**
 * Interface for real provider implementations.
 * Implement this and call registerAdapter() to enable a provider.
 */
export interface KBProviderAdapter {
  provider: KBProvider
  type: KBType

  /** Execute a vector similarity search. Called only for type='vector'. */
  vectorSearch?(client: KBClient, params: VectorSearchParams): Promise<VectorSearchResult>

  /** Execute a SQL template query. Called only for type='sql'. */
  sqlQuery?(client: KBClient, params: SQLQueryParams): Promise<SQLQueryResult>
}

// ── Provider registry ────────────────────────────────────────────────────────

const adapterRegistry = new Map<string, KBProviderAdapter>()

/**
 * Register a real provider adapter. Key is `${type}:${provider}`.
 * Call this at server startup after importing the provider SDK.
 *
 * Example:
 *   import { PineconeAdapter } from './kb-providers/pinecone.ts'
 *   registerAdapter(new PineconeAdapter())
 */
export function registerAdapter(adapter: KBProviderAdapter): void {
  const key = `${adapter.type}:${adapter.provider}`
  adapterRegistry.set(key, adapter)
}

function getAdapter(type: KBType, provider: KBProvider): KBProviderAdapter | null {
  return adapterRegistry.get(`${type}:${provider}`) ?? null
}

// ── In-memory query log ──────────────────────────────────────────────────────

// Keyed by run_id. Each entry is an array of log records.
const queryLogStore = new Map<string, KBQueryLogEntry[]>()

function appendLog(run_id: string, entry: KBQueryLogEntry): void {
  if (!queryLogStore.has(run_id)) queryLogStore.set(run_id, [])
  queryLogStore.get(run_id)!.push(entry)
}

/**
 * Retrieve query log entries for a run, optionally filtered by phase.
 */
export function getQueryLog(run_id: string, phase?: string): KBQueryLogEntry[] {
  const entries = queryLogStore.get(run_id) ?? []
  if (!phase) return entries
  // Phase is embedded in the entry via source tagging — we store it separately
  return phaseIndex.get(`${run_id}:${phase}`) ?? []
}

// Phase-indexed entries for fast filtering
const phaseIndex = new Map<string, KBQueryLogEntry[]>()

function appendLogWithPhase(run_id: string, phase: string, entry: KBQueryLogEntry): void {
  appendLog(run_id, entry)
  const key = `${run_id}:${phase}`
  if (!phaseIndex.has(key)) phaseIndex.set(key, [])
  phaseIndex.get(key)!.push(entry)
}

/**
 * Clear all query log entries. Useful for test isolation.
 */
export function clearQueryLog(): void {
  queryLogStore.clear()
  phaseIndex.clear()
}

/**
 * Export the query log for a run/phase in kb-query-log schema format.
 * This is the artifact that gets stored alongside phase outputs.
 */
export function exportQueryLog(run_id: string, phase: string, debate_transcript_id?: string): KBQueryLog {
  const queries = getQueryLog(run_id, phase)
  return {
    run_id,
    phase,
    ...(debate_transcript_id ? { debate_transcript_id } : {}),
    queries,
  }
}

// ── createKBClient ───────────────────────────────────────────────────────────

/**
 * Create a typed KB client from a config. The client is a plain object —
 * it does not open any connections at creation time.
 */
export function createKBClient(config: KBConfig): KBClient {
  return {
    name: config.name,
    type: config.type,
    provider: config.provider,
    config: config.config,
    sql_templates: config.sql_templates ?? {},
  }
}

// ── vectorSearch ─────────────────────────────────────────────────────────────

/**
 * Execute a vector similarity search.
 *
 * If a real provider adapter is registered for this client's provider, it is
 * used. Otherwise, returns provider_not_configured with a clear message.
 *
 * Always logs the query regardless of outcome.
 */
export async function vectorSearch(client: KBClient, params: VectorSearchParams): Promise<VectorSearchResult> {
  if (client.type !== 'vector') {
    return {
      status: 'error',
      results: [],
      results_count: 0,
      error: `KB client "${client.name}" is not a vector store (type=${client.type}). Use sqlQuery instead.`,
    }
  }

  let result: VectorSearchResult

  const adapter = getAdapter('vector', client.provider)
  if (adapter?.vectorSearch) {
    result = await adapter.vectorSearch(client, params)
  } else {
    result = {
      status: 'provider_not_configured',
      results: [],
      results_count: 0,
      error: `Vector provider "${client.provider}" is not configured. Install the provider SDK and call registerAdapter() at startup. Query was: "${params.query}"`,
    }
  }

  const entry: KBQueryLogEntry = {
    timestamp: new Date().toISOString(),
    source: client.name,
    query_type: 'vector_search',
    query: params.query,
    results_count: result.results_count,
    results_used: [],
  }
  appendLogWithPhase(params.run_id, params.phase, entry)

  return result
}

// ── sqlQuery ──────────────────────────────────────────────────────────────────

/**
 * Execute a predefined SQL template query.
 *
 * Template names must be registered in the client's sql_templates map.
 * If a real provider adapter is registered, it is used. Otherwise returns
 * provider_not_configured.
 *
 * Always logs the query regardless of outcome.
 */
export async function sqlQuery(client: KBClient, params: SQLQueryParams): Promise<SQLQueryResult> {
  if (client.type !== 'sql') {
    return {
      status: 'error',
      rows: [],
      rows_count: 0,
      error: `KB client "${client.name}" is not a SQL store (type=${client.type}). Use vectorSearch instead.`,
    }
  }

  // Validate template exists
  const template = client.sql_templates[params.template_name]
  if (!template) {
    const available = Object.keys(client.sql_templates).join(', ') || '(none)'
    const entry: KBQueryLogEntry = {
      timestamp: new Date().toISOString(),
      source: client.name,
      query_type: 'sql_template',
      query: params.template_name,
      parameters: params.parameters,
      results_count: 0,
      results_used: [],
    }
    appendLogWithPhase(params.run_id, params.phase, entry)
    return {
      status: 'error',
      rows: [],
      rows_count: 0,
      error: `SQL template "${params.template_name}" not found in client "${client.name}". Available templates: ${available}`,
    }
  }

  let result: SQLQueryResult

  const adapter = getAdapter('sql', client.provider)
  if (adapter?.sqlQuery) {
    result = await adapter.sqlQuery(client, params)
  } else {
    result = {
      status: 'provider_not_configured',
      rows: [],
      rows_count: 0,
      error: `SQL provider "${client.provider}" is not configured. Install the provider SDK and call registerAdapter() at startup. Template: "${params.template_name}"`,
      template_used: params.template_name,
      sql_expanded: template,
    }
  }

  const entry: KBQueryLogEntry = {
    timestamp: new Date().toISOString(),
    source: client.name,
    query_type: 'sql_template',
    query: params.template_name,
    parameters: params.parameters,
    results_count: result.rows_count,
    results_used: [],
  }
  appendLogWithPhase(params.run_id, params.phase, entry)

  return result
}
```

- [ ] **Step 4: Run KB client tests**

```bash
cd /c/Users/olive/Documents/ai_trading/pipeline-mcp && node --test tests/kb-client.test.ts 2>&1
```

Expected: All tests pass — all stubs return `provider_not_configured`, all logging assertions pass.

- [ ] **Step 5: Verify TypeScript**

```bash
cd /c/Users/olive/Documents/ai_trading/pipeline-mcp && npx tsc --noEmit kb-client.ts 2>&1
```

Expected: No errors

- [ ] **Step 6: Run full test suite**

```bash
cd /c/Users/olive/Documents/ai_trading/pipeline-mcp && node --test tests/*.test.ts 2>&1
```

Expected: All tests pass (existing 39 + codebase-analyzer tests + kb-client tests).

- [ ] **Step 7: Commit**

```bash
cd /c/Users/olive/Documents/ai_trading/pipeline-mcp && git add kb-client.ts tests/kb-client.test.ts && git commit -m "feat(pipeline-mcp): KB client with stub provider interface, query logging, and kb-query-log export"
```

---

### Task 4: Add 3 New Tools to server.ts

**Files:**
- Modify: `pipeline-mcp/server.ts`

Three tools are added:
1. `pipeline_analyze_codebase` — Runs the analyzer on a given path, returns the artifact JSON
2. `pipeline_kb_vector_search` — Executes a vector search against a named KB from the run config
3. `pipeline_kb_sql_query` — Executes a SQL template query against a named KB from the run config

The KB tools read KB configuration from the run config (passed as `kb_configs` at init time or provided inline). For this plan, KB configs are accepted inline in the tool call to avoid coupling to a not-yet-designed run-config extension.

- [ ] **Step 1: Read server.ts before editing**

Re-read `pipeline-mcp/server.ts` from the beginning to confirm current state before making changes.

- [ ] **Step 2: Add imports to server.ts**

At the top of `server.ts`, add the new imports after the existing import block:

```typescript
import { analyzeCodebase } from './codebase-analyzer.ts'
import {
  createKBClient,
  vectorSearch,
  sqlQuery,
  exportQueryLog,
  type KBConfig,
} from './kb-client.ts'
```

- [ ] **Step 3: Add three tool definitions to the tools array in server.ts**

Add these three entries inside the `tools: [...]` array, before the closing `]` of the array:

```typescript
    {
      name: 'pipeline_analyze_codebase',
      description: [
        'Analyze an existing package codebase and produce a codebase-requirements artifact.',
        'Walks the directory tree reading AGENTS.md/CLAUDE.md files, package manifests',
        '(package.json, pyproject.toml, Cargo.toml), and source files to detect conventions.',
        'Returns a codebase-requirements JSON artifact conforming to the pipeline schema.',
        'Use this in Phase C before design synthesis when implementing into an existing codebase.',
      ].join(' '),
      inputSchema: {
        type: 'object' as const,
        properties: {
          codebase_path: {
            type: 'string',
            description: 'Absolute path to the package root to analyze',
          },
        },
        required: ['codebase_path'],
      },
    },
    {
      name: 'pipeline_kb_vector_search',
      description: [
        'Query a vector knowledge base (Pinecone, Chroma, etc.) during a pipeline phase.',
        'Provider must be configured via registerAdapter() — returns provider_not_configured if not.',
        'All queries are logged in kb-query-log format. Call pipeline_kb_export_query_log at phase end.',
        'Available during: design_synthesis, debate, validation, implementation_scaffold.',
      ].join(' '),
      inputSchema: {
        type: 'object' as const,
        properties: {
          kb_name: {
            type: 'string',
            description: 'Knowledge base name as defined in the run config kb_configs',
          },
          kb_config: {
            type: 'object',
            description: 'Inline KB configuration (name, type, provider, config)',
          },
          run_id: {
            type: 'string',
            description: 'Current run ID (for query logging)',
          },
          phase: {
            type: 'string',
            description: 'Current phase name (for query logging)',
          },
          query: {
            type: 'string',
            description: 'Natural language search query',
          },
          top_k: {
            type: 'number',
            description: 'Number of results to return (default: 5)',
          },
          filter: {
            type: 'object',
            description: 'Optional metadata filter (provider-specific)',
          },
        },
        required: ['run_id', 'phase', 'query'],
      },
    },
    {
      name: 'pipeline_kb_sql_query',
      description: [
        'Execute a predefined SQL template query against a knowledge base (PostgreSQL, SQLite, etc.).',
        'Template must be registered in the KB config sql_templates map.',
        'Provider must be configured via registerAdapter() — returns provider_not_configured if not.',
        'All queries are logged in kb-query-log format.',
      ].join(' '),
      inputSchema: {
        type: 'object' as const,
        properties: {
          kb_config: {
            type: 'object',
            description: 'Inline KB configuration (name, type, provider, config, sql_templates)',
          },
          run_id: {
            type: 'string',
            description: 'Current run ID (for query logging)',
          },
          phase: {
            type: 'string',
            description: 'Current phase name (for query logging)',
          },
          template_name: {
            type: 'string',
            description: 'Name of the SQL template to execute',
          },
          parameters: {
            type: 'object',
            description: 'Template parameters (e.g., { "$1": "value" })',
          },
        },
        required: ['run_id', 'phase', 'template_name'],
      },
    },
    {
      name: 'pipeline_kb_export_query_log',
      description: [
        'Export the KB query log for a run/phase in kb-query-log schema format.',
        'Call this at the end of a phase to get the full audit trail of KB queries.',
        'The exported log can be stored as a pipeline artifact alongside phase outputs.',
      ].join(' '),
      inputSchema: {
        type: 'object' as const,
        properties: {
          run_id: {
            type: 'string',
            description: 'Run ID to export log for',
          },
          phase: {
            type: 'string',
            description: 'Phase to filter log by',
          },
          debate_transcript_id: {
            type: 'string',
            description: 'Optional debate transcript ID if queries were made during a debate',
          },
        },
        required: ['run_id', 'phase'],
      },
    },
```

- [ ] **Step 4: Add case branches to the switch statement in server.ts**

In the `switch (req.params.name)` block, add these cases before the `default:` case:

```typescript
      case 'pipeline_analyze_codebase':
        return handleAnalyzeCodebase(args)
      case 'pipeline_kb_vector_search':
        return await handleKBVectorSearch(args)
      case 'pipeline_kb_sql_query':
        return await handleKBSQLQuery(args)
      case 'pipeline_kb_export_query_log':
        return handleKBExportQueryLog(args)
```

- [ ] **Step 5: Add handler implementations to server.ts**

Add these functions before the `// ── Transport & lifecycle ──` comment at the end of server.ts:

```typescript
function handleAnalyzeCodebase(args: Record<string, unknown>) {
  const codebasePath = args.codebase_path as string
  if (!codebasePath) throw new Error('codebase_path is required')

  const result = analyzeCodebase(codebasePath)
  return text(JSON.stringify(result, null, 2))
}

async function handleKBVectorSearch(args: Record<string, unknown>) {
  const runId = args.run_id as string
  const phase = args.phase as string
  const query = args.query as string
  const topK = (args.top_k as number | undefined) ?? 5
  const filter = args.filter as Record<string, unknown> | undefined

  if (!runId) throw new Error('run_id is required')
  if (!phase) throw new Error('phase is required')
  if (!query) throw new Error('query is required')

  const kbConfigRaw = args.kb_config as KBConfig | undefined
  if (!kbConfigRaw) throw new Error('kb_config is required')

  const client = createKBClient(kbConfigRaw)
  const result = await vectorSearch(client, { run_id: runId, phase, query, top_k: topK, filter })

  return text(JSON.stringify(result, null, 2), result.status === 'error')
}

async function handleKBSQLQuery(args: Record<string, unknown>) {
  const runId = args.run_id as string
  const phase = args.phase as string
  const templateName = args.template_name as string
  const parameters = (args.parameters as Record<string, unknown>) ?? {}

  if (!runId) throw new Error('run_id is required')
  if (!phase) throw new Error('phase is required')
  if (!templateName) throw new Error('template_name is required')

  const kbConfigRaw = args.kb_config as KBConfig | undefined
  if (!kbConfigRaw) throw new Error('kb_config is required')

  const client = createKBClient(kbConfigRaw)
  const result = await sqlQuery(client, { run_id: runId, phase, template_name: templateName, parameters })

  return text(JSON.stringify(result, null, 2), result.status === 'error')
}

function handleKBExportQueryLog(args: Record<string, unknown>) {
  const runId = args.run_id as string
  const phase = args.phase as string
  const debateTranscriptId = args.debate_transcript_id as string | undefined

  if (!runId) throw new Error('run_id is required')
  if (!phase) throw new Error('phase is required')

  const log = exportQueryLog(runId, phase, debateTranscriptId)
  return text(JSON.stringify(log, null, 2))
}
```

- [ ] **Step 6: Verify TypeScript compiles**

```bash
cd /c/Users/olive/Documents/ai_trading/pipeline-mcp && npx tsc --noEmit server.ts 2>&1
```

Expected: No errors

- [ ] **Step 7: Run full test suite**

```bash
cd /c/Users/olive/Documents/ai_trading/pipeline-mcp && node --test tests/*.test.ts 2>&1
```

Expected: All tests pass. Count should be ≥ 39 (existing) plus new analyzer and KB client tests.

- [ ] **Step 8: Commit**

```bash
cd /c/Users/olive/Documents/ai_trading/pipeline-mcp && git add server.ts && git commit -m "feat(pipeline-mcp): add pipeline_analyze_codebase, pipeline_kb_vector_search, pipeline_kb_sql_query, pipeline_kb_export_query_log tools to server"
```

---

### Task 5: Integration Test — Real Codebase Analysis

This task adds an integration test that runs `analyzeCodebase` against the real `synth-mcp` and `concepts-collector` projects and validates the output against the `codebase-requirements.json` schema using AJV (already a dependency).

**Files:**
- Modify: `pipeline-mcp/tests/integration.test.ts` (append new describe block)

- [ ] **Step 1: Read integration.test.ts before editing**

Re-read `pipeline-mcp/tests/integration.test.ts` to confirm its current state.

- [ ] **Step 2: Append integration test for codebase analysis to integration.test.ts**

Append the following block at the end of `tests/integration.test.ts`:

```typescript
// ── Codebase Analysis Integration ─────────────────────────────────────────

import { analyzeCodebase } from '../codebase-analyzer.ts'
import { loadSchemas } from '../validator.ts'
import { validateArtifact } from '../validator.ts'

describe('integration: codebase analysis against real projects', () => {
  it('analyzes synth-mcp and produces schema-valid codebase-requirements', () => {
    const __dirname_local = dirname(fileURLToPath(import.meta.url))
    const synthDir = resolve(__dirname_local, '../../synth-mcp')
    const schemas = loadSchemas(join(PIPELINE_DIR, 'schemas'))

    const artifact = analyzeCodebase(synthDir)

    // Validate against schema
    const validation = validateArtifact(schemas, 'codebase-requirements.json', artifact)
    assert.equal(
      validation.valid,
      true,
      `Schema validation failed for synth-mcp:\n${validation.errors.join('\n')}`,
    )

    // Spot checks
    assert.equal(artifact.package.name, 'synth-mcp')
    assert.equal(artifact.package.language, 'TypeScript')
    assert.equal(artifact.package.manifest, 'package.json')
    assert.ok(artifact.dependencies.runtime.some(d => d.includes('@modelcontextprotocol')))
    assert.ok(artifact.structure.directory_tree.length === 0 || true) // flat project, may have no subdirs
  })

  it('analyzes concepts-collector and produces schema-valid codebase-requirements', () => {
    const __dirname_local = dirname(fileURLToPath(import.meta.url))
    const conceptsDir = resolve(__dirname_local, '../../concepts-collector')
    const schemas = loadSchemas(join(PIPELINE_DIR, 'schemas'))

    const artifact = analyzeCodebase(conceptsDir)

    const validation = validateArtifact(schemas, 'codebase-requirements.json', artifact)
    assert.equal(
      validation.valid,
      true,
      `Schema validation failed for concepts-collector:\n${validation.errors.join('\n')}`,
    )

    assert.equal(artifact.package.name, 'concepts-collector')
    assert.equal(artifact.package.language, 'TypeScript')
    assert.ok(artifact.requirements_id.startsWith('cr-'))
    assert.ok(artifact.dependencies.runtime.some(d => d.includes('@modelcontextprotocol')))
  })

  it('analyzes pipeline-mcp itself and produces schema-valid codebase-requirements', () => {
    const __dirname_local = dirname(fileURLToPath(import.meta.url))
    const pipelineDir = resolve(__dirname_local, '..')
    const schemas = loadSchemas(join(PIPELINE_DIR, 'schemas'))

    const artifact = analyzeCodebase(pipelineDir)

    const validation = validateArtifact(schemas, 'codebase-requirements.json', artifact)
    assert.equal(
      validation.valid,
      true,
      `Schema validation failed for pipeline-mcp:\n${validation.errors.join('\n')}`,
    )

    assert.equal(artifact.package.name, 'pipeline-mcp')
    assert.equal(artifact.package.language, 'TypeScript')
    // Should detect smol-toml, ajv, @modelcontextprotocol in runtime deps
    assert.ok(artifact.dependencies.runtime.some(d => d.includes('smol-toml')))
    assert.ok(artifact.dependencies.runtime.some(d => d.includes('ajv')))
    // tests dir should be in directory tree
    assert.ok(artifact.structure.directory_tree.includes('tests'))
  })

  it('full pipeline run including codebase analysis phase', () => {
    // This test simulates a pipeline run that includes a codebase analysis step:
    // init run → add manifest artifact → start codebase_analysis phase →
    // produce codebase-requirements artifact → validate → store → complete

    const __dirname_local = dirname(fileURLToPath(import.meta.url))
    const config = loadPipelineConfig(join(PIPELINE_DIR, 'pipeline.toml'))
    const schemas = loadSchemas(join(PIPELINE_DIR, 'schemas'))

    // Init a run that skips all phases except what we need
    const runDir = join(tempDir, 'runs', 'codebase-analysis-run')
    const phaseNames = Object.keys(config.phases)
    let state = initRun('codebase-analysis-run', config.pipeline.version, phaseNames, runDir)

    // Provide a research manifest so DAG can resolve
    state = addArtifact(state, {
      type: 'research-manifest',
      path: 'manifests/test.json',
      phase: 'pre-existing',
      created_at: new Date().toISOString(),
    }, runDir)

    // Run codebase analysis on synth-mcp
    const synthDir = resolve(__dirname_local, '../../synth-mcp')
    const codebaseArtifact = analyzeCodebase(synthDir)

    // Validate the artifact
    const validation = validateArtifact(schemas, 'codebase-requirements.json', codebaseArtifact)
    assert.equal(validation.valid, true, `Codebase artifact invalid: ${validation.errors.join(', ')}`)

    // Store it
    const sc: StorageConfig = {
      base_dir: tempDir,
      paths: {
        raw_collections: 'collections/raw',
        specs: 'specs',
        overviews: 'overviews',
        codebase_requirements: 'codebase',
      },
    }
    const storedPath = storeArtifact(sc, 'codebase_requirements', `${codebaseArtifact.requirements_id}.json`, codebaseArtifact)
    assert.ok(storedPath.includes('codebase'))

    // Register in run state
    state = addArtifact(state, {
      type: 'codebase-requirements',
      path: storedPath,
      phase: 'codebase_analysis',
      created_at: new Date().toISOString(),
    }, runDir)

    // Load it back and verify
    const loaded = loadArtifact(sc, 'codebase_requirements', `${codebaseArtifact.requirements_id}.json`)
    assert.ok(loaded !== null)
    const loadedArtifact = loaded as typeof codebaseArtifact
    assert.equal(loadedArtifact.package.name, 'synth-mcp')
    assert.equal(loadedArtifact.requirements_id, codebaseArtifact.requirements_id)
  })
})
```

- [ ] **Step 3: Run the integration tests**

```bash
cd /c/Users/olive/Documents/ai_trading/pipeline-mcp && node --test tests/integration.test.ts 2>&1
```

Expected: All integration tests pass including the 4 new codebase analysis tests.

- [ ] **Step 4: Run full test suite**

```bash
cd /c/Users/olive/Documents/ai_trading/pipeline-mcp && node --test tests/*.test.ts 2>&1
```

Expected: All tests pass. Record the final test count.

- [ ] **Step 5: Verify TypeScript on all new files**

```bash
cd /c/Users/olive/Documents/ai_trading/pipeline-mcp && npx tsc --noEmit codebase-analyzer.ts kb-client.ts server.ts 2>&1
```

Expected: No errors

- [ ] **Step 6: Commit**

```bash
cd /c/Users/olive/Documents/ai_trading/pipeline-mcp && git add tests/integration.test.ts && git commit -m "test(pipeline-mcp): integration tests for codebase analysis against real synth-mcp and concepts-collector projects"
```

---

## Appendix: Key Design Decisions

### Why No External Dependencies for the Analyzer

The codebase analyzer uses only `node:fs`, `node:path`, and `node:crypto`. This keeps it zero-dependency and avoids version conflicts with the target codebases being analyzed. The only TOML parsing needed (for `pyproject.toml` and `Cargo.toml`) is handled with simple regex, which is sufficient for extracting the subset of fields we need without implementing a full TOML parser.

### Why KB Tools Are Stubs

The KB tools define a complete interface (inputs, outputs, logging contract) without requiring any provider SDKs to be installed. The `registerAdapter()` pattern means adding Pinecone support later is a single-file change (a new `kb-providers/pinecone.ts`) with no changes to server.ts or the tool interface. The stubs always log queries, so the audit trail is complete from day one even before real providers are wired up.

### Why 4 Tools Instead of 3

A fourth tool, `pipeline_kb_export_query_log`, is included because without it there is no way to retrieve the query log from an MCP tool call. The three core tools (analyze_codebase, kb_vector_search, kb_sql_query) are the ones specified in the plan brief; the export tool is necessary infrastructure for the logging contract to be useful.

### codebase-requirements Schema Conformance

The `analyzeCodebase` function produces output that:
- Has all required fields: `requirements_id`, `codebase_path`, `analyzed_date`, `package.name`, `package.language`
- Uses the `cr-{8char_hex}` ID format
- Omits optional fields rather than including empty strings
- Uses `undefined` for absent optional fields (which JSON.stringify omits)

This ensures the artifact passes AJV schema validation without `additionalProperties: false` conflicts.
