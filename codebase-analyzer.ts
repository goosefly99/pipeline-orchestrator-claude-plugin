/**
 * Codebase analyzer — static directory walk, manifest parsing, AGENTS.md extraction,
 * and convention detection for the codebase-requirements pipeline artifact.
 *
 * Uses only node:fs, node:path, node:crypto — no external dependencies.
 */

import { readdirSync, statSync, readFileSync, existsSync } from 'node:fs'
import { join, relative, dirname, extname } from 'node:path'
import { createHash } from 'node:crypto'

// ─── Types ───────────────────────────────────────────────────────────────────

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
  parse_error?: string
}

function emptyManifest(parseError?: string): ManifestParsed {
  const base: ManifestParsed = {
    name: '',
    runtime: null,
    runtime_deps: [],
    dev_deps: [],
    entry_points: [],
    test_script: null,
  }
  if (parseError) base.parse_error = parseError
  return base
}

export interface AgentsDirective {
  file: string
  scope: string
  directives: string[]
}

export interface ConventionDetection {
  import_style: 'esm' | 'commonjs' | 'unknown'
  type_annotations: boolean
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
    entry_points: string[]
  }
  structure: {
    src_root?: string
    test_root?: string
    directory_tree: string[]
  }
  conventions: {
    import_style: string
    type_annotations: string
  }
  dependencies: {
    runtime: string[]
    dev: string[]
  }
  agents_directives: AgentsDirective[]
  testing: {
    framework?: string
  }
}

// ─── Constants ───────────────────────────────────────────────────────────────

export const EXCLUDED_DIRS: Set<string> = new Set([
  'node_modules',
  '.git',
  '.svn',
  '.hg',
  'dist',
  'build',
  'out',
  '__pycache__',
  '.venv',
  'venv',
  '.env',
  '.tox',
  '.mypy_cache',
  '.pytest_cache',
  '.ruff_cache',
  'target',
  '.next',
  '.nuxt',
  'coverage',
  '.nyc_output',
])

export const MANIFEST_PRIORITY: string[] = [
  'package.json',
  'pyproject.toml',
  'Cargo.toml',
  'go.mod',
]

// ─── Internal helpers ────────────────────────────────────────────────────────

function normalizePath(root: string, fullPath: string): string {
  return relative(root, fullPath).replace(/\\/g, '/')
}

function isExcluded(name: string): boolean {
  return EXCLUDED_DIRS.has(name) || name.startsWith('.')
}

/**
 * Collect up to maxFiles source files with the given extensions, skipping excluded dirs.
 */
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
      const full = join(dir, entry)
      let st
      try {
        st = statSync(full)
      } catch {
        continue
      }
      if (st.isDirectory()) {
        if (!isExcluded(entry)) walk(full)
      } else if (st.isFile()) {
        const ext = extname(entry)
        if (extensions.includes(ext)) results.push(full)
      }
    }
  }

  walk(root)
  return results
}

/**
 * Extract import/require lines from a list of source files.
 */
function collectImportLines(files: string[]): string[] {
  const lines: string[] = []
  for (const file of files) {
    try {
      const content = readFileSync(file, 'utf8')
      for (const line of content.split('\n')) {
        const trimmed = line.trim()
        if (trimmed.startsWith('import ') || trimmed.startsWith('from ') || trimmed.startsWith('require(') || trimmed.includes("require('") || trimmed.includes('require("')) {
          lines.push(trimmed)
        }
      }
    } catch {
      // skip unreadable files
    }
  }
  return lines
}

/**
 * Parse markdown bullet points and strip bold/italic/code formatting from each.
 * Handles - and * list markers. Strips em-dash prefix if present.
 */
