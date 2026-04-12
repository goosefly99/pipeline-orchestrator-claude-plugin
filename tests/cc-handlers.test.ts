import { describe, it } from 'node:test'
import assert from 'node:assert/strict'
import { handleCCGetItems, handleCCSaveOverview } from '../cc-handlers.ts'
import type { CCContext } from '../cc-handlers.ts'
import type { ResearchItem, KnowledgeOverview } from '../cc-types.ts'
import { collectConcepts } from '../concepts.ts'

// ── Helpers ───────────────────────────────────────────────────

function makeItem(id: string, content: string): ResearchItem {
  return {
    id,
    title: `Item ${id}`,
    content,
    tags: [],
    metadata: {},
  }
}

function makeCtx(items: ResearchItem[]): CCContext {
  return {
    loadCollection: () => { throw new Error('not implemented') },
    queryItems: () => [],
    getItems: (_collection: string, ids: string[]) => items.filter(i => ids.includes(i.id)),
    collectConcepts: () => ({ synthesis_prompt: '' }),
    saveOverview: () => '',
    listOverviews: () => [],
    getOverview: () => null,
  }
}

function makeOverview(overrides?: Partial<KnowledgeOverview>): KnowledgeOverview {
  return {
    overview_id: 'test-id',
    title: 'Test Overview',
    created_date: new Date().toISOString(),
    status: 'draft',
    sources: [],
    summary: '',
    concepts: [],
    themes: [],
    key_findings: [],
    knowledge_gaps: [],
    open_questions: [],
    ...overrides,
  }
}

// ── handleCCGetItems truncation tests ─────────────────────────

describe('handleCCGetItems — content truncation', () => {
  it('truncates content to 500 chars by default when content exceeds limit', () => {
    const longContent = 'A'.repeat(600)
    const ctx = makeCtx([makeItem('i1', longContent)])

    const result = handleCCGetItems({ collection: 'test', item_ids: ['i1'] }, ctx)

    assert.ok(result.json.includes('A'.repeat(500) + '...'), 'should contain 500 As followed by ellipsis')
    assert.ok(!result.json.includes('A'.repeat(501)), 'should not contain 501+ consecutive As')
  })

  it('returns content as-is when content is under the default 500-char limit', () => {
    const shortContent = 'Hello world'
    const ctx = makeCtx([makeItem('i1', shortContent)])

    const result = handleCCGetItems({ collection: 'test', item_ids: ['i1'] }, ctx)

    assert.ok(result.json.includes('Hello world'), 'should include full short content')
    assert.ok(!result.json.includes('...'), 'should not append ellipsis for short content')
  })

  it('respects max_content_length override', () => {
    const content = 'B'.repeat(200)
    const ctx = makeCtx([makeItem('i1', content)])

    const result = handleCCGetItems(
      { collection: 'test', item_ids: ['i1'], max_content_length: 100 },
      ctx,
    )

    assert.ok(result.json.includes('B'.repeat(100) + '...'), 'should truncate at custom limit')
    assert.ok(!result.json.includes('B'.repeat(101)), 'should not include content past the custom limit')
  })

  it('returns complete content when full=true regardless of length', () => {
    const longContent = 'C'.repeat(2000)
    const ctx = makeCtx([makeItem('i1', longContent)])

    const result = handleCCGetItems({ collection: 'test', item_ids: ['i1'], full: true }, ctx)

    assert.ok(result.json.includes('C'.repeat(2000)), 'should contain all 2000 chars')
    assert.ok(!result.json.endsWith('...'), 'should not append ellipsis when full=true')
  })

  it('full=true overrides max_content_length', () => {
    const content = 'D'.repeat(1000)
    const ctx = makeCtx([makeItem('i1', content)])

    const result = handleCCGetItems(
      { collection: 'test', item_ids: ['i1'], full: true, max_content_length: 50 },
      ctx,
    )

    assert.ok(result.json.includes('D'.repeat(1000)), 'should return full content even with max_content_length set')
  })

  it('content at exactly the default limit is returned without ellipsis', () => {
    const exactContent = 'E'.repeat(500)
    const ctx = makeCtx([makeItem('i1', exactContent)])

    const result = handleCCGetItems({ collection: 'test', item_ids: ['i1'] }, ctx)

    assert.ok(result.json.includes('E'.repeat(500)), 'should include all 500 chars')
    assert.ok(!result.json.includes('E'.repeat(500) + '...'), 'should not append ellipsis at exact limit')
  })

  it('returns no items message when item_ids are not found', () => {
    const ctx = makeCtx([])

    const result = handleCCGetItems({ collection: 'test', item_ids: ['missing'] }, ctx)

    assert.ok(result.json.includes('No items found'), 'should report no items found')
  })

  it('throws when collection or item_ids are missing', () => {
    const ctx = makeCtx([])

    assert.throws(
      () => handleCCGetItems({ item_ids: ['i1'] }, ctx),
      /collection and item_ids are required/,
    )

    assert.throws(
      () => handleCCGetItems({ collection: 'test' }, ctx),
      /collection and item_ids are required/,
    )
  })
})

// ── handleCCSaveOverview — persistOverview routing (Feature C) ─

