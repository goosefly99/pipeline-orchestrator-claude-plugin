import { describe, it, beforeEach, afterEach } from 'node:test'
import assert from 'node:assert/strict'
import {
  mkdtempSync,
  rmSync,
  writeFileSync,
  mkdirSync,
} from 'node:fs'
import { join } from 'node:path'
import { tmpdir } from 'node:os'
import {
  extractProposedDiffAnchorRefs,
  markdownHasHeading,
  handleRegisterScaffoldOutputs,
  type RegisterScaffoldOutputsContext,
} from '../misc-handlers.ts'
import type { RunState } from '../types.ts'
import { initRun, addArtifact } from '../run-state.ts'

/**
 * Build a RegisterScaffoldOutputsContext backed by real run-state helpers.
 * Mirrors the pattern used in scaffold-register-outputs.test.ts, but points
 * `baseDirFromRunDir` at a subdirectory of the root tmp dir so that the
 * project-root lookup (`resolve(baseDir, '..')`) lands inside the test harness.
 */
function makeCtx(
  baseDir: string,
  runState: { current: RunState },
  runDir: string,
): RegisterScaffoldOutputsContext {
  return {
    requireRun: () => runState.current,
    setActiveRun: (state) => {
      runState.current = state
    },
    getActiveRun: () => null,
    getActiveRunDir: () => runDir,
    baseDirFromRunDir: () => baseDir,
    addArtifact: (state, ref, dir) => addArtifact(state, ref, dir),
  }
}

describe('extractProposedDiffAnchorRefs', () => {
  it('returns [] for empty string', () => {
    assert.deepEqual(extractProposedDiffAnchorRefs(''), [])
  })

  it('returns [] for content with no anchor-reference phrases', () => {
    const content = [
      '# Proposed Diff',
      'This file is a placeholder patch with no anchor references at all.',
      'It contains prose about testing and ## headings in body text only.',
    ].join('\n')
    assert.deepEqual(extractProposedDiffAnchorRefs(content), [])
  })

  it('captures a double-quoted "under the existing" section reference', () => {
    const content =
      'Apply the section below by inserting it into AGENTS.md under the existing "Quality Gates" section.'
    assert.deepEqual(extractProposedDiffAnchorRefs(content), ['quality gates'])
  })

  it('captures a single-quoted "under the existing" section reference', () => {
    const content =
      "Insert the patch under the existing 'Core Patterns' section of the file."
    assert.deepEqual(extractProposedDiffAnchorRefs(content), ['core patterns'])
  })

  it('captures "insert after" phrasing', () => {
    const content =
      'Insert after the "Architectural Constraints" section so it lands near the top.'
    assert.deepEqual(extractProposedDiffAnchorRefs(content), [
      'architectural constraints',
    ])
  })

  it('captures "under the X heading" phrasing (without "existing")', () => {
    const content =
      'Put the new note under the "Operational Notes" heading for visibility.'
    assert.deepEqual(extractProposedDiffAnchorRefs(content), [
      'operational notes',
    ])
  })

  it('does NOT match a "Create a new top-level" declaration', () => {
    const content =
      'Create a new top-level `## Quality Gates` section positioned after `## Core Patterns`.'
    assert.deepEqual(extractProposedDiffAnchorRefs(content), [])
  })

  it('normalizes backtick-quoted ATX headings by stripping leading hashes', () => {
    const content =
      'Apply the diff under the existing `## Quality Gates` section of AGENTS.md.'
    assert.deepEqual(extractProposedDiffAnchorRefs(content), ['quality gates'])
  })

  it('dedupes repeated anchor references across phrasings', () => {
    const content = [
      'Insert it into AGENTS.md under the existing "Quality Gates" section.',
      'Ensure it lives under the existing "Quality Gates" section.',
      'Specifically: insert after the "Quality Gates" section header.',
    ].join('\n')
    assert.deepEqual(extractProposedDiffAnchorRefs(content), ['quality gates'])
  })

  it('lowercases captured headings for case-insensitive downstream matching', () => {
    const content =
      'Place it under the existing "QUALITY GATES" section, please.'
    assert.deepEqual(extractProposedDiffAnchorRefs(content), ['quality gates'])
  })
})