function extractBulletDirectives(content: string): string[] {
  const directives: string[] = []
  for (const line of content.split('\n')) {
    const trimmed = line.trim()
    // Match markdown list items: "- text" or "* text"
    const match = trimmed.match(/^[-*]\s+(.+)$/)
    if (match) {
      let text = match[1]
      // Strip em-dash prefix: "— ..." → "..."
      text = text.replace(/^—\s*/, '')
      // Strip bold: **text** → text
      text = text.replace(/\*\*(.+?)\*\*/g, '$1')
      // Strip italic: *text* → text, _text_ → text
      text = text.replace(/\*(.+?)\*/g, '$1')
      text = text.replace(/_(.+?)_/g, '$1')
      // Strip inline code: `text` → text
      text = text.replace(/`(.+?)`/g, '$1')
      text = text.trim()
      if (text.length > 0) directives.push(text)
    }
  }
  return directives
}

// ─── walkDirectoryTree ────────────────────────────────────────────────────────

/**
 * Recursively walks the directory tree, skipping excluded dirs.
 * Returns relative paths (forward slashes) of all subdirectories.
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
      if (isExcluded(entry)) continue
      const full = join(dir, entry)
      let st
      try {
        st = statSync(full)
      } catch {
        continue
      }
      if (st.isDirectory()) {
        results.push(normalizePath(root, full))
        walk(full)
      }
    }
  }

  walk(root)
  return results
}

// ─── detectManifest ──────────────────────────────────────────────────────────

/**
 * Check manifest files in priority order. Detect language and package manager
 * from manifest files and lockfiles.
 */
export function detectManifest(root: string): ManifestDetection {
  for (const manifest of MANIFEST_PRIORITY) {
    if (existsSync(join(root, manifest))) {
      if (manifest === 'package.json') {
        // Detect language: TypeScript if tsconfig.json exists, else JavaScript
        const language = existsSync(join(root, 'tsconfig.json')) ? 'typescript' : 'javascript'

        // Detect package manager via lockfiles (checked in priority order)
        let package_manager: string | null = 'npm'
        if (existsSync(join(root, 'pnpm-lock.yaml'))) {
          package_manager = 'pnpm'
        } else if (existsSync(join(root, 'yarn.lock'))) {
          package_manager = 'yarn'
        } else if (existsSync(join(root, 'bun.lockb')) || existsSync(join(root, 'bun.lock'))) {
          package_manager = 'bun'
        } else if (existsSync(join(root, 'package-lock.json'))) {
          package_manager = 'npm'
        }

        return { manifest, language, package_manager }
      }

      if (manifest === 'pyproject.toml') {
        // Detect Python package manager
        let package_manager: string | null = null
        if (existsSync(join(root, 'uv.lock'))) {
          package_manager = 'uv'
        } else if (existsSync(join(root, 'poetry.lock'))) {
          package_manager = 'poetry'
        } else if (existsSync(join(root, 'Pipfile.lock'))) {
          package_manager = 'pipenv'
        }
        return { manifest, language: 'python', package_manager }
      }

      if (manifest === 'Cargo.toml') {
        return { manifest, language: 'rust', package_manager: 'cargo' }
      }

      if (manifest === 'go.mod') {
        return { manifest, language: 'go', package_manager: 'go' }
      }
    }
  }

  return { manifest: null, language: 'unknown', package_manager: null }
}

// ─── parseManifest ───────────────────────────────────────────────────────────

/**
 * Parse the detected manifest to extract name, runtime, deps, etc.
 */
export function parseManifest(root: string, manifest: string): ManifestParsed {
  const manifestPath = join(root, manifest)

  if (manifest === 'package.json') {
    return parsePackageJson(manifestPath)
  }

  if (manifest === 'pyproject.toml') {
    return parsePyprojectToml(manifestPath)
  }

  if (manifest === 'Cargo.toml') {
    return parseCargoToml(manifestPath)
  }

  if (manifest === 'go.mod') {
    return parseGoMod(manifestPath)
  }

  // Fallback for unknown manifests
  return emptyManifest(`No parser registered for manifest "${manifest}"`)
}

