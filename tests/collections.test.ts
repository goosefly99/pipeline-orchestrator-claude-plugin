import { describe, it, beforeEach, afterEach } from 'node:test'
import assert from 'node:assert/strict'
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { tmpdir } from 'node:os'
import {
  detectFieldMap,
  loadCollection,
  queryItems,
  getItems,
  getCollection,
  listCollections,
} from '../collections.ts'

let tempDir: string

beforeEach(() => {
  tempDir = mkdtempSync(join(tmpdir(), 'collections-test-'))
})

afterEach(() => {
  rmSync(tempDir, { recursive: true, force: true })
})

function writeJson(name: string, data: unknown): string {
  const p = join(tempDir, name)
  writeFileSync(p, JSON.stringify(data), 'utf-8')
  return p
}

// ── detectFieldMap ────────────────────────────────────────────

describe('detectFieldMap', () => {
  it('detects standard field names (id, content, title, tags)', () => {
    const data = {
      items: [
        { id: '1', content: 'hello', title: 'hi', tags: ['a'] },
      ],
    }
    const fm = detectFieldMap(data)
    assert.equal(fm.items_key, 'items')
    assert.equal(fm.id_field, 'id')
    assert.equal(fm.content_field, 'content')
    assert.equal(fm.title_field, 'title')
    assert.equal(fm.tags_field, 'tags')
  })

  it('detects non-standard field names from candidate lists', () => {
    const data = {
      posts: [
        { tweet_id: 't1', body: 'hello world', headline: 'News', categories: ['finance'], profile: 'alice' },
      ],
    }
    const fm = detectFieldMap(data)
    assert.equal(fm.items_key, 'posts')
    assert.equal(fm.id_field, 'tweet_id')
    assert.equal(fm.content_field, 'body')
    assert.equal(fm.title_field, 'headline')
    assert.equal(fm.tags_field, 'categories')
    assert.equal(fm.author_field, 'profile')
  })

  it('detects _id suffix as fallback for id field', () => {
    const data = {
      items: [
        { article_id: 'a1', text: 'content' },
      ],
    }
    const fm = detectFieldMap(data)
    assert.equal(fm.id_field, 'article_id')
  })

  it('detects date and url fields', () => {
    const data = {
      items: [
        { id: '1', content: 'x', created_at: '2026-01-01', permalink: 'https://example.com' },
      ],
    }
    const fm = detectFieldMap(data)
    assert.equal(fm.date_field, 'created_at')
    assert.equal(fm.url_field, 'permalink')
  })

  it('applies overrides over auto-detected values', () => {
    const data = {
      items: [
        { id: '1', content: 'x', title: 'y', tags: [] },
      ],
    }
    const fm = detectFieldMap(data, { content_field: 'title', tags_field: 'custom_tags' })
    assert.equal(fm.content_field, 'title')
    assert.equal(fm.tags_field, 'custom_tags')
    // Non-overridden fields still auto-detected
    assert.equal(fm.id_field, 'id')
  })

  it('throws when no items array found', () => {
    assert.throws(
      () => detectFieldMap({ metadata: 'nothing here' }),
      /No items array found/,
    )
  })

  it('throws when items array is empty', () => {
    assert.throws(
      () => detectFieldMap({ items: [] }),
      /No items array found/,
    )
  })

  it('defaults to "id" when no id field or _id suffix found', () => {
    const data = {
      items: [
        { name: 'test', content: 'x' },
      ],
    }
    const fm = detectFieldMap(data)
    assert.equal(fm.id_field, 'id')
  })
})

// ── loadCollection ────────────────────────────────────────────

