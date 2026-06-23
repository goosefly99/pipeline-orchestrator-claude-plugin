import { describe, it } from 'node:test'
import assert from 'node:assert/strict'
import { mkdtempSync, rmSync, mkdirSync, writeFileSync } from 'node:fs'
import { join, dirname, resolve } from 'node:path'
import { tmpdir } from 'node:os'
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

// ─── helpers ────────────────────────────────────────────────────────────────

function makeTempDir(): string {
  return mkdtempSync(join(tmpdir(), 'codebase-analyzer-test-'))
}

function writeFixture(dir: string, relPath: string, content: string): void {
  const full = join(dir, relPath)
  mkdirSync(dirname(full), { recursive: true })
  writeFileSync(full, content, 'utf8')
}

// ─── walkDirectoryTree ──────────────────────────────────────────────────────

describe('walkDirectoryTree', () => {
  it('returns significant directories relative to root', () => {
    const dir = makeTempDir()
    try {
      mkdirSync(join(dir, 'src'), { recursive: true })
      mkdirSync(join(dir, 'src', 'lib'), { recursive: true })
      mkdirSync(join(dir, 'tests'), { recursive: true })
      writeFixture(dir, 'src/index.ts', 'export {}')
      writeFixture(dir, 'src/lib/util.ts', 'export {}')
      writeFixture(dir, 'tests/foo.test.ts', '')

      const dirs = walkDirectoryTree(dir)
      assert.ok(dirs.includes('src'), `expected 'src' in ${JSON.stringify(dirs)}`)
      assert.ok(dirs.includes('src/lib'), `expected 'src/lib' in ${JSON.stringify(dirs)}`)
      assert.ok(dirs.includes('tests'), `expected 'tests' in ${JSON.stringify(dirs)}`)
    } finally {
      rmSync(dir, { recursive: true, force: true })
    }
  })

  it('excludes node_modules', () => {
    const dir = makeTempDir()
    try {
      mkdirSync(join(dir, 'node_modules', 'some-pkg'), { recursive: true })
      writeFixture(dir, 'node_modules/some-pkg/index.js', '')

      const dirs = walkDirectoryTree(dir)
      assert.ok(!dirs.some(d => d.startsWith('node_modules')), `node_modules should be excluded: ${JSON.stringify(dirs)}`)
    } finally {
      rmSync(dir, { recursive: true, force: true })
    }
  })

  it('excludes hidden dirs (.git, .venv), __pycache__, dist', () => {
    const dir = makeTempDir()
    try {
      mkdirSync(join(dir, '.git'), { recursive: true })
      mkdirSync(join(dir, '.venv'), { recursive: true })
      mkdirSync(join(dir, '__pycache__'), { recursive: true })
      mkdirSync(join(dir, 'dist'), { recursive: true })
      mkdirSync(join(dir, 'src'), { recursive: true })
      writeFixture(dir, '.git/HEAD', 'ref: refs/heads/main')
      writeFixture(dir, 'dist/bundle.js', '')
      writeFixture(dir, 'src/main.ts', '')

      const dirs = walkDirectoryTree(dir)
      assert.ok(!dirs.some(d => d.startsWith('.git')), `.git should be excluded`)
      assert.ok(!dirs.some(d => d.startsWith('.venv')), `.venv should be excluded`)
      assert.ok(!dirs.some(d => d.startsWith('__pycache__')), `__pycache__ should be excluded`)
      assert.ok(!dirs.some(d => d.startsWith('dist')), `dist should be excluded`)
      assert.ok(dirs.includes('src'), `src should be included`)
    } finally {
      rmSync(dir, { recursive: true, force: true })
    }
  })

  it('returns empty array for flat directory with only files', () => {
    const dir = makeTempDir()
    try {
      writeFixture(dir, 'index.ts', 'export {}')
      writeFixture(dir, 'package.json', '{}')

      const dirs = walkDirectoryTree(dir)
      assert.deepEqual(dirs, [])
    } finally {
      rmSync(dir, { recursive: true, force: true })
    }
  })
})

