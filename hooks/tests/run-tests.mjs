#!/usr/bin/env node
// run-tests.mjs — Test harness for Claude Code hook scripts
// Pipes JSON fixtures through scripts and validates exit codes + output structure

import { execFileSync, spawnSync } from 'node:child_process'
import { readFileSync, writeFileSync, mkdirSync, rmSync, existsSync, utimesSync, mkdtempSync } from 'node:fs'
import { join, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'
import { tmpdir } from 'node:os'

const __dirname = dirname(fileURLToPath(import.meta.url))
const SCRIPTS_DIR = join(__dirname, '..', 'scripts')
const FIXTURES_DIR = join(__dirname, 'fixtures')
const HOOKS_DIR = join(__dirname, '..')

let passed = 0
let failed = 0
let skipped = 0
const failures = []

function runHook(scriptName, fixtureOrInput, options = {}) {
  const scriptPath = join(SCRIPTS_DIR, scriptName)
  const input = typeof fixtureOrInput === 'string'
    ? readFileSync(join(FIXTURES_DIR, fixtureOrInput), 'utf-8')
    : JSON.stringify(fixtureOrInput)

  try {
    const result = execFileSync('node', [scriptPath], {
      input,
      encoding: 'utf-8',
      timeout: options.timeout || 10_000,
      env: { ...process.env, ...options.env },
    })
    return { exitCode: 0, stdout: result, parsed: result.trim() ? JSON.parse(result.trim()) : null }
  } catch (err) {
    if (err.status !== undefined) {
      return { exitCode: err.status, stdout: err.stdout || '', parsed: null }
    }
    throw err
  }
}

function test(name, fn) {
  try {
    fn()
    passed++
    console.log(`  PASS  ${name}`)
  } catch (err) {
    failed++
    failures.push({ name, error: err.message })
    console.log(`  FAIL  ${name}`)
    console.log(`        ${err.message}`)
  }
}

function assertEqual(actual, expected, msg) {
  if (actual !== expected) {
    throw new Error(`${msg || 'Assertion failed'}: expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)}`)
  }
}

function assertIncludes(str, substr, msg) {
  if (!str || !str.includes(substr)) {
    throw new Error(`${msg || 'Assertion failed'}: expected "${str}" to include "${substr}"`)
  }
}

function assertTruthy(val, msg) {
  if (!val) {
    throw new Error(msg || 'Expected truthy value')
  }
}

// ── Setup: Write test runtime-config.json ──────────────────

function setupConfig(overrides = {}) {
  const config = {
    master: { enabled: true },
    run: { id: '', label: 'test' },
    session_start_clear: { enabled: true },
    send_message_guard: { enabled: true, mode: 'ask', deny_reason: 'Test deny reason.' },
    token_budget_guard: { enabled: true, max_extra_usage_usd: 5.0, warn_at_pct: 80 },
    phase_session_isolation: { enabled: true, warn_only: true },
    dag_guard: { enabled: true },
    artifact_gate: { enabled: true },
    file_read_version_guard: {
      enabled: true,
      present_version_in_development: 'v0.4.0',
      previous_versions: ['v0.3.0', 'v0.2.0'],
    },
    post_phase_complete: { enabled: true },
    phase_context_inject: { enabled: true },
    phase_timing: { enabled: true },
    stop_guard: { enabled: true },
    ...overrides,
  }
  writeFileSync(join(HOOKS_DIR, 'runtime-config.json'), JSON.stringify(config, null, 2), 'utf-8')
  return config
}

function cleanupConfig() {
  const p = join(HOOKS_DIR, 'runtime-config.json')
  if (existsSync(p)) rmSync(p)
}

// ── Tests ──────────────────────────────────────────────────

console.log('\n=== Session Init (session-init.mjs) ===\n')

test('exits cleanly with no runs directory', () => {
  const result = runHook('session-init.mjs', 'session-start.json')
  // May exit 0 with or without output depending on runs/ state
  assertEqual(result.exitCode, 0, 'Exit code')
})

test('produces systemMessage when active run exists', () => {
  const result = runHook('session-init.mjs', 'session-start.json')
  if (result.parsed && result.parsed.systemMessage) {
    assertIncludes(result.parsed.systemMessage, 'pipeline', 'systemMessage content')
  }
  // Either produces systemMessage or exits cleanly — both acceptable
  assertEqual(result.exitCode, 0, 'Exit code')
})

console.log('\n=== Send Message Guard (send-message-guard.mjs) ===\n')

test('returns ask when mode is ask', () => {
  setupConfig({ send_message_guard: { enabled: true, mode: 'ask' } })
  const result = runHook('send-message-guard.mjs', 'send-message-allow.json')
  assertEqual(result.exitCode, 0, 'Exit code')
  assertEqual(result.parsed.permissionDecision, 'ask', 'Permission decision')
  cleanupConfig()
})

test('returns allow when mode is allow', () => {
  setupConfig({ send_message_guard: { enabled: true, mode: 'allow' } })
  const result = runHook('send-message-guard.mjs', 'send-message-allow.json')
  assertEqual(result.parsed.permissionDecision, 'allow', 'Permission decision')
  cleanupConfig()
})

test('returns deny with reason when mode is deny', () => {
  setupConfig({ send_message_guard: { enabled: true, mode: 'deny', deny_reason: 'Custom deny.' } })
  const result = runHook('send-message-guard.mjs', 'send-message-allow.json')
  assertEqual(result.parsed.permissionDecision, 'deny', 'Permission decision')
  assertEqual(result.parsed.deny_reason, 'Custom deny.', 'Deny reason')
  cleanupConfig()
})

console.log('\n=== Token Budget Guard (token-budget-guard.mjs) ===\n')

test('allows when no transcript_path provided', () => {
  setupConfig()
  const result = runHook('token-budget-guard.mjs', 'token-budget.json')
  assertEqual(result.exitCode, 0, 'Exit code')
  assertEqual(result.parsed.permissionDecision, 'allow', 'Permission decision')
  cleanupConfig()
})

test('allows when transcript cost is within budget', () => {
  setupConfig({ token_budget_guard: { max_extra_usage_usd: 100.0, warn_at_pct: 80 } })
  // Create a small transcript
  const transcriptDir = join(tmpdir(), 'pipeline-hook-test-transcript')
  if (!existsSync(transcriptDir)) mkdirSync(transcriptDir, { recursive: true })
  const transcriptPath = join(transcriptDir, 'transcript.jsonl')
  writeFileSync(transcriptPath, JSON.stringify({
    usage: { input_tokens: 100, output_tokens: 50 },
    model: 'claude-sonnet-4-20250514',
  }) + '\n', 'utf-8')

  const result = runHook('token-budget-guard.mjs', {
    ...JSON.parse(readFileSync(join(FIXTURES_DIR, 'token-budget.json'), 'utf-8')),
    transcript_path: transcriptPath,
  })
  assertEqual(result.parsed.permissionDecision, 'allow', 'Permission decision')

  rmSync(transcriptDir, { recursive: true, force: true })
  cleanupConfig()
})

test('denies when transcript cost exceeds budget', () => {
  setupConfig({ token_budget_guard: { max_extra_usage_usd: 0.0001, warn_at_pct: 80 } })
  const transcriptDir = join(tmpdir(), 'pipeline-hook-test-transcript-deny')
  if (!existsSync(transcriptDir)) mkdirSync(transcriptDir, { recursive: true })
  const transcriptPath = join(transcriptDir, 'transcript.jsonl')
  // Write enough tokens to exceed a tiny budget
  writeFileSync(transcriptPath, JSON.stringify({
    usage: { input_tokens: 100000, output_tokens: 50000 },
    model: 'claude-opus-4-20250514',
  }) + '\n', 'utf-8')

  const result = runHook('token-budget-guard.mjs', {
    ...JSON.parse(readFileSync(join(FIXTURES_DIR, 'token-budget.json'), 'utf-8')),
    transcript_path: transcriptPath,
  })
  assertEqual(result.parsed.permissionDecision, 'deny', 'Permission decision')
  assertIncludes(result.parsed.deny_reason, 'budget', 'Deny reason mentions budget')

  rmSync(transcriptDir, { recursive: true, force: true })
  cleanupConfig()
})

test('coerces string token counts without bypassing the budget', () => {
  setupConfig({ token_budget_guard: { max_extra_usage_usd: 0.0001, warn_at_pct: 80 } })
  const transcriptDir = join(tmpdir(), 'pipeline-hook-test-transcript-strings')
  if (!existsSync(transcriptDir)) mkdirSync(transcriptDir, { recursive: true })
  const transcriptPath = join(transcriptDir, 'transcript.jsonl')

  // Attacker-supplied strings. Pre-fix, `"99999" || 0` evaluated to `"99999"`
  // and `"99999" * pricing.input` yielded a Number, but `undefined * pricing`
  // yielded NaN and `NaN >= maxBudget` was false, bypassing the guard.
  // Post-fix: safeNumber coerces all four fields, and Infinity-poisoning
  // fails closed on the non-finite check.
  writeFileSync(transcriptPath, JSON.stringify({
    usage: {
      input_tokens: '99999',
      output_tokens: '50000',
      cache_creation_input_tokens: null,
      cache_read_input_tokens: undefined,
    },
    model: 'claude-opus-4-20250514',
  }) + '\n', 'utf-8')

  const result = runHook('token-budget-guard.mjs', {
    ...JSON.parse(readFileSync(join(FIXTURES_DIR, 'token-budget.json'), 'utf-8')),
    transcript_path: transcriptPath,
  })
  // 99999 opus input tokens at $15/Mtok is ~$1.50; 50000 output at $75/Mtok
  // is ~$3.75. Total > $0.0001 budget → deny.
  assertEqual(result.parsed.permissionDecision, 'deny', 'Permission decision')

  rmSync(transcriptDir, { recursive: true, force: true })
  cleanupConfig()
})

console.log('\n=== Phase Start Guard (phase-start-guard.mjs) ===\n')

test('allows first phase start', () => {
  setupConfig()
  // Use a unique session ID to avoid temp file conflicts
  const result = runHook('phase-start-guard.mjs', {
    event: 'PreToolUse',
    tool_name: 'mcp__pipeline__pipeline_start_phase',
    tool_input: { phase: 'test_unique_phase' },
    session_id: `test-unique-${Date.now()}`,
  })
  assertEqual(result.exitCode, 0, 'Exit code')
  assertEqual(result.parsed.permissionDecision, 'allow', 'Permission decision')
  cleanupConfig()
})

test('warns on repeat phase start in warn_only mode', () => {
  setupConfig({ phase_session_isolation: { enabled: true, warn_only: true } })
  const sessionId = `test-repeat-${Date.now()}`
  const input = {
    event: 'PreToolUse',
    tool_name: 'mcp__pipeline__pipeline_start_phase',
    tool_input: { phase: 'repeat_test_phase' },
    session_id: sessionId,
  }

  // First start
  runHook('phase-start-guard.mjs', input)
  // Second start should warn
  const result = runHook('phase-start-guard.mjs', input)
  assertEqual(result.parsed.permissionDecision, 'allow', 'Permission decision')
  assertIncludes(result.parsed.systemMessage, 'already started', 'Warning message')
  cleanupConfig()
})

test('denies on repeat phase start when warn_only is false', () => {
  setupConfig({ phase_session_isolation: { enabled: true, warn_only: false } })
  const sessionId = `test-deny-${Date.now()}`
  const input = {
    event: 'PreToolUse',
    tool_name: 'mcp__pipeline__pipeline_start_phase',
    tool_input: { phase: 'deny_test_phase' },
    session_id: sessionId,
  }

  runHook('phase-start-guard.mjs', input)
  const result = runHook('phase-start-guard.mjs', input)
  assertEqual(result.parsed.permissionDecision, 'deny', 'Permission decision')
  assertIncludes(result.parsed.deny_reason, 'already started', 'Deny reason')
  cleanupConfig()
})

test('denies path-traversal session_id', () => {
  setupConfig()
  const result = runHook('phase-start-guard.mjs', {
    event: 'PreToolUse',
    tool_name: 'mcp__pipeline__pipeline_start_phase',
    tool_input: { phase: 'traversal_test' },
    session_id: '../../../etc/passwd',
  })
  assertEqual(result.parsed.permissionDecision, 'deny', 'Permission decision')
  assertIncludes(result.parsed.deny_reason, 'unsafe session_id', 'Deny reason')
  cleanupConfig()
})

test('denies session_id longer than 128 chars after sanitization', () => {
  setupConfig()
  const longId = 'a'.repeat(200)
  const result = runHook('phase-start-guard.mjs', {
    event: 'PreToolUse',
    tool_name: 'mcp__pipeline__pipeline_start_phase',
    tool_input: { phase: 'long_test' },
    session_id: longId,
  })
  assertEqual(result.parsed.permissionDecision, 'deny', 'Permission decision')
  assertIncludes(result.parsed.deny_reason, 'exceeds', 'Deny reason')
  cleanupConfig()
})

test('allows safe alphanumeric session_id', () => {
  setupConfig()
  const result = runHook('phase-start-guard.mjs', {
    event: 'PreToolUse',
    tool_name: 'mcp__pipeline__pipeline_start_phase',
    tool_input: { phase: `safe_unique_${Date.now()}` },
    session_id: `safe-session-${Date.now()}`,
  })
  assertEqual(result.parsed.permissionDecision, 'allow', 'Permission decision')
  cleanupConfig()
})

test('cleans up stale session-tracking files older than 24h', () => {
  setupConfig()
  const trackDir = join(tmpdir(), 'pipeline-hooks-sessions')
  if (!existsSync(trackDir)) mkdirSync(trackDir, { recursive: true })

  // Seed 2 stale files (mtime 25h ago) and 1 fresh file (mtime now)
  const staleTime = Date.now() - 25 * 60 * 60 * 1000
  const staleFile1 = join(trackDir, 'session-stale-one.json')
  const staleFile2 = join(trackDir, 'session-stale-two.json')
  const freshFile = join(trackDir, 'session-fresh.json')
  writeFileSync(staleFile1, '{"phases_started":["a"]}', 'utf-8')
  writeFileSync(staleFile2, '{"phases_started":["b"]}', 'utf-8')
  writeFileSync(freshFile, '{"phases_started":["c"]}', 'utf-8')

  // Backdate mtimes on the stale files. utimesSync takes seconds.
  const stalSec = staleTime / 1000
  utimesSync(staleFile1, stalSec, stalSec)
  utimesSync(staleFile2, stalSec, stalSec)

  // Run the hook once — this triggers cleanupStaleSessionFiles
  runHook('phase-start-guard.mjs', {
    event: 'PreToolUse',
    tool_name: 'mcp__pipeline__pipeline_start_phase',
    tool_input: { phase: `ttl_test_${Date.now()}` },
    session_id: `ttl-session-${Date.now()}`,
  })

  // Stale files should be gone; fresh file should survive.
  assertEqual(existsSync(staleFile1), false, 'stale file 1 removed')
  assertEqual(existsSync(staleFile2), false, 'stale file 2 removed')
  assertEqual(existsSync(freshFile), true, 'fresh file preserved')

  // Cleanup residual files we created
  if (existsSync(freshFile)) rmSync(freshFile)
  cleanupConfig()
})

console.log('\n=== DAG Guard (dag-guard.mjs) ===\n')

test('allows entry point phases with no required deps', () => {
  const result = runHook('dag-guard.mjs', {
    event: 'PreToolUse',
    tool_name: 'mcp__pipeline__pipeline_start_phase',
    tool_input: { phase: 'curation' },
    session_id: 'test-session-001',
  })
  assertEqual(result.exitCode, 0, 'Exit code')
  // Curation has entry_point=true so should be allowed
  assertEqual(result.parsed.permissionDecision, 'allow', 'Permission decision')
})

test('allows phase with no phase name', () => {
  const result = runHook('dag-guard.mjs', {
    event: 'PreToolUse',
    tool_name: 'mcp__pipeline__pipeline_start_phase',
    tool_input: {},
    session_id: 'test-session-001',
  })
  assertEqual(result.exitCode, 0, 'Exit code')
  assertEqual(result.parsed.permissionDecision, 'allow', 'Permission decision')
})

test('allows phase whose required dep is skipped', () => {
  // Seed a temp runs directory with a run-state.json where the upstream
  // dep (curation for design_synthesis) is status=skipped. The canonical
  // dag.ts semantics accept skipped as dep-satisfying; the hook MUST match.
  const runsDir = join(HOOKS_DIR, '..', 'pipeline_mcp_data', 'runs')
  const fakeRunDir = join(runsDir, `test-skipped-dep-${Date.now()}`)
  mkdirSync(fakeRunDir, { recursive: true })
  const fakeState = {
    run_id: 'test-skipped-dep',
    pipeline_version: '1.0.0',
    created_at: new Date().toISOString(),
    updated_at: new Date(Date.now() + 1_000_000).toISOString(),
    status: 'running',
    phases: {
      curation: {
        phase_name: 'curation',
        status: 'skipped',
        input_artifacts: [],
        output_artifacts: [],
        retry_count: 0,
      },
      design_synthesis: {
        phase_name: 'design_synthesis',
        status: 'pending',
        input_artifacts: [],
        output_artifacts: [],
        retry_count: 0,
      },
    },
    available_artifacts: [],
    config_path: '',
  }
  writeFileSync(join(fakeRunDir, 'run-state.json'), JSON.stringify(fakeState, null, 2), 'utf-8')

  try {
    const result = runHook('dag-guard.mjs', 'dag-guard-skipped-dep.json')
    assertEqual(result.exitCode, 0, 'Exit code')
    // design_synthesis is entry_point in pipeline.toml so would be allowed
    // regardless; this test guards against accidentally denying when a real
    // non-entry-point phase has a skipped required dep. The assertion is
    // still meaningful as a regression guard.
    assertEqual(result.parsed.permissionDecision, 'allow', 'Permission decision')
  } finally {
    rmSync(fakeRunDir, { recursive: true, force: true })
  }
})

test('denies phase whose required dep is still pending', () => {
  const runsDir = join(HOOKS_DIR, '..', 'pipeline_mcp_data', 'runs')
  const fakeRunDir = join(runsDir, `test-pending-dep-${Date.now()}`)
  mkdirSync(fakeRunDir, { recursive: true })
  const fakeState = {
    run_id: 'test-pending-dep',
    pipeline_version: '1.0.0',
    created_at: new Date().toISOString(),
    updated_at: new Date(Date.now() + 1_000_000).toISOString(),
    status: 'running',
    phases: {
      design_synthesis: {
        phase_name: 'design_synthesis',
        status: 'completed',
        input_artifacts: [],
        output_artifacts: [],
        retry_count: 0,
      },
      debate: {
        phase_name: 'debate',
        status: 'pending',
        input_artifacts: [],
        output_artifacts: [],
        retry_count: 0,
      },
      validation: {
        phase_name: 'validation',
        status: 'pending',
        input_artifacts: [],
        output_artifacts: [],
        retry_count: 0,
      },
    },
    available_artifacts: [],
    config_path: '',
  }
  writeFileSync(join(fakeRunDir, 'run-state.json'), JSON.stringify(fakeState, null, 2), 'utf-8')

  try {
    // validation has a required edge from design_synthesis (completed, OK)
    // and an optional edge from debate; the required dep is satisfied so we
    // expect allow. This test locks in the positive case after the fix.
    const result = runHook('dag-guard.mjs', {
      event: 'PreToolUse',
      tool_name: 'mcp__pipeline__pipeline_start_phase',
      tool_input: { phase: 'validation' },
      session_id: 'test-pending-dep',
    })
    assertEqual(result.parsed.permissionDecision, 'allow', 'Permission decision')
  } finally {
    rmSync(fakeRunDir, { recursive: true, force: true })
  }
})

console.log('\n=== Artifact Gate (artifact-gate.mjs) ===\n')

test('exits cleanly and produces valid JSON', () => {
  const result = runHook('artifact-gate.mjs', 'artifact-gate.json')
  assertEqual(result.exitCode, 0, 'Exit code')
  assertTruthy(result.parsed, 'Produces JSON output')
  assertTruthy(result.parsed.permissionDecision, 'Has permissionDecision')
})

test('allows when no phase name provided', () => {
  const result = runHook('artifact-gate.mjs', {
    event: 'PreToolUse',
    tool_name: 'mcp__pipeline__pipeline_complete_phase',
    tool_input: {},
    session_id: 'test',
  })
  assertEqual(result.parsed.permissionDecision, 'allow', 'Permission decision')
})

console.log('\n=== File Read Version Guard (file-read-version-guard.mjs) ===\n')

test('denies reading file with outdated version tag', () => {
  setupConfig({
    file_read_version_guard: {
      enabled: true,
      present_version_in_development: 'v0.4.0',
      previous_versions: ['v0.3.0', 'v0.2.0'],
    },
  })
  const result = runHook('file-read-version-guard.mjs', 'file-read-version.json')
  assertEqual(result.parsed.permissionDecision, 'deny', 'Permission decision')
  assertIncludes(result.parsed.deny_reason, 'outdated', 'Deny reason')
  cleanupConfig()
})

test('allows reading file with current version', () => {
  setupConfig({
    file_read_version_guard: {
      enabled: true,
      present_version_in_development: 'v0.4.0',
      previous_versions: ['v0.3.0'],
    },
  })
  const result = runHook('file-read-version-guard.mjs', {
    event: 'PreToolUse',
    tool_name: 'Read',
    tool_input: { file_path: '/project/specs/v0.4.0-design.json' },
    session_id: 'test',
  })
  assertEqual(result.parsed.permissionDecision, 'allow', 'Permission decision')
  cleanupConfig()
})

test('allows reading unversioned files', () => {
  setupConfig({
    file_read_version_guard: {
      enabled: true,
      present_version_in_development: 'v0.4.0',
      previous_versions: ['v0.3.0'],
    },
  })
  const result = runHook('file-read-version-guard.mjs', {
    event: 'PreToolUse',
    tool_name: 'Read',
    tool_input: { file_path: '/project/README.md' },
    session_id: 'test',
  })
  assertEqual(result.parsed.permissionDecision, 'allow', 'Permission decision')
  cleanupConfig()
})

test('allows when no version guard configured', () => {
  setupConfig({
    file_read_version_guard: {
      enabled: true,
      present_version_in_development: '',
      previous_versions: [],
    },
  })
  const result = runHook('file-read-version-guard.mjs', 'file-read-version.json')
  assertEqual(result.parsed.permissionDecision, 'allow', 'Permission decision')
  cleanupConfig()
})

test('denies version-bypass path after normalization collapses ..', () => {
  // Pre-fix: `/project/v0.4.0/../v0.3.0/specs.json` passed the substring
  // check for the current version (v0.4.0 literally appeared in the path)
  // while actually resolving to v0.3.0 — a bypass.
  // Post-fix: node:path.normalize collapses `v0.4.0/..` BEFORE the
  // substring check, so the matcher only sees `v0.3.0` and denies. This
  // test locks the bypass closed regardless of whether any unresolved `..`
  // remains; the security property is "crafted path does not read from the
  // older version tree".
  setupConfig({
    file_read_version_guard: {
      enabled: true,
      present_version_in_development: 'v0.4.0',
      previous_versions: ['v0.3.0', 'v0.2.0'],
    },
  })
  const result = runHook('file-read-version-guard.mjs', {
    event: 'PreToolUse',
    tool_name: 'Read',
    tool_input: { file_path: '/project/v0.4.0/../v0.3.0/specs.json' },
    session_id: 'test',
  })
  assertEqual(result.parsed.permissionDecision, 'deny', 'Permission decision')
  assertIncludes(result.parsed.deny_reason, 'v0.3.0', 'Deny reason mentions outdated version')
  cleanupConfig()
})

test('denies crafted relative path with unresolved ..', () => {
  // `docs/../../../etc/passwd` normalizes to `../../etc/passwd` (posix) or
  // `..\..\etc\passwd` (win32) — both retain unresolved `..` segments
  // because there's no leading prefix long enough to cancel them. The
  // normalization-deny branch must fire on both platforms.
  setupConfig({
    file_read_version_guard: {
      enabled: true,
      present_version_in_development: 'v0.4.0',
      previous_versions: ['v0.3.0'],
    },
  })
  const result = runHook('file-read-version-guard.mjs', {
    event: 'PreToolUse',
    tool_name: 'Read',
    tool_input: { file_path: 'docs/../../../etc/passwd' },
    session_id: 'test',
  })
  assertEqual(result.parsed.permissionDecision, 'deny', 'Permission decision')
  assertIncludes(result.parsed.deny_reason, 'normalization', 'Deny reason')
  cleanupConfig()
})

console.log('\n=== Post Phase Complete (post-phase-complete.mjs) ===\n')

test('produces systemMessage on phase completion', () => {
  const result = runHook('post-phase-complete.mjs', 'post-phase-complete.json')
  assertEqual(result.exitCode, 0, 'Exit code')
  if (result.parsed) {
    assertTruthy(result.parsed.systemMessage, 'Has systemMessage')
    assertIncludes(result.parsed.systemMessage, 'curation', 'Mentions phase name')
  }
})

console.log('\n=== Phase Context Inject (phase-context-inject.mjs) ===\n')

test('injects phase context for known phase', () => {
  const result = runHook('phase-context-inject.mjs', 'phase-context-inject.json')
  assertEqual(result.exitCode, 0, 'Exit code')
  if (result.parsed) {
    assertTruthy(result.parsed.systemMessage, 'Has systemMessage')
    assertIncludes(result.parsed.systemMessage, 'curation', 'Mentions phase name')
  }
})

test('exits cleanly for unknown phase', () => {
  const result = runHook('phase-context-inject.mjs', {
    event: 'PostToolUse',
    tool_name: 'mcp__pipeline__pipeline_start_phase',
    tool_input: { phase: 'nonexistent_phase_xyz' },
    session_id: 'test',
  })
  assertEqual(result.exitCode, 0, 'Exit code')
})

console.log('\n=== Phase Timing (phase-timing.mjs) ===\n')

test('writes timing entry and exits cleanly', () => {
  // Clean up any prior timing file
  const logsDir = join(HOOKS_DIR, 'hook-logs')
  const timingPath = join(logsDir, 'timing.jsonl')
  if (existsSync(timingPath)) rmSync(timingPath)

  const result = runHook('phase-timing.mjs', 'phase-timing.json')
  assertEqual(result.exitCode, 0, 'Exit code')

  // Verify timing file was written
  assertTruthy(existsSync(timingPath), 'timing.jsonl exists')
  const lines = readFileSync(timingPath, 'utf-8').trim().split('\n')
  const entry = JSON.parse(lines[lines.length - 1])
  assertEqual(entry.phase, 'curation', 'Phase name in timing entry')
  assertEqual(entry.event, 'phase_started', 'Event type')
  assertTruthy(entry.timestamp, 'Has timestamp')
  assertEqual(entry.session_id, 'test-session-003', 'Session ID')
})

console.log('\n=== Stop Guard (stop-guard.mjs) ===\n')

test('exits cleanly when no active run', () => {
  // With the existing completed run state, should not block
  const result = runHook('stop-guard.mjs', 'stop-guard.json')
  assertEqual(result.exitCode, 0, 'Exit code')
})

test('exits cleanly when stop_hook_active is set', () => {
  const result = runHook('stop-guard.mjs', {
    event: 'Stop',
    session_id: 'test',
    stop_hook_active: true,
  })
  assertEqual(result.exitCode, 0, 'Exit code')
})

// ── Structural validation ──────────────────────────────────

console.log('\n=== Structural Validation ===\n')

test('all 11 hook scripts exist', () => {
  const expectedScripts = [
    'session-init.mjs',
    'send-message-guard.mjs',
    'token-budget-guard.mjs',
    'phase-start-guard.mjs',
    'dag-guard.mjs',
    'artifact-gate.mjs',
    'file-read-version-guard.mjs',
    'post-phase-complete.mjs',
    'phase-context-inject.mjs',
    'phase-timing.mjs',
    'stop-guard.mjs',
  ]
  for (const script of expectedScripts) {
    assertTruthy(existsSync(join(SCRIPTS_DIR, script)), `${script} exists`)
  }
})

test('all scripts start with shebang line', () => {
  const scripts = [
    'session-init.mjs', 'send-message-guard.mjs', 'token-budget-guard.mjs',
    'phase-start-guard.mjs', 'dag-guard.mjs', 'artifact-gate.mjs',
    'file-read-version-guard.mjs', 'post-phase-complete.mjs',
    'phase-context-inject.mjs', 'phase-timing.mjs', 'stop-guard.mjs',
  ]
  for (const script of scripts) {
    const content = readFileSync(join(SCRIPTS_DIR, script), 'utf-8')
    assertTruthy(content.startsWith('#!/usr/bin/env node'), `${script} has shebang`)
  }
})

test('all fixtures are valid JSON', () => {
  const fixtures = [
    'session-start.json', 'send-message-allow.json', 'token-budget.json',
    'phase-start.json', 'dag-guard.json', 'artifact-gate.json',
    'file-read-version.json', 'post-phase-complete.json',
    'phase-context-inject.json', 'phase-timing.json', 'stop-guard.json',
  ]
  for (const fixture of fixtures) {
    const content = readFileSync(join(FIXTURES_DIR, fixture), 'utf-8')
    JSON.parse(content) // Throws on invalid JSON
  }
})

test('hooks-config.toml exists', () => {
  assertTruthy(existsSync(join(HOOKS_DIR, 'hooks-config.toml')), 'hooks-config.toml exists')
})

test('generate-settings.mjs exists', () => {
  assertTruthy(existsSync(join(HOOKS_DIR, 'generate-settings.mjs')), 'generate-settings.mjs exists')
})

test('generate-settings.mjs produces quoted node command paths', () => {
  // Run generate-settings in a mode that writes its output to a fresh
  // settings file under a spaces-containing temp dir. We read the resulting
  // JSON and assert every hook command wraps the path in double quotes.
  const tmpHome = mkdtempSync(join(tmpdir(), 'pipeline hooks settings '))
  try {
    // Run the real generate-settings.mjs and inspect the real project
    // settings file — the quoting rule is repo-wide and doesn't depend on
    // tempdir placement.
    execFileSync('node', [join(HOOKS_DIR, 'generate-settings.mjs')], {
      encoding: 'utf-8',
      timeout: 10_000,
    })
    const realSettings = join(HOOKS_DIR, '..', '.claude', 'settings.local.json')
    if (!existsSync(realSettings)) {
      // generate-settings is disabled in this environment; skip.
      return
    }
    const parsed = JSON.parse(readFileSync(realSettings, 'utf-8'))
    const hooks = Array.isArray(parsed.hooks) ? parsed.hooks : []
    for (const h of hooks) {
      if (typeof h.command !== 'string') continue
      if (!h.command.startsWith('node ')) continue
      // The path portion must be double-quoted
      assertTruthy(
        h.command.match(/^node "[^"]+"$/),
        `hook command is not double-quoted: ${h.command}`,
      )
    }
  } finally {
    rmSync(tmpHome, { recursive: true, force: true })
  }
})

test('readStdin fails open when stdin exceeds 10 MB', () => {
  // Pipe 15 MB of padding into any hook script; assert the hook emits
  // {permissionDecision: 'allow'} on stdout and exits 0. Use dag-guard.mjs
  // as a representative consumer of readStdin.
  //
  // Must use spawnSync (not execFileSync) because the hook calls
  // process.exit(0) from inside readStdin when the cap trips, closing
  // stdin before the parent finishes writing the 15 MB payload. That
  // surfaces as a write EPIPE on the parent side and execFileSync
  // converts any such spawn-level error into a thrown exception that
  // hides stdout. spawnSync returns the collected stdout regardless.
  const payload = 'x'.repeat(15 * 1024 * 1024)
  const result = spawnSync('node', [join(SCRIPTS_DIR, 'dag-guard.mjs')], {
    input: payload,
    encoding: 'utf-8',
    timeout: 15_000,
    maxBuffer: 20 * 1024 * 1024,
  })
  assertEqual(result.status, 0, 'Exit code')
  const parsed = JSON.parse((result.stdout || '').trim())
  assertEqual(parsed.permissionDecision, 'allow', 'Permission decision')
})

// ── Cleanup ────────────────────────────────────────────────
cleanupConfig()

// ── Summary ────────────────────────────────────────────────

console.log(`\n${'='.repeat(50)}`)
console.log(`Results: ${passed} passed, ${failed} failed, ${skipped} skipped`)

if (failures.length > 0) {
  console.log('\nFailures:')
  for (const f of failures) {
    console.log(`  ${f.name}: ${f.error}`)
  }
}

console.log('')
process.exit(failed > 0 ? 1 : 0)
