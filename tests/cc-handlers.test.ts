import { describe, it } from 'node:test'
import assert from 'node:assert/strict'
import { handleCCGetItems } from '../cc-handlers.ts'
import type { CCContext } from '../cc-handlers.ts'
import type { ResearchItem } from '../cc-types.ts'

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