describe('markdownHasHeading', () => {
  it('returns true for an exact h2 heading match', () => {
    const content = ['# Title', '', '## Quality Gates', '', 'Body.'].join('\n')
    assert.equal(markdownHasHeading(content, 'Quality Gates'), true)
  })

  it('returns true for case-insensitive heading matches', () => {
    const content = '## Quality Gates\n\nBody text.\n'
    assert.equal(markdownHasHeading(content, 'quality gates'), true)
    assert.equal(markdownHasHeading(content, 'QUALITY GATES'), true)
  })

  it('returns false when the heading is absent', () => {
    const content = '## Core Patterns\n\nBody text.\n'
    assert.equal(markdownHasHeading(content, 'Quality Gates'), false)
  })

  it('returns true for h1, h3, and h6 headings (not only h2)', () => {
    const h1 = '# Quality Gates\n'
    const h3 = '### Quality Gates\n'
    const h6 = '###### Quality Gates\n'
    assert.equal(markdownHasHeading(h1, 'Quality Gates'), true)
    assert.equal(markdownHasHeading(h3, 'Quality Gates'), true)
    assert.equal(markdownHasHeading(h6, 'Quality Gates'), true)
  })

  it('returns false when the heading appears only in body text (not as a heading)', () => {
    const content = [
      '# Title',
      '',
      'Here we mention Quality Gates in prose, but there is no heading.',
      'Quality Gates appear inline only.',
    ].join('\n')
    assert.equal(markdownHasHeading(content, 'Quality Gates'), false)
  })

  it('returns false for empty content or empty needle', () => {
    assert.equal(markdownHasHeading('', 'Quality Gates'), false)
    assert.equal(markdownHasHeading('## Quality Gates\n', ''), false)
  })
})