describe('handleCCSaveOverview', () => {
  it('prefers ctx.persistOverview when present and passes the output_dir override through', () => {
    const calls: { overview: KnowledgeOverview; outputDir?: string }[] = []
    const saveCalls: { overview: KnowledgeOverview; outputDir?: string }[] = []

    const ctx: CCContext = {
      ...makeCtx([]),
      saveOverview: (overview, outputDir) => {
        saveCalls.push({ overview, outputDir })
        return '/should/not/be/used.json'
      },
      persistOverview: (overview, outputDirOverride) => {
        calls.push({ overview, outputDir: outputDirOverride })
        return '/tmp/runs/abc/overviews/persisted--ov1.json'
      },
    }

    const overview = makeOverview({ overview_id: 'ov1', title: 'Persisted' })
    const result = handleCCSaveOverview(
      { overview, output_dir: '/custom/override' },
      ctx,
    )

    // Exactly one persistOverview call, zero saveOverview calls.
    assert.equal(calls.length, 1, 'persistOverview should be called exactly once')
    assert.equal(saveCalls.length, 0, 'saveOverview should not be called when persistOverview is present')
    assert.equal(calls[0].overview.overview_id, 'ov1')
    assert.equal(calls[0].outputDir, '/custom/override')

    // Handler mutated status to 'complete' before delegation.
    assert.equal(overview.status, 'complete')

    // Response lines: path, ID, title, concepts, themes.
    assert.ok(result.json.includes('/tmp/runs/abc/overviews/persisted--ov1.json'))
    assert.ok(result.json.includes('ID: ov1'))
    assert.ok(result.json.includes('Title: Persisted'))
    assert.ok(result.json.includes('Concepts: 0'))
    assert.ok(result.json.includes('Themes: 0'))
  })

  it('falls back to ctx.saveOverview when ctx.persistOverview is absent', () => {
    const saveCalls: { overview: KnowledgeOverview; outputDir?: string }[] = []

    const ctx: CCContext = {
      ...makeCtx([]),
      saveOverview: (overview, outputDir) => {
        saveCalls.push({ overview, outputDir })
        return '/legacy/overviews/fallback--ov2.json'
      },
      // persistOverview intentionally omitted.
    }

    const overview = makeOverview({ overview_id: 'ov2', title: 'Fallback' })
    const result = handleCCSaveOverview({ overview }, ctx)

    assert.equal(saveCalls.length, 1)
    assert.equal(saveCalls[0].overview.overview_id, 'ov2')
    assert.equal(saveCalls[0].outputDir, undefined)
    assert.ok(result.json.includes('/legacy/overviews/fallback--ov2.json'))
    assert.ok(result.json.includes('ID: ov2'))
    assert.ok(result.json.includes('Title: Fallback'))
  })

  it('throws when overview is missing', () => {
    const ctx = makeCtx([])
    assert.throws(
      () => handleCCSaveOverview({}, ctx),
      /overview is required/,
    )
  })
})

// ── collectConcepts — theme template correctness ───────────────

describe('collectConcepts — theme template and prompt', () => {
  function makeResearchItem(id: string, content: string): ResearchItem {
    return { id, title: `Item ${id}`, content, tags: [], metadata: {} }
  }

  it('synthesis_prompt contains "concept_names" in theme instructions', () => {
    const sourceGroups = [
      { collection: 'test-collection', items: [makeResearchItem('r1', 'Some research content')] },
    ]
    const { synthesis_prompt } = collectConcepts(sourceGroups, { title: 'Test Overview' })
    assert.ok(
      synthesis_prompt.includes('concept_names'),
      'synthesis_prompt must mention "concept_names" in theme instructions',
    )
  })

  it('synthesis_prompt theme instructions do not use "concepts" as the theme field name', () => {
    const sourceGroups = [
      { collection: 'test-collection', items: [makeResearchItem('r1', 'Some research content')] },
    ]
    const { synthesis_prompt } = collectConcepts(sourceGroups, { title: 'Test Overview' })

    const themesBlockMatch = synthesis_prompt.match(/For themes:[\s\S]*?(?=\n\n|\n##|$)/)
    assert.ok(themesBlockMatch, 'synthesis_prompt must contain a "For themes:" block')
    const themesBlock = themesBlockMatch![0]

    assert.ok(
      !themesBlock.match(/["']concepts["']\s*:/),
      'theme instructions must not reference "concepts": as a JSON key',
    )
  })

  it('template.themes[0] has concept_names key and not concepts key', () => {
    const sourceGroups = [
      { collection: 'test-collection', items: [makeResearchItem('r1', 'Some research content')] },
    ]
    const { template } = collectConcepts(sourceGroups, { title: 'Test Overview' })

    assert.ok(Array.isArray(template.themes), 'template.themes must be an array')
    assert.ok(template.themes.length > 0, 'template.themes must contain at least one placeholder')

    const placeholder = template.themes[0] as unknown as Record<string, unknown>
    assert.ok(
      Object.prototype.hasOwnProperty.call(placeholder, 'concept_names'),
      'template.themes[0] must have "concept_names" key',
    )
    assert.ok(
      !Object.prototype.hasOwnProperty.call(placeholder, 'concepts'),
      'template.themes[0] must NOT have old "concepts" key',
    )
  })
})