function parsePackageJson(filePath: string): ManifestParsed {
  let pkg: Record<string, unknown>
  try {
    pkg = JSON.parse(readFileSync(filePath, 'utf8')) as Record<string, unknown>
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err)
    return emptyManifest(`Failed to parse package.json at "${filePath}": ${msg}`)
  }

  const name = typeof pkg.name === 'string' ? pkg.name : ''

  // Runtime from engines.node
  let runtime: string | null = null
  if (pkg.engines && typeof pkg.engines === 'object') {
    const engines = pkg.engines as Record<string, string>
    if (engines.node) runtime = engines.node
  }

  // Dependencies → "name@version"
  const runtime_deps = depsToList(pkg.dependencies)
  const dev_deps = depsToList(pkg.devDependencies)

  // Entry points from main / exports / bin
  const entry_points: string[] = []
  if (typeof pkg.main === 'string') entry_points.push(pkg.main)
  if (typeof pkg.bin === 'string') entry_points.push(pkg.bin)

  // Test script
  let test_script: string | null = null
  if (pkg.scripts && typeof pkg.scripts === 'object') {
    const scripts = pkg.scripts as Record<string, string>
    if (scripts.test) test_script = scripts.test
  }

  return { name, runtime, runtime_deps, dev_deps, entry_points, test_script }
}

function depsToList(deps: unknown): string[] {
  if (!deps || typeof deps !== 'object') return []
  return Object.entries(deps as Record<string, string>).map(([name, version]) => `${name}@${version}`)
}

function parsePyprojectToml(filePath: string): ManifestParsed {
  let content: string
  try {
    content = readFileSync(filePath, 'utf8')
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err)
    return emptyManifest(`Failed to read pyproject.toml at "${filePath}": ${msg}`)
  }

  // Extract name from [project] section
  const nameMatch = content.match(/^\[project\][\s\S]*?^name\s*=\s*["']([^"']+)["']/m)
  const name = nameMatch ? nameMatch[1] : ''

  // Extract requires-python
  const runtimeMatch = content.match(/requires-python\s*=\s*["']([^"']+)["']/)
  const runtime = runtimeMatch ? runtimeMatch[1] : null

  // Extract [project].dependencies array
  const runtime_deps = extractTomlStringArray(content, /^\[project\][\s\S]*?^dependencies\s*=\s*\[([^\]]*)\]/m)

  // Extract dev dependencies from [tool.uv.dev-dependencies] or [tool.uv] dev section
  // Also try [tool.poetry.dev-dependencies] and [dev-dependencies]
  let dev_deps: string[] = []

  // Try [tool.uv.dev-dependencies] section: look for array under it
  const uvDevMatch = content.match(/\[tool\.uv\.dev-dependencies\]\s*\ndev\s*=\s*\[([^\]]*)\]/m)
  if (uvDevMatch) {
    dev_deps = parseTomlStringList(uvDevMatch[1])
  } else {
    // Try [tool.uv] with optional-dependencies or dev
    const uvDevAlt = content.match(/\[tool\.uv\][\s\S]*?dev\s*=\s*\[([^\]]*)\]/m)
    if (uvDevAlt) {
      dev_deps = parseTomlStringList(uvDevAlt[1])
    }
    // Try [tool.poetry.dev-dependencies]
    if (dev_deps.length === 0) {
      const poetryDevMatch = content.match(/\[tool\.poetry\.dev-dependencies\]([\s\S]*?)(?=\[|$)/m)
      if (poetryDevMatch) {
        dev_deps = extractTomlKeyValueDeps(poetryDevMatch[1])
      }
    }
  }

  // Entry points from [project.scripts]
  const entry_points: string[] = []
  const scriptsMatch = content.match(/\[project\.scripts\]([\s\S]*?)(?=\[|$)/)
  if (scriptsMatch) {
    const scriptEntries = scriptsMatch[1].matchAll(/^\s*\S+\s*=\s*["']([^"']+)["']/mg)
    for (const m of scriptEntries) entry_points.push(m[1])
  }

  return { name, runtime, runtime_deps, dev_deps, entry_points, test_script: null }
}

function extractTomlStringArray(content: string, pattern: RegExp): string[] {
  const match = content.match(pattern)
  if (!match) return []
  return parseTomlStringList(match[1])
}

function parseTomlStringList(raw: string): string[] {
  const results: string[] = []
  // Match quoted strings in the array
  const matches = raw.matchAll(/["']([^"']+)["']/g)
  for (const m of matches) {
    results.push(m[1].trim())
  }
  return results
}

