import { describe, it, beforeEach, afterEach } from 'node:test'
import assert from 'node:assert/strict'
import { mkdtempSync, rmSync, existsSync } from 'node:fs'
import { join } from 'node:path'
import { tmpdir } from 'node:os'
import {
  collectConcepts,
  saveOverview,
  listOverviews,
  getOverview,
} from '../concepts.ts'
import type { ResearchItem, KnowledgeOverview } from '../cc-types.ts'

let tempDir: string

beforeEach(() => {
  tempDir = mkdtempSync(join(tmpdir(), 'concepts-test-'))
})

afterEach(() => {
  rmSync(tempDir, { recursive: true, force: true })
})

function makeItem(overrides: Partial<ResearchItem> & { id: string }): ResearchItem {
  return {
    title: '',
    content: 'default content',
    tags: [],
    metadata: {},
    ...overrides,
  }
}

function makeOverview(overrides?: Partial<KnowledgeOverview>): KnowledgeOverview {
  return {
    overview_id: 'test-id-1234',
    title: 'Test Overview',
    created_date: new Date().toISOString(),
    status: 'draft',
    sources: [],
    summary: 'A test summary',
    concepts: [],
    themes: [],
    key_findings: [],
    knowledge_gaps: [],
    open_questions: [],
    ...overrides,
  }
}

// ── collectConcepts ───────────────────────────────────────────

describe('collectConcepts', () => {
  it('returns a template and synthesis_prompt', () => {
    const result = collectConcepts(
      [{ collection: 'test-col', items: [makeItem({ id: 'i1', content: 'hello' })] }],
      { title: 'My Overview' },
    )

    assert.ok(result.template)
    assert.ok(result.synthesis_prompt)
    assert.equal(typeof result.synthesis_prompt, 'string')
    assert.ok(result.synthesis_prompt.length > 0)
  })

  it('template has correct structure', () => {
    const result = collectConcepts(
      [{ collection: 'c1', items: [makeItem({ id: 'i1' })] }],
      { title: 'Structure Test' },
    )
    const t = result.template

    assert.equal(t.title, 'Structure Test')
    assert.equal(t.status, 'draft')
    assert.ok(t.overview_id.length > 0)
    assert.ok(t.created_date.length > 0)
    assert.ok(Array.isArray(t.sources))
    assert.ok(Array.isArray(t.concepts))
    assert.ok(Array.isArray(t.themes))
    assert.equal(t.summary, '')
    assert.deepEqual(t.concepts, [])
    assert.deepEqual(t.themes, [])
  })

  it('template sources list all items from all groups', () => {
    const result = collectConcepts(
      [
        { collection: 'col-a', items: [makeItem({ id: 'a1' }), makeItem({ id: 'a2' })] },
        { collection: 'col-b', items: [makeItem({ id: 'b1' })] },
      ],
      { title: 'Multi-source' },
    )

    assert.equal(result.template.sources.length, 3)
    assert.ok(result.template.sources.some(s => s.item_id === 'a1' && s.collection === 'col-a'))
    assert.ok(result.template.sources.some(s => s.item_id === 'b1' && s.collection === 'col-b'))
  })

  it('synthesis_prompt includes the title', () => {
    const result = collectConcepts(
      [{ collection: 'c', items: [makeItem({ id: 'i' })] }],
      { title: 'Unique Title XYZ' },
    )
    assert.ok(result.synthesis_prompt.includes('Unique Title XYZ'))
  })

  it('synthesis_prompt includes source item content', () => {
    const result = collectConcepts(
      [{ collection: 'c', items: [makeItem({ id: 'i1', content: 'Specific research finding about markets' })] }],
      { title: 'Content Test' },
    )
    assert.ok(result.synthesis_prompt.includes('Specific research finding about markets'))
  })

  it('synthesis_prompt includes item ID for reference', () => {
    const result = collectConcepts(
      [{ collection: 'c', items: [makeItem({ id: 'item-abc-123', content: 'data' })] }],
      { title: 'ID Test' },
    )
    assert.ok(result.synthesis_prompt.includes('item-abc-123'))
  })

  it('synthesis_prompt includes focus clause when provided', () => {
    const result = collectConcepts(
      [{ collection: 'c', items: [makeItem({ id: 'i' })] }],
      { title: 'Focus Test', focus: 'risk management' },
    )
    assert.ok(result.synthesis_prompt.includes('risk management'))
    assert.ok(result.synthesis_prompt.includes('Focus area'))
  })

  it('synthesis_prompt omits focus clause when not provided', () => {
    const result = collectConcepts(
      [{ collection: 'c', items: [makeItem({ id: 'i' })] }],
      { title: 'No Focus' },
    )
    assert.ok(!result.synthesis_prompt.includes('Focus area'))
  })

  it('respects depth parameter in instructions', () => {
    const brief = collectConcepts(
      [{ collection: 'c', items: [makeItem({ id: 'i' })] }],
      { title: 'Brief', depth: 'brief' },
    )
    assert.ok(brief.synthesis_prompt.includes('3-5 top-level concepts'))

    const deep = collectConcepts(
      [{ collection: 'c', items: [makeItem({ id: 'i' })] }],
      { title: 'Deep', depth: 'deep' },
    )
    assert.ok(deep.synthesis_prompt.includes('every distinct concept'))
  })

  it('includes author info in prompt when present', () => {
    const result = collectConcepts(
      [{ collection: 'c', items: [makeItem({ id: 'i', author: { name: 'Alice', handle: '@alice' } })] }],
      { title: 'Author Test' },
    )
    assert.ok(result.synthesis_prompt.includes('Alice'))
    assert.ok(result.synthesis_prompt.includes('@alice'))
  })

  it('includes tags in prompt when present', () => {
    const result = collectConcepts(
      [{ collection: 'c', items: [makeItem({ id: 'i', tags: ['ml', 'finance'] })] }],
      { title: 'Tags Test' },
    )
    assert.ok(result.synthesis_prompt.includes('ml'))
    assert.ok(result.synthesis_prompt.includes('finance'))
  })

  it('includes cross-reference analysis for multiple items with shared tags', () => {
    const result = collectConcepts(
      [{
        collection: 'c',
        items: [
          makeItem({ id: 'i1', tags: ['shared-tag', 'unique1'] }),
          makeItem({ id: 'i2', tags: ['shared-tag', 'unique2'] }),
        ],
      }],
      { title: 'CrossRef Test' },
    )
    assert.ok(result.synthesis_prompt.includes('Cross-Reference'))
    assert.ok(result.synthesis_prompt.includes('shared-tag'))
  })

  it('includes JSON template in prompt for LLM to fill', () => {
    const result = collectConcepts(
      [{ collection: 'c', items: [makeItem({ id: 'i' })] }],
      { title: 'Template Test' },
    )
    assert.ok(result.synthesis_prompt.includes('"overview_id"'))
    assert.ok(result.synthesis_prompt.includes('"concepts"'))
    assert.ok(result.synthesis_prompt.includes('"themes"'))
  })
})