// ─── detectManifest ─────────────────────────────────────────────────────────

describe('detectManifest', () => {
  it('detects package.json → TypeScript (if tsconfig.json exists) with npm', () => {
    const dir = makeTempDir()
    try {
      writeFixture(dir, 'package.json', JSON.stringify({ name: 'my-pkg', version: '1.0.0' }))
      writeFixture(dir, 'tsconfig.json', JSON.stringify({ compilerOptions: {} }))
      writeFixture(dir, 'package-lock.json', JSON.stringify({ lockfileVersion: 3 }))

      const result = detectManifest(dir)
      assert.equal(result.manifest, 'package.json')
      assert.equal(result.language, 'typescript')
      assert.equal(result.package_manager, 'npm')
    } finally {
      rmSync(dir, { recursive: true, force: true })
    }
  })

  it('detects pyproject.toml → Python', () => {
    const dir = makeTempDir()
    try {
      writeFixture(dir, 'pyproject.toml', '[project]\nname = "my-project"\nversion = "0.1.0"\n')

      const result = detectManifest(dir)
      assert.equal(result.manifest, 'pyproject.toml')
      assert.equal(result.language, 'python')
    } finally {
      rmSync(dir, { recursive: true, force: true })
    }
  })

  it('detects Cargo.toml → Rust with cargo', () => {
    const dir = makeTempDir()
    try {
      writeFixture(dir, 'Cargo.toml', '[package]\nname = "my-crate"\nversion = "0.1.0"\n')

      const result = detectManifest(dir)
      assert.equal(result.manifest, 'Cargo.toml')
      assert.equal(result.language, 'rust')
      assert.equal(result.package_manager, 'cargo')
    } finally {
      rmSync(dir, { recursive: true, force: true })
    }
  })

  it('detects pnpm via pnpm-lock.yaml', () => {
    const dir = makeTempDir()
    try {
      writeFixture(dir, 'package.json', JSON.stringify({ name: 'my-pkg' }))
      writeFixture(dir, 'pnpm-lock.yaml', 'lockfileVersion: 9.0\n')

      const result = detectManifest(dir)
      assert.equal(result.manifest, 'package.json')
      assert.equal(result.package_manager, 'pnpm')
    } finally {
      rmSync(dir, { recursive: true, force: true })
    }
  })

  it('returns unknown for no manifest', () => {
    const dir = makeTempDir()
    try {
      writeFixture(dir, 'README.md', '# Hello')

      const result = detectManifest(dir)
      assert.equal(result.manifest, null)
      assert.equal(result.language, 'unknown')
      assert.equal(result.package_manager, null)
    } finally {
      rmSync(dir, { recursive: true, force: true })
    }
  })
})

// ─── parseManifest ──────────────────────────────────────────────────────────