function extractTomlKeyValueDeps(section: string): string[] {
  const results: string[] = []
  for (const line of section.split('\n')) {
    const match = line.match(/^\s*(\S+)\s*=\s*["']([^"']+)["']/)
    if (match) results.push(`${match[1]}${match[2]}`)
  }
  return results
}

function parseCargoToml(filePath: string): ManifestParsed {
  let content: string
  try {
    content = readFileSync(filePath, 'utf8')
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err)
    return emptyManifest(`Failed to read Cargo.toml at "${filePath}": ${msg}`)
  }

  // Extract name from [package]
  const nameMatch = content.match(/^\[package\][\s\S]*?^name\s*=\s*["']([^"']+)["']/m)
  const name = nameMatch ? nameMatch[1] : ''

  // Split content into TOML sections by top-level section headers
  const sections = content.split(/(?=^\[)/m)

  // Extract [dependencies]
  const depSection = sections.find(s => s.startsWith('[dependencies]')) ?? ''
  const runtime_deps = extractCargoDepsFromSection(depSection)

  // Extract [dev-dependencies]
  const devDepSection = sections.find(s => s.startsWith('[dev-dependencies]')) ?? ''
  const dev_deps = extractCargoDepsFromSection(devDepSection)

  return { name, runtime: null, runtime_deps, dev_deps, entry_points: [], test_script: null }
}

function extractCargoDepsFromSection(section: string): string[] {
  const results: string[] = []
  if (!section) return results

  // Skip the section header line
  const lines = section.split('\n').slice(1)
  for (const line of lines) {
    const trimmed = line.trim()
    // Skip empty lines, comments, and sub-table headers
    if (!trimmed || trimmed.startsWith('#') || trimmed.startsWith('[')) continue
    // Match: name = "version" (simple string version)
    const simpleMatch = trimmed.match(/^([a-zA-Z0-9_-]+)\s*=\s*["']([^"']+)["']/)
    // Match: name = { version = "...", ... } (inline table)
    const tableMatch = trimmed.match(/^([a-zA-Z0-9_-]+)\s*=\s*\{/)
    if (simpleMatch) {
      results.push(`${simpleMatch[1]}@${simpleMatch[2]}`)
    } else if (tableMatch) {
      // Extract version from inline table
      const versionMatch = trimmed.match(/version\s*=\s*["']([^"']+)["']/)
      if (versionMatch) {
        results.push(`${tableMatch[1]}@${versionMatch[1]}`)
      } else {
        results.push(tableMatch[1])
      }
    }
  }
  return results
}

function parseGoMod(filePath: string): ManifestParsed {
  let content: string
  try {
    content = readFileSync(filePath, 'utf8')
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err)
    return emptyManifest(`Failed to read go.mod at "${filePath}": ${msg}`)
  }

  // Extract module name
  const moduleMatch = content.match(/^module\s+(\S+)/m)
  const name = moduleMatch ? moduleMatch[1] : ''

  // Extract go version
  const goMatch = content.match(/^go\s+(\S+)/m)
  const runtime = goMatch ? `go ${goMatch[1]}` : null

  // Extract require dependencies
  const runtime_deps: string[] = []

  // Single-line requires: require github.com/foo/bar v1.2.3
  for (const match of content.matchAll(/^require\s+(\S+)\s+(\S+)$/gm)) {
    runtime_deps.push(`${match[1]}@${match[2]}`)
  }

  // Block requires: require ( ... ) — matchAll to capture ALL blocks
  for (const blockMatch of content.matchAll(/^require\s*\(([\s\S]*?)\)/gm)) {
    for (const line of blockMatch[1].split('\n')) {
      const trimmed = line.trim()
      if (!trimmed || trimmed.startsWith('//')) continue
      const parts = trimmed.split(/\s+/)
      if (parts.length >= 2) {
        runtime_deps.push(`${parts[0]}@${parts[1]}`)
      }
    }
  }

  return { name, runtime, runtime_deps, dev_deps: [], entry_points: [], test_script: 'go test ./...' }
}