describe('handleRegisterScaffoldOutputs anchor validation', () => {
  let rootDir: string
  let baseDir: string
  let runDir: string
  let scaffoldDir: string

  beforeEach(() => {
    // rootDir acts as the "project root"; baseDir is a subdirectory so that
    // `resolve(baseDir, '..')` lands inside rootDir and the handler can find
    // target files written there. This mirrors the real layout where
    // pipeline_mcp_data/ lives at the project root.
    rootDir = mkdtempSync(join(tmpdir(), 'fix35-'))
    baseDir = join(rootDir, 'pipeline_mcp_data')
    runDir = join(baseDir, 'runs', 'scaffold-run')
    scaffoldDir = join(baseDir, 'scaffold')
    mkdirSync(runDir, { recursive: true })
    mkdirSync(scaffoldDir, { recursive: true })
  })

  afterEach(() => {
    rmSync(rootDir, { recursive: true, force: true })
  })

  it('emits an anchor warning when the proposed diff references a heading missing from the target file', () => {
    // Target file exists at the project root but does NOT contain the heading
    // the proposed diff claims is already there.
    writeFileSync(
      join(rootDir, 'AGENTS.md'),
      [
        '# AGENTS',
        '',
        '## Core Patterns',
        '',
        'Unrelated content — note that there is no "Quality Gates" heading.',
      ].join('\n'),
      'utf-8',
    )

    writeFileSync(
      join(scaffoldDir, 'AGENTS.md.proposed-diff.md'),
      [
        '# AGENTS.md proposed diff',
        '',
        'Apply the section below by inserting it into `AGENTS.md` under the existing "Quality Gates" section.',
      ].join('\n'),
      'utf-8',
    )

    const runState = {
      current: initRun(
        'scaffold-anchor-run',
        '1.0.0',
        ['implementation_scaffold'],
        runDir,
      ),
    }
    const ctx = makeCtx(baseDir, runState, runDir)

    const result = handleRegisterScaffoldOutputs({}, ctx)
    assert.ok(!result.isError, 'response should not be an error')

    const parsed = JSON.parse(result.json) as {
      status: string
      data: {
        scaffold_dir: string
        registered: Array<{ filename: string }>
        skipped_existing: string[]
        anchor_warnings?: Array<{
          filename: string
          target_file: string
          missing_anchor: string
        }>
      }
      warnings?: string[]
    }

    // Registration still succeeds — validation is advisory, not blocking.
    assert.equal(parsed.status, 'ok')
    assert.equal(parsed.data.registered.length, 1)
    assert.equal(parsed.data.registered[0].filename, 'AGENTS.md.proposed-diff.md')
    assert.equal(runState.current.available_artifacts.length, 1)

    // The envelope must surface the warning through BOTH the top-level
    // `warnings` field AND the structured `data.anchor_warnings` list.
    assert.ok(
      Array.isArray(parsed.warnings) && parsed.warnings.length >= 1,
      'envelope should include a warnings array when anchors are missing',
    )
    assert.ok(
      parsed.warnings!.some((w) => w.toLowerCase().includes('quality gates')),
      'warnings array should mention the missing anchor',
    )
    assert.ok(
      parsed.data.anchor_warnings && parsed.data.anchor_warnings.length === 1,
      'data.anchor_warnings should contain exactly one entry',
    )
    assert.equal(parsed.data.anchor_warnings![0].filename, 'AGENTS.md.proposed-diff.md')
    assert.equal(parsed.data.anchor_warnings![0].target_file, 'AGENTS.md')
    assert.equal(parsed.data.anchor_warnings![0].missing_anchor, 'quality gates')
  })

  it('emits no anchor warnings when the referenced heading exists in the target file', () => {
    // Target file exists and DOES contain the heading the proposed diff
    // references — no warning should be produced.
    writeFileSync(
      join(rootDir, 'AGENTS.md'),
      [
        '# AGENTS',
        '',
        '## Quality Gates',
        '',
        'Existing quality gates content goes here.',
      ].join('\n'),
      'utf-8',
    )

    writeFileSync(
      join(scaffoldDir, 'AGENTS.md.proposed-diff.md'),
      [
        '# AGENTS.md proposed diff',
        '',
        'Apply the patch under the existing "Quality Gates" section.',
      ].join('\n'),
      'utf-8',
    )

    const runState = {
      current: initRun(
        'scaffold-anchor-run-2',
        '1.0.0',
        ['implementation_scaffold'],
        runDir,
      ),
    }
    const ctx = makeCtx(baseDir, runState, runDir)

    const result = handleRegisterScaffoldOutputs({}, ctx)
    assert.ok(!result.isError, 'response should not be an error')

    const parsed = JSON.parse(result.json) as {
      status: string
      data: {
        registered: Array<{ filename: string }>
        anchor_warnings?: unknown
      }
      warnings?: unknown
    }

    assert.equal(parsed.status, 'ok')
    assert.equal(parsed.data.registered.length, 1)
    // Neither surface should mention warnings when the anchor resolves.
    assert.equal(
      parsed.warnings,
      undefined,
      'envelope should not include a warnings field when anchors resolve',
    )
    assert.equal(
      parsed.data.anchor_warnings,
      undefined,
      'data should not include anchor_warnings when anchors resolve',
    )
  })

  it('skips validation (no error, no warning) when the target file is absent', () => {
    // No target file at the project root. The proposed diff references a
    // heading, but since the target is not on disk we cannot validate and the
    // handler should quietly skip validation rather than fail registration.
    writeFileSync(
      join(scaffoldDir, 'AGENTS.md.proposed-diff.md'),
      'Apply this under the existing "Some Heading" section.',
      'utf-8',
    )

    const runState = {
      current: initRun(
        'scaffold-anchor-run-3',
        '1.0.0',
        ['implementation_scaffold'],
        runDir,
      ),
    }
    const ctx = makeCtx(baseDir, runState, runDir)

    const result = handleRegisterScaffoldOutputs({}, ctx)
    assert.ok(!result.isError)

    const parsed = JSON.parse(result.json) as {
      status: string
      data: { registered: unknown[]; anchor_warnings?: unknown }
      warnings?: unknown
    }

    assert.equal(parsed.status, 'ok')
    assert.equal(parsed.data.registered.length, 1)
    assert.equal(parsed.warnings, undefined)
    assert.equal(parsed.data.anchor_warnings, undefined)
  })
})