// ── collectConcepts — chunked mode ────────────────────────────

describe('collectConcepts chunked mode', () => {
  function makeItems(count: number): ResearchItem[] {
    return Array.from({ length: count }, (_, i) => makeItem({ id: `item-${i + 1}`, content: `content for item ${i + 1}` }))
  }

  it('small collection (≤15 items) returns no chunk field', () => {
    const items = makeItems(10)
    const result = collectConcepts(
      [{ collection: 'col', items }],
      { title: 'Small Collection' },
    )

    assert.equal(result.chunk, undefined)
    assert.ok(result.synthesis_prompt.length > 0)
    assert.ok(result.template)
  })

  it('large collection (20 items) activates chunk mode with 8 items in first chunk', () => {
    const items = makeItems(20)
    const result = collectConcepts(
      [{ collection: 'col', items }],
      { title: 'Large Collection' },
    )

    assert.ok(result.chunk, 'chunk field should be present')
    assert.equal(result.chunk!.chunk_index, 0)
    assert.equal(result.chunk!.item_count, 8)
    assert.equal(result.chunk!.total_items, 20)
    assert.equal(result.chunk!.total_chunks, 3)
    assert.equal(result.chunk!.is_final_chunk, false)
    assert.ok(typeof result.chunk!.continuation_token === 'string', 'continuation_token should be set')
  })

  it('continuation_token is base64 encoding of the next chunk index', () => {
    const items = makeItems(20)
    const result = collectConcepts(
      [{ collection: 'col', items }],
      { title: 'Token Test' },
    )

    assert.ok(result.chunk)
    const decoded = Buffer.from(result.chunk!.continuation_token!, 'base64').toString('utf-8')
    assert.equal(decoded, '1')
  })

  it('last chunk has is_final_chunk=true and no continuation_token', () => {
    const items = makeItems(20)
    // 20 items, CHUNK_SIZE=8: chunk 0 = items 0-7, chunk 1 = items 8-15, chunk 2 = items 16-19 (final)
    const result = collectConcepts(
      [{ collection: 'col', items }],
      { title: 'Final Chunk', chunk_index: 2 },
    )

    assert.ok(result.chunk)
    assert.equal(result.chunk!.is_final_chunk, true)
    assert.equal(result.chunk!.continuation_token, undefined)
    assert.equal(result.chunk!.item_count, 4)  // items 16-19
  })

  it('chunk_index=1 processes items 8-15 (second chunk)', () => {
    const items = makeItems(20)
    const result = collectConcepts(
      [{ collection: 'col', items }],
      { title: 'Second Chunk', chunk_index: 1 },
    )

    assert.ok(result.chunk)
    assert.equal(result.chunk!.chunk_index, 1)
    assert.equal(result.chunk!.item_count, 8)
    assert.equal(result.chunk!.is_final_chunk, false)
    // Items 8-15 should appear in the prompt
    assert.ok(result.synthesis_prompt.includes('item-9'))   // 1-indexed
    assert.ok(result.synthesis_prompt.includes('item-16'))
    assert.ok(!result.synthesis_prompt.includes('item-1\n') && !result.synthesis_prompt.includes('[item-1]'))
  })

  it('total_chunks is Math.ceil(totalItems / 8)', () => {
    for (const [count, expected] of [[16, 2], [17, 3], [24, 3], [25, 4]] as const) {
      const items = makeItems(count)
      const result = collectConcepts(
        [{ collection: 'col', items }],
        { title: `Count ${count}` },
      )
      assert.ok(result.chunk, `chunk expected for count ${count}`)
      assert.equal(result.chunk!.total_chunks, expected, `total_chunks for ${count} items`)
    }
  })

  it('synthesis_prompt includes chunk header when chunking is active', () => {
    const items = makeItems(20)
    const result = collectConcepts(
      [{ collection: 'col', items }],
      { title: 'Chunk Header Test' },
    )

    assert.ok(result.chunk)
    assert.ok(result.synthesis_prompt.includes('Chunk:'))
    assert.ok(result.synthesis_prompt.includes('of 3'))
    assert.ok(result.synthesis_prompt.includes('of 20'))
  })

  it('no chunk header in prompt for small collection', () => {
    const items = makeItems(5)
    const result = collectConcepts(
      [{ collection: 'col', items }],
      { title: 'No Chunk Header' },
    )

    assert.equal(result.chunk, undefined)
    assert.ok(!result.synthesis_prompt.includes('**Chunk:**'))
  })

  it('chunk continuation round-trip: decode token from chunk 0 and use it to fetch chunk 1', () => {
    const items = makeItems(20)

    // Step 1: fetch chunk 0 (no chunk_index provided, defaults to 0)
    const chunk0Result = collectConcepts(
      [{ collection: 'col', items }],
      { title: 'Round-Trip Test' },
    )

    assert.ok(chunk0Result.chunk, 'chunk 0 should have chunk metadata')
    assert.equal(chunk0Result.chunk!.chunk_index, 0)
    assert.ok(typeof chunk0Result.chunk!.continuation_token === 'string', 'chunk 0 should have a continuation_token')

    // Step 2: decode the token to get the next chunk index
    const token = chunk0Result.chunk!.continuation_token!
    const decoded = Buffer.from(token, 'base64').toString('utf-8')
    const nextChunkIndex = parseInt(decoded, 10)
    assert.equal(nextChunkIndex, 1, 'decoded token should indicate chunk index 1')

    // Step 3: fetch chunk 1 using the decoded index
    const chunk1Result = collectConcepts(
      [{ collection: 'col', items }],
      { title: 'Round-Trip Test', chunk_index: nextChunkIndex },
    )

    assert.ok(chunk1Result.chunk, 'chunk 1 should have chunk metadata')
    assert.equal(chunk1Result.chunk!.chunk_index, 1)
    assert.equal(chunk1Result.chunk!.item_count, 8, 'chunk 1 should contain 8 items (items 8-15)')
    assert.equal(chunk1Result.chunk!.is_final_chunk, false, 'chunk 1 should not be the final chunk')
    assert.ok(typeof chunk1Result.chunk!.continuation_token === 'string', 'chunk 1 should provide a token for chunk 2')

    // Step 4: verify chunk 1 prompt contains items from the correct slice (items 8-15 = item-9 through item-16)
    assert.ok(chunk1Result.synthesis_prompt.includes('item-9'), 'chunk 1 prompt should include item-9')
    assert.ok(chunk1Result.synthesis_prompt.includes('item-16'), 'chunk 1 prompt should include item-16')
  })
})