// ─── readAgentsDirectives ────────────────────────────────────────────────────

const DIRECTIVE_FILENAMES = new Set(['AGENTS.md', 'CLAUDE.md'])

/**
 * Walk the directory tree looking for AGENTS.md and CLAUDE.md files.
 * Extracts bullet directives from each, strips markdown formatting.
 * Skips excluded directories (node_modules, .git, etc.).
 */
export function readAgentsDirectives(root: string): AgentsDirective[] {
  const results: AgentsDirective[] = []

  function walk(dir: string): void {
    let entries: string[]
    try {
      entries = readdirSync(dir)
    } catch {
      return
    }

    for (const entry of entries) {
      const full = join(dir, entry)
      let st
      try {
        st = statSync(full)
      } catch {
        continue
      }

      if (st.isDirectory()) {
        if (!isExcluded(entry)) walk(full)
      } else if (st.isFile() && DIRECTIVE_FILENAMES.has(entry)) {
        try {
          const content = readFileSync(full, 'utf8')
          const directives = extractBulletDirectives(content)
          const scope = normalizePath(root, dirname(full)) || '.'
          results.push({ file: full, scope, directives })
        } catch {
          // skip unreadable files
        }
      }
    }
  }

  walk(root)
  return results
}

// ─── detectConventions ───────────────────────────────────────────────────────

/**
 * Sample source files to detect import style, type annotations, and test framework.
 */
export function detectConventions(root: string, language: string): ConventionDetection {
  let import_style: 'esm' | 'commonjs' | 'unknown' = 'unknown'
  let type_annotations = false
  let test_framework: string | null = null
  let src_root: string | null = null
  let test_root: string | null = null

  // Detect src_root and test_root from directory tree
  const dirs = walkDirectoryTree(root)
  if (dirs.includes('src')) src_root = 'src'
  if (dirs.includes('lib') && !src_root) src_root = 'lib'
  if (dirs.includes('tests')) test_root = 'tests'
  if (dirs.includes('test') && !test_root) test_root = 'test'

  if (language === 'typescript' || language === 'javascript') {
    // Check package.json for "type": "module"
    const pkgPath = join(root, 'package.json')
    if (existsSync(pkgPath)) {
      try {
        const pkg = JSON.parse(readFileSync(pkgPath, 'utf8')) as Record<string, unknown>

        // Detect import style from package type field
        if (pkg.type === 'module') {
          import_style = 'esm'
        } else if (pkg.type === 'commonjs') {
          import_style = 'commonjs'
        }

        // Detect test framework from scripts.test
        if (pkg.scripts && typeof pkg.scripts === 'object') {
          const scripts = pkg.scripts as Record<string, string>
          const testScript = scripts.test ?? ''
          if (testScript.includes('node --test') || testScript.includes('node:test')) {
            test_framework = 'node:test'
          } else if (testScript.includes('jest')) {
            test_framework = 'jest'
          } else if (testScript.includes('vitest')) {
            test_framework = 'vitest'
          } else if (testScript.includes('mocha')) {
            test_framework = 'mocha'
          }
        }
      } catch {
        // ignore parse errors
      }
    }

    // Sample TypeScript/JavaScript source files
    const extensions = language === 'typescript' ? ['.ts', '.tsx'] : ['.js', '.mjs', '.cjs']
    const files = collectSourceFiles(root, extensions, 20)
    const importLines = collectImportLines(files)

    // If import_style not yet set from package.json, infer from import lines
    if (import_style === 'unknown' && importLines.length > 0) {
      const esmCount = importLines.filter(l => l.startsWith('import ')).length
      const cjsCount = importLines.filter(l => l.includes('require(')).length
      if (esmCount > cjsCount) import_style = 'esm'
      else if (cjsCount > 0) import_style = 'commonjs'
    }

    // Detect TypeScript type annotations from source content
    if (language === 'typescript') {
      type_annotations = true // TypeScript projects inherently use type annotations
      // Verify by checking for actual type syntax in files
    }

    // Check for node:test imports if not detected via package.json
    if (!test_framework) {
      const testFiles = collectSourceFiles(root, ['.ts', '.js', '.mjs'], 10)
      const testImports = collectImportLines(testFiles)
      if (testImports.some(l => l.includes('node:test'))) {
        test_framework = 'node:test'
      } else if (testImports.some(l => l.includes('jest') || l.includes('@jest'))) {
        test_framework = 'jest'
      } else if (testImports.some(l => l.includes('vitest'))) {
        test_framework = 'vitest'
      }
    }
  }

  if (language === 'python') {
    // Check pyproject.toml for pytest
    const pyprojectPath = join(root, 'pyproject.toml')
    if (existsSync(pyprojectPath)) {
      try {
        const content = readFileSync(pyprojectPath, 'utf8')
        if (content.includes('pytest')) test_framework = 'pytest'
        else if (content.includes('unittest')) test_framework = 'unittest'
      } catch {
        // ignore
      }
    }

    // Sample Python files for import style
    const files = collectSourceFiles(root, ['.py'], 20)
    const importLines = collectImportLines(files)
    if (importLines.some(l => l.startsWith('import ') || l.startsWith('from '))) {
      import_style = 'esm' // Python uses module imports, map to 'esm'
    }
    // Python always has type annotations available (check for : type syntax)
    if (files.length > 0) {
      for (const file of files.slice(0, 5)) {
        try {
          const content = readFileSync(file, 'utf8')
          if (content.match(/def\s+\w+\([^)]*:\s*\w+/)) {
            type_annotations = true
            break
          }
        } catch {
          // ignore
        }
      }
    }
  }

  if (language === 'rust') {
    import_style = 'esm' // Rust uses mod/use, map to 'esm'
    type_annotations = true
    // Check for test framework
    const files = collectSourceFiles(root, ['.rs'], 10)
    for (const file of files.slice(0, 5)) {
      try {
        const content = readFileSync(file, 'utf8')
        if (content.includes('#[test]') || content.includes('#[cfg(test)]')) {
          test_framework = 'cargo-test'
          break
        }
      } catch {
        // ignore
      }
    }
  }

  return { import_style, type_annotations, test_framework, src_root, test_root }
}