describe('parseManifest', () => {
  it('extracts name, runtime (from engines.node), deps from package.json', () => {
    const dir = makeTempDir()
    try {
      const pkg = {
        name: 'test-pkg',
        engines: { node: '>=20' },
        dependencies: { express: '^4.18.0', lodash: '^4.17.21' },
        devDependencies: { typescript: '^5.0.0', jest: '^29.0.0' },
        scripts: { test: 'jest' },
      }
      writeFixture(dir, 'package.json', JSON.stringify(pkg))

      const result = parseManifest(dir, 'package.json')
      assert.equal(result.name, 'test-pkg')
      assert.equal(result.runtime, '>=20')
      assert.ok(result.runtime_deps.some(d => d.startsWith('express@')), `expected express dep`)
      assert.ok(result.dev_deps.some(d => d.startsWith('typescript@')), `expected typescript dev dep`)
      assert.equal(result.test_script, 'jest')
    } finally {
      rmSync(dir, { recursive: true, force: true })
    }
  })

  it('extracts name, runtime (requires-python), deps from pyproject.toml', () => {
    const dir = makeTempDir()
    try {
      const content = `[project]
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
`
      writeFixture(dir, 'pyproject.toml', content)

      const result = parseManifest(dir, 'pyproject.toml')
      assert.equal(result.name, 'my-project')
      assert.equal(result.runtime, '>=3.11')
      assert.ok(result.runtime_deps.some(d => d.includes('requests')), `expected requests dep`)
      assert.ok(result.dev_deps.some(d => d.includes('pytest')), `expected pytest dev dep`)
    } finally {
      rmSync(dir, { recursive: true, force: true })
    }
  })

  it('extracts name, deps from Cargo.toml', () => {
    const dir = makeTempDir()
    try {
      const content = `[package]
name = "my-crate"
version = "0.1.0"
edition = "2021"

[dependencies]
serde = { version = "1.0", features = ["derive"] }
tokio = "1.0"

[dev-dependencies]
criterion = "0.5"
`
      writeFixture(dir, 'Cargo.toml', content)

      const result = parseManifest(dir, 'Cargo.toml')
      assert.equal(result.name, 'my-crate')
      assert.ok(result.runtime_deps.some(d => d.includes('serde')), `expected serde dep: ${JSON.stringify(result.runtime_deps)}`)
      assert.ok(result.dev_deps.some(d => d.includes('criterion')), `expected criterion dev dep: ${JSON.stringify(result.dev_deps)}`)
    } finally {
      rmSync(dir, { recursive: true, force: true })
    }
  })

  it('extracts deps from go.mod with multiple require blocks', () => {
    const dir = makeTempDir()
    try {
      const content = `module github.com/example/multi-require

go 1.21

require (
\tgithub.com/stretchr/testify v1.8.4
\tgithub.com/gorilla/mux v1.8.0
)

require (
\tgolang.org/x/text v0.14.0
\tgolang.org/x/net v0.20.0
)
`
      writeFixture(dir, 'go.mod', content)

      const result = parseManifest(dir, 'go.mod')
      assert.equal(result.name, 'github.com/example/multi-require')
      assert.equal(result.runtime, 'go 1.21')
      // Dependencies from the first require block
      assert.ok(result.runtime_deps.some(d => d.includes('stretchr/testify')),
        `expected stretchr/testify dep: ${JSON.stringify(result.runtime_deps)}`)
      assert.ok(result.runtime_deps.some(d => d.includes('gorilla/mux')),
        `expected gorilla/mux dep: ${JSON.stringify(result.runtime_deps)}`)
      // Dependencies from the second require block
      assert.ok(result.runtime_deps.some(d => d.includes('golang.org/x/text')),
        `expected golang.org/x/text dep: ${JSON.stringify(result.runtime_deps)}`)
      assert.ok(result.runtime_deps.some(d => d.includes('golang.org/x/net')),
        `expected golang.org/x/net dep: ${JSON.stringify(result.runtime_deps)}`)
      // Verify total count: 4 deps across 2 blocks
      assert.equal(result.runtime_deps.length, 4,
        `expected 4 deps total, got ${result.runtime_deps.length}: ${JSON.stringify(result.runtime_deps)}`)
    } finally {
      rmSync(dir, { recursive: true, force: true })
    }
  })
})