// ── saveOverview / listOverviews / getOverview round-trip ─────

describe('saveOverview', () => {
  it('saves an overview to disk and returns the file path', () => {
    const overview = makeOverview({ title: 'Save Test' })
    const path = saveOverview(overview, tempDir)

    assert.ok(existsSync(path))
    assert.ok(path.includes('save-test'))
    assert.ok(path.endsWith('.json'))
  })

  it('creates the output directory if it does not exist', () => {
    const subDir = join(tempDir, 'nested', 'output')
    const overview = makeOverview({ title: 'Dir Create' })
    const path = saveOverview(overview, subDir)

    assert.ok(existsSync(path))
    assert.ok(path.startsWith(subDir))
  })

  it('generates slug from title', () => {
    const overview = makeOverview({ title: 'My Complex Title! @#$' })
    const path = saveOverview(overview, tempDir)

    assert.ok(path.includes('my-complex-title'))
    // Special chars stripped
    assert.ok(!path.includes('@'))
    assert.ok(!path.includes('#'))
  })
})

describe('listOverviews', () => {
  it('lists saved overviews with metadata', () => {
    saveOverview(makeOverview({
      title: 'List Test A',
      overview_id: 'ov-aaa',
      sources: [{ collection: 'c', item_id: 'i', title: 't' }],
    }), tempDir)
    saveOverview(makeOverview({
      title: 'List Test B',
      overview_id: 'ov-bbb',
    }), tempDir)

    const list = listOverviews(tempDir)
    assert.equal(list.length, 2)

    const a = list.find(l => l.overview_id === 'ov-aaa')
    assert.ok(a)
    assert.equal(a!.title, 'List Test A')
    assert.equal(a!.source_count, 1)
  })

  it('returns empty array for nonexistent directory', () => {
    const list = listOverviews(join(tempDir, 'nope'))
    assert.deepEqual(list, [])
  })
})