describe('loadCollection', () => {
  it('loads a valid collection from a JSON file', () => {
    const data = {
      items: [
        { id: 'item1', content: 'Research on X', title: 'X Research', tags: ['ml'] },
        { id: 'item2', content: 'Research on Y', title: 'Y Research', tags: ['ml', 'nlp'] },
      ],
    }
    const path = writeJson('research.json', data)
    const col = loadCollection(path, 'test-research')

    assert.equal(col.name, 'test-research')
    assert.equal(col.items.length, 2)
    assert.equal(col.items[0].id, 'item1')
    assert.equal(col.items[0].content, 'Research on X')
    assert.deepEqual(col.available_tags, ['ml', 'nlp'])
  })

  it('defaults name to filename without extension', () => {
    const data = { items: [{ id: '1', content: 'x' }] }
    const path = writeJson('my-data.json', data)
    const col = loadCollection(path)

    assert.equal(col.name, 'my-data')
  })

  it('normalizes author from string', () => {
    const data = {
      items: [
        { id: '1', content: 'x', author: 'Alice' },
      ],
    }
    const path = writeJson('authors-str.json', data)
    const col = loadCollection(path, 'authors-str')
    assert.equal(col.items[0].author?.name, 'Alice')
  })

  it('normalizes author from object with display_name/handle/bio', () => {
    const data = {
      items: [
        { id: '1', content: 'x', author: { display_name: 'Bob', handle: '@bob', bio: 'dev' } },
      ],
    }
    const path = writeJson('authors-obj.json', data)
    const col = loadCollection(path, 'authors-obj')
    assert.equal(col.items[0].author?.name, 'Bob')
    assert.equal(col.items[0].author?.handle, '@bob')
    assert.equal(col.items[0].author?.bio, 'dev')
  })

  it('collects remaining fields as metadata', () => {
    const data = {
      items: [
        { id: '1', content: 'x', title: 't', tags: [], custom_score: 0.95, source: 'api' },
      ],
    }
    const path = writeJson('metadata-test.json', data)
    const col = loadCollection(path, 'metadata-test')
    assert.equal(col.items[0].metadata.custom_score, 0.95)
    assert.equal(col.items[0].metadata.source, 'api')
  })

  it('assigns fallback id (item_0, item_1) when id field is missing', () => {
    const data = {
      items: [
        { content: 'no id here' },
        { content: 'also no id' },
      ],
    }
    const path = writeJson('no-id.json', data)
    const col = loadCollection(path, 'no-id')
    assert.equal(col.items[0].id, 'item_0')
    assert.equal(col.items[1].id, 'item_1')
  })

  it('throws on missing file', () => {
    assert.throws(
      () => loadCollection(join(tempDir, 'nonexistent.json')),
      /ENOENT|no such file/,
    )
  })

  it('throws on invalid JSON', () => {
    const path = join(tempDir, 'bad.json')
    writeFileSync(path, '{ invalid json', 'utf-8')
    assert.throws(
      () => loadCollection(path),
      /JSON/i,
    )
  })

  it('respects field overrides for items_key', () => {
    const data = {
      articles: [
        { id: 'a1', body: 'article content' },
      ],
    }
    const path = writeJson('articles.json', data)
    const col = loadCollection(path, 'articles-override', { items_key: 'articles', content_field: 'body' })
    assert.equal(col.items.length, 1)
    assert.equal(col.items[0].content, 'article content')
  })
})

// ── queryItems ────────────────────────────────────────────────

describe('queryItems', () => {
  it('filters by tags', () => {
    const data = {
      items: [
        { id: '1', content: 'a', tags: ['ml'] },
        { id: '2', content: 'b', tags: ['finance'] },
        { id: '3', content: 'c', tags: ['ml', 'finance'] },
      ],
    }
    const path = writeJson('query-tags.json', data)
    loadCollection(path, 'query-tags')

    const results = queryItems({ collection: 'query-tags', tags: ['finance'] })
    assert.ok(results.length >= 2)
    assert.ok(results.every(r => r.tags.includes('finance')))
  })

  it('full-text search matches content, title, and tags', () => {
    const data = {
      items: [
        { id: '1', content: 'machine learning overview', title: 'ML', tags: [] },
        { id: '2', content: 'financial report', title: 'Q4', tags: [] },
      ],
    }
    const path = writeJson('query-search.json', data)
    loadCollection(path, 'query-search')

    const results = queryItems({ collection: 'query-search', search: 'machine' })
    assert.equal(results.length, 1)
    assert.equal(results[0].id, '1')
  })

  it('respects limit parameter', () => {
    const items = Array.from({ length: 50 }, (_, i) => ({ id: `i${i}`, content: `item ${i}`, tags: ['all'] }))
    const data = { items }
    const path = writeJson('query-limit.json', data)
    loadCollection(path, 'query-limit')

    const results = queryItems({ collection: 'query-limit', tags: ['all'], limit: 5 })
    assert.equal(results.length, 5)
  })

  it('throws on unknown collection', () => {
    assert.throws(
      () => queryItems({ collection: 'nonexistent-collection-xyz' }),
      /not loaded/,
    )
  })
})

// ── getItems ──────────────────────────────────────────────────

describe('getItems', () => {
  it('retrieves items by id', () => {
    const data = {
      items: [
        { id: 'x1', content: 'first' },
        { id: 'x2', content: 'second' },
        { id: 'x3', content: 'third' },
      ],
    }
    const path = writeJson('getitems.json', data)
    loadCollection(path, 'getitems')

    const results = getItems('getitems', ['x1', 'x3'])
    assert.equal(results.length, 2)
    assert.ok(results.some(r => r.id === 'x1'))
    assert.ok(results.some(r => r.id === 'x3'))
  })

  it('throws on unknown collection', () => {
    assert.throws(
      () => getItems('nonexistent-xyz', ['1']),
      /not loaded/,
    )
  })
})

// ── getCollection and listCollections ─────────────────────────

describe('getCollection', () => {
  it('returns a previously loaded collection', () => {
    const data = { items: [{ id: '1', content: 'x' }] }
    const path = writeJson('getcol.json', data)
    loadCollection(path, 'getcol-test')

    const col = getCollection('getcol-test')
    assert.ok(col)
    assert.equal(col!.name, 'getcol-test')
  })

  it('returns undefined for unknown name', () => {
    assert.equal(getCollection('nonexistent-getcol'), undefined)
  })
})

describe('listCollections', () => {
  it('lists all loaded collections with metadata', () => {
    const data = { items: [{ id: '1', content: 'x', tags: ['a'] }] }
    const path = writeJson('listcol.json', data)
    loadCollection(path, 'listcol-test')

    const list = listCollections()
    const entry = list.find(c => c.name === 'listcol-test')
    assert.ok(entry)
    assert.equal(entry!.item_count, 1)
    assert.ok(Array.isArray(entry!.available_tags))
    assert.ok(typeof entry!.field_map === 'object')
  })
})