describe('parseManifest parse_error surfacing', () => {
  it('valid package.json has no parse_error', () => {
    const dir = makeTempDir()
    try {
      writeFixture(dir, 'package.json', JSON.stringify({ name: 'ok' }))
      const result = parseManifest(dir, 'package.json')
      assert.equal(result.parse_error, undefined)
      assert.equal(result.name, 'ok')
    } finally {
      rmSync(dir, { recursive: true, force: true })
    }
  })

  it('reports parse_error for malformed JSON package.json', () => {
    const dir = makeTempDir()
    try {
      writeFixture(dir, 'package.json', '{ "name": "broken", invalid json here')
      const result = parseManifest(dir, 'package.json')
      assert.ok(result.parse_error, 'expected parse_error to be set')
      assert.ok(result.parse_error!.includes('package.json'), `expected error to mention package.json: ${result.parse_error}`)
      assert.equal(result.name, '')
    } finally {
      rmSync(dir, { recursive: true, force: true })
    }
  })

  it('reports parse_error when package.json does not exist', () => {
    const dir = makeTempDir()
    try {
      // no package.json written
      const result = parseManifest(dir, 'package.json')
      assert.ok(result.parse_error, 'expected parse_error to be set for missing file')
      assert.ok(result.parse_error!.toLowerCase().includes('package.json'))
    } finally {
      rmSync(dir, { recursive: true, force: true })
    }
  })

  it('reports parse_error when pyproject.toml does not exist', () => {
    const dir = makeTempDir()
    try {
      const result = parseManifest(dir, 'pyproject.toml')
      assert.ok(result.parse_error, 'expected parse_error to be set')
      assert.ok(result.parse_error!.includes('pyproject.toml'))
    } finally {
      rmSync(dir, { recursive: true, force: true })
    }
  })

  it('reports parse_error when Cargo.toml does not exist', () => {
    const dir = makeTempDir()
    try {
      const result = parseManifest(dir, 'Cargo.toml')
      assert.ok(result.parse_error, 'expected parse_error to be set')
      assert.ok(result.parse_error!.includes('Cargo.toml'))
    } finally {
      rmSync(dir, { recursive: true, force: true })
    }
  })

  it('reports parse_error when go.mod does not exist', () => {
    const dir = makeTempDir()
    try {
      const result = parseManifest(dir, 'go.mod')
      assert.ok(result.parse_error, 'expected parse_error to be set')
      assert.ok(result.parse_error!.includes('go.mod'))
    } finally {
      rmSync(dir, { recursive: true, force: true })
    }
  })

  it('unknown manifest type returns parse_error describing the reason', () => {
    const dir = makeTempDir()
    try {
      const result = parseManifest(dir, 'composer.json')
      assert.ok(result.parse_error, 'expected parse_error for unknown manifest')
      assert.ok(result.parse_error!.includes('composer.json'))
      assert.equal(result.name, '')
    } finally {
      rmSync(dir, { recursive: true, force: true })
    }
  })

  it('analyzeCodebase handles malformed manifest gracefully with empty package name', () => {
    const dir = makeTempDir()
    try {
      writeFixture(dir, 'package.json', '{ invalid json')
      const result = analyzeCodebase(dir)
      assert.equal(result.package.name, '', 'expected empty name when manifest parse fails')
    } finally {
      rmSync(dir, { recursive: true, force: true })
    }
  })
})

// ─── readAgentsDirectives ───────────────────────────────────────────────────