describe('getOverview', () => {
  it('retrieves a saved overview by ID', () => {
    const overview = makeOverview({ overview_id: 'ov-retrieve-me', title: 'Retrieve' })
    saveOverview(overview, tempDir)

    const loaded = getOverview('ov-retrieve-me', tempDir)
    assert.ok(loaded)
    assert.equal(loaded!.overview_id, 'ov-retrieve-me')
    assert.equal(loaded!.title, 'Retrieve')
  })

  it('returns null for nonexistent ID', () => {
    saveOverview(makeOverview({ overview_id: 'ov-other' }), tempDir)
    const loaded = getOverview('ov-doesnt-exist', tempDir)
    assert.equal(loaded, null)
  })

  it('returns null for nonexistent directory', () => {
    const loaded = getOverview('anything', join(tempDir, 'nope'))
    assert.equal(loaded, null)
  })

  it('round-trips: save then load preserves all fields', () => {
    const overview = makeOverview({
      overview_id: 'ov-roundtrip',
      title: 'Round Trip',
      summary: 'Test summary',
      concepts: [{ name: 'C1', category: 'test', description: 'desc', key_details: ['d1'], source_items: ['i1'], relationships: [] }],
      themes: [{ name: 'T1', description: 'theme', concept_names: ['C1'] }],
      key_findings: ['finding1'],
      knowledge_gaps: ['gap1'],
      open_questions: ['q1'],
    })
    saveOverview(overview, tempDir)

    const loaded = getOverview('ov-roundtrip', tempDir)
    assert.ok(loaded)
    assert.equal(loaded!.summary, 'Test summary')
    assert.equal(loaded!.concepts.length, 1)
    assert.equal(loaded!.concepts[0].name, 'C1')
    assert.equal(loaded!.themes.length, 1)
    assert.equal(loaded!.key_findings.length, 1)
    assert.equal(loaded!.knowledge_gaps.length, 1)
    assert.equal(loaded!.open_questions.length, 1)
  })
})