// ─── analyzeCodebase ─────────────────────────────────────────────────────────

/**
 * Orchestrate all analysis steps and return a full CodebaseRequirements artifact.
 */
export function analyzeCodebase(codebasePath: string): CodebaseRequirements {
  const analyzed_date = new Date().toISOString()

  // Generate stable ID based on path + date (truncated to day for stability in tests)
  const dateDay = analyzed_date.slice(0, 10)
  const hash = createHash('sha256').update(`${codebasePath}:${dateDay}`).digest('hex')
  const requirements_id = `cr-${hash.slice(0, 8)}`

  const directory_tree = walkDirectoryTree(codebasePath)
  const manifest = detectManifest(codebasePath)
  const parsed = manifest.manifest
    ? parseManifest(codebasePath, manifest.manifest)
    : emptyManifest()
  const agents_directives = readAgentsDirectives(codebasePath)
  const conventions = detectConventions(codebasePath, manifest.language)

  return {
    requirements_id,
    codebase_path: codebasePath,
    analyzed_date,
    package: {
      name: parsed.name,
      language: manifest.language,
      ...(parsed.runtime !== null && { runtime: parsed.runtime }),
      ...(manifest.package_manager !== null && { package_manager: manifest.package_manager }),
      ...(manifest.manifest !== null && { manifest: manifest.manifest }),
      entry_points: parsed.entry_points,
    },
    structure: {
      ...(conventions.src_root !== null && { src_root: conventions.src_root }),
      ...(conventions.test_root !== null && { test_root: conventions.test_root }),
      directory_tree,
    },
    conventions: {
      import_style: conventions.import_style,
      type_annotations: conventions.type_annotations ? 'required' : 'optional',
    },
    dependencies: {
      runtime: parsed.runtime_deps,
      dev: parsed.dev_deps,
    },
    agents_directives,
    testing: {
      ...(conventions.test_framework !== null && { framework: conventions.test_framework }),
    },
  }
}