describe('readAgentsDirectives', () => {
  it('reads AGENTS.md at multiple directory levels', () => {
    const dir = makeTempDir()
    try {
      writeFixture(dir, 'AGENTS.md', `# Root Directives\n\n- Use TypeScript\n- Follow strict mode\n`)
      mkdirSync(join(dir, 'src'), { recursive: true })
      writeFixture(dir, 'src/AGENTS.md', `# Src Directives\n\n- Keep modules small\n`)

      const directives = readAgentsDirectives(dir)
      assert.ok(directives.length >= 2, `expected at least 2 directive files: ${JSON.stringify(directives)}`)
      const files = directives.map(d => d.file)
      assert.ok(files.some(f => f.endsWith('AGENTS.md') && !f.includes('src')), `expected root AGENTS.md`)
      assert.ok(files.some(f => f.includes('src')), `expected src AGENTS.md`)

      const allDirectives = directives.flatMap(d => d.directives)
      assert.ok(allDirectives.some(d => d.includes('TypeScript')), `expected TypeScript directive`)
    } finally {
      rmSync(dir, { recursive: true, force: true })
    }
  })

  it('reads CLAUDE.md as well', () => {
    const dir = makeTempDir()
    try {
      writeFixture(dir, 'CLAUDE.md', `# Claude Instructions\n\n- Always write tests\n- Use ESM imports\n`)

      const directives = readAgentsDirectives(dir)
      assert.ok(directives.length >= 1, `expected at least 1 directive file`)
      const allDirectives = directives.flatMap(d => d.directives)
      assert.ok(allDirectives.some(d => d.includes('write tests')), `expected 'write tests' directive`)
    } finally {
      rmSync(dir, { recursive: true, force: true })
    }
  })

  it('strips markdown bold/italic formatting from directives', () => {
    const dir = makeTempDir()
    try {
      writeFixture(dir, 'AGENTS.md', `# Directives\n\n- **Never** use var declarations\n- Use *strict* mode\n- Check \`tsconfig.json\` settings\n`)

      const directives = readAgentsDirectives(dir)
      const allDirectives = directives.flatMap(d => d.directives)
      // Bold should be stripped (** removed but text preserved)
      assert.ok(allDirectives.some(d => d.includes('Never') && !d.includes('**')), `bold markers should be stripped`)
      // Italic should be stripped
      assert.ok(allDirectives.some(d => d.includes('strict') && !d.includes('*strict*')), `italic markers should be stripped`)
    } finally {
      rmSync(dir, { recursive: true, force: true })
    }
  })

  it('returns empty for no directive files', () => {
    const dir = makeTempDir()
    try {
      writeFixture(dir, 'README.md', '# My Project\n')
      writeFixture(dir, 'package.json', '{}')

      const directives = readAgentsDirectives(dir)
      assert.deepEqual(directives, [])
    } finally {
      rmSync(dir, { recursive: true, force: true })
    }
  })

  it('skips node_modules when scanning', () => {
    const dir = makeTempDir()
    try {
      mkdirSync(join(dir, 'node_modules', 'some-pkg'), { recursive: true })
      writeFixture(dir, 'node_modules/some-pkg/AGENTS.md', `- Inside node_modules\n`)
      writeFixture(dir, 'AGENTS.md', `- Root directive\n`)

      const directives = readAgentsDirectives(dir)
      // Should find root AGENTS.md but not node_modules one
      const files = directives.map(d => d.file)
      assert.ok(!files.some(f => f.includes('node_modules')), `should not include node_modules AGENTS.md`)
    } finally {
      rmSync(dir, { recursive: true, force: true })
    }
  })
})

// ─── detectConventions ──────────────────────────────────────────────────────

describe('detectConventions', () => {
  it('detects ESM imports in TypeScript files', () => {
    const dir = makeTempDir()
    try {
      writeFixture(dir, 'package.json', JSON.stringify({ name: 'test', type: 'module' }))
      writeFixture(dir, 'src/index.ts', `import { foo } from './foo.ts'\nimport type { Bar } from './bar.ts'\nexport { foo }\n`)

      const result = detectConventions(dir, 'typescript')
      assert.equal(result.import_style, 'esm')
    } finally {
      rmSync(dir, { recursive: true, force: true })
    }
  })

  it('detects type annotations in TypeScript', () => {
    const dir = makeTempDir()
    try {
      writeFixture(dir, 'package.json', JSON.stringify({ name: 'test' }))
      writeFixture(dir, 'src/util.ts', `export function greet(name: string): string {\n  return \`Hello, \${name}\`\n}\n`)

      const result = detectConventions(dir, 'typescript')
      assert.equal(result.type_annotations, true)
    } finally {
      rmSync(dir, { recursive: true, force: true })
    }
  })

  it('detects pytest from Python dev deps', () => {
    const dir = makeTempDir()
    try {
      const content = `[project]
name = "my-project"
requires-python = ">=3.11"
dependencies = []

[tool.uv.dev-dependencies]
dev = [
  "pytest>=7.0.0",
]
`
      writeFixture(dir, 'pyproject.toml', content)
      writeFixture(dir, 'tests/test_foo.py', `def test_basic():\n    assert 1 + 1 == 2\n`)

      const result = detectConventions(dir, 'python')
      assert.equal(result.test_framework, 'pytest')
    } finally {
      rmSync(dir, { recursive: true, force: true })
    }
  })

  it('detects node:test from package.json test script', () => {
    const dir = makeTempDir()
    try {
      const pkg = {
        name: 'test-pkg',
        scripts: { test: 'node --test tests/*.test.ts' },
      }
      writeFixture(dir, 'package.json', JSON.stringify(pkg))
      writeFixture(dir, 'tests/foo.test.ts', `import { describe, it } from 'node:test'\n`)

      const result = detectConventions(dir, 'typescript')
      assert.equal(result.test_framework, 'node:test')
    } finally {
      rmSync(dir, { recursive: true, force: true })
    }
  })
})

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

  it('produces the same requirements_id for two calls on the same directory same day', () => {
    const root = makeTempDir()
    try {
      writeFixture(root, 'package.json', JSON.stringify({ name: 'pkg' }))

      const result1 = analyzeCodebase(root)
      const result2 = analyzeCodebase(root)

      // ID is deterministic per path+day, so both calls return the same id
      assert.equal(result1.requirements_id, result2.requirements_id)
    } finally {
      rmSync(root, { recursive: true, force: true })
    }
  })

  it('does not throw on a nonexistent path; returns a structurally valid result', () => {
    const bogusPath = join(tmpdir(), 'definitely-not-a-real-directory-xyz-789')
    const result = analyzeCodebase(bogusPath)

    // Must still produce the full structural shape
    assert.ok(result.requirements_id.startsWith('cr-'))
    assert.equal(result.codebase_path, bogusPath)
    assert.equal(result.package.language, 'unknown')
    assert.equal(result.package.manifest, undefined)
    assert.deepEqual(result.structure.directory_tree, [])
    assert.deepEqual(result.agents_directives, [])
    assert.deepEqual(result.dependencies.runtime, [])
  })

  it('does not throw when given a path that points at a file (not a directory)', () => {
    const root = makeTempDir()
    try {
      writeFixture(root, 'not-a-dir.txt', 'plain file')
      const filePath = join(root, 'not-a-dir.txt')
      const result = analyzeCodebase(filePath)

      assert.ok(result.requirements_id.startsWith('cr-'))
      assert.equal(result.codebase_path, filePath)
      assert.deepEqual(result.structure.directory_tree, [])
    } finally {
      rmSync(root, { recursive: true, force: true })
    }
  })

  it('all analyzeCodebase results satisfy structural invariants', () => {
    const root = makeTempDir()
    try {
      writeFixture(root, 'package.json', JSON.stringify({ name: 'invariant-test' }))
      const result = analyzeCodebase(root)

      // Required top-level fields
      assert.ok(typeof result.requirements_id === 'string' && result.requirements_id.length > 0)
      assert.ok(typeof result.analyzed_date === 'string' && result.analyzed_date.length > 0)
      assert.ok(typeof result.codebase_path === 'string' && result.codebase_path.length > 0)

      // package shape
      assert.ok(typeof result.package === 'object' && result.package !== null)
      assert.ok(typeof result.package.language === 'string')
      assert.ok(typeof result.package.name === 'string')
      assert.ok(Array.isArray(result.package.entry_points))

      // dependencies shape — required array fields are always arrays
      assert.ok(typeof result.dependencies === 'object' && result.dependencies !== null)
      assert.ok(Array.isArray(result.dependencies.runtime))
      assert.ok(Array.isArray(result.dependencies.dev))

      // agents_directives is always an array
      assert.ok(Array.isArray(result.agents_directives))

      // structure.directory_tree is always an array of strings
      assert.ok(Array.isArray(result.structure.directory_tree))
      for (const entry of result.structure.directory_tree) {
        assert.ok(typeof entry === 'string', `directory_tree entry must be string: ${typeof entry}`)
      }

      // conventions is an object
      assert.ok(typeof result.conventions === 'object' && result.conventions !== null)
    } finally {
      rmSync(root, { recursive: true, force: true })
    }
  })

  it('generates distinct ids for distinct paths', () => {
    const rootA = makeTempDir()
    const rootB = makeTempDir()
    try {
      const a = analyzeCodebase(rootA)
      const b = analyzeCodebase(rootB)
      assert.notEqual(a.requirements_id, b.requirements_id, 'different paths should produce different ids')
    } finally {
      rmSync(rootA, { recursive: true, force: true })
      rmSync(rootB, { recursive: true, force: true })
    }
  })

  it('analyzes the real pipeline-mcp project correctly', () => {
    const pipelineMcpDir = resolve(__dirname, '..')

    const result = analyzeCodebase(pipelineMcpDir)

    assert.ok(result.requirements_id.startsWith('cr-'))
    assert.equal(result.package.name, 'pipeline-mcp')
    // No tsconfig.json at root, so language detects as 'javascript'
    assert.ok(
      result.package.language === 'javascript' || result.package.language === 'typescript',
      `Expected a JS/TS language, got: ${result.package.language}`,
    )
    assert.equal(result.package.manifest, 'package.json')
    assert.ok(result.structure.directory_tree.includes('tests'))
    // AGENTS.md exists at project root, so directives should be captured
    assert.ok(Array.isArray(result.agents_directives))
    assert.ok(result.agents_directives.length > 0, 'expected AGENTS.md directives to be captured')
    assert.ok(
      result.testing.framework?.includes('node') || result.testing.framework?.includes('test'),
      `Expected node:test framework, got: ${result.testing.framework}`,
    )
    assert.ok(result.dependencies.runtime.some((d: string) => d.includes('@modelcontextprotocol')))
    assert.ok(result.dependencies.dev.some((d: string) => d.includes('typescript')))
  })
})

describe('detectConventions — Python with absolute imports', () => {
  it('correctly identifies type annotations in Python source', () => {
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

def helper(name: str) -> str:
    return name
`)
      writeFixture(root, 'src/utils.py', `
def helper() -> str:
    return "hello"
`)
      const conventions = detectConventions(root, 'python')
      assert.ok(
        conventions.type_annotations === true,
        `Expected type_annotations to be true, got: ${conventions.type_annotations}`,
      )
    } finally {
      rmSync(root, { recursive: true, force: true })
    }
  })
})

// ─── analyzeCodebase (integration) ──────────────────────────────────────────

describe('analyzeCodebase', () => {
  it('produces valid codebase-requirements for a TypeScript project fixture', () => {
    const dir = makeTempDir()
    try {
      const pkg = {
        name: 'my-app',
        version: '1.0.0',
        type: 'module',
        engines: { node: '>=20' },
        dependencies: { '@modelcontextprotocol/sdk': '^1.0.0' },
        devDependencies: { typescript: '^5.0.0' },
        scripts: { test: 'node --test tests/*.test.ts' },
      }
      writeFixture(dir, 'package.json', JSON.stringify(pkg))
      writeFixture(dir, 'tsconfig.json', JSON.stringify({ compilerOptions: { strict: true } }))
      writeFixture(dir, 'src/index.ts', `import { Server } from '@modelcontextprotocol/sdk'\nexport function main(): void {}\n`)
      writeFixture(dir, 'tests/index.test.ts', `import { describe, it } from 'node:test'\n`)
      writeFixture(dir, 'AGENTS.md', `# Project Rules\n\n- Use strict TypeScript\n- Write tests for all exports\n`)

      const result = analyzeCodebase(dir)

      // Must have required fields
      assert.ok(result.requirements_id, 'should have a requirements_id')
      assert.ok(result.analyzed_date, 'should have analyzed_date')
      assert.equal(result.codebase_path, dir)
      assert.equal(result.package.manifest, 'package.json')
      assert.equal(result.package.language, 'typescript')
      assert.ok(result.package.name === 'my-app')
      assert.ok(result.conventions.type_annotations === 'required')
      assert.ok(Array.isArray(result.agents_directives))
      assert.ok(Array.isArray(result.structure.directory_tree))
    } finally {
      rmSync(dir, { recursive: true, force: true })
    }
  })

})
