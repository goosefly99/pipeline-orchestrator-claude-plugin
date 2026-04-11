// tests/bm25.test.ts — Unit tests for BM25Index

import { describe, it, beforeEach, afterEach } from 'node:test'
import assert from 'node:assert/strict'
import { mkdtempSync, rmSync, existsSync } from 'node:fs'
import { join } from 'node:path'
import { tmpdir } from 'node:os'
import type { ResearchItem } from '../cc-types.ts'
import { BM25Index, bm25IndexDir } from '../bm25.ts'

function makeItem(id: string, title: string, content: string, tags: string[] = []): ResearchItem {
  return { id, title, content, tags, metadata: {} }
}

const sampleItems: ResearchItem[] = [
  makeItem('item-1', 'Bitcoin Trading Strategies', 'Analysis of momentum and mean-reversion strategies for BTC spot markets.', ['crypto', 'trading']),
  makeItem('item-2', 'Ethereum DeFi Yield Farming', 'Yield farming across Uniswap, Aave, and Compound protocols.', ['crypto', 'defi']),
  makeItem('item-3', 'Prediction Market Design', 'How prediction markets aggregate information through binary contracts.', ['markets', 'design']),
  makeItem('item-4', 'Election Forecasting Models', 'Bayesian models for election outcome prediction using polling data.', ['elections', 'models']),
  makeItem('item-5', 'Options Pricing Theory', 'Black-Scholes and binomial tree models for option valuation.', ['finance', 'options']),
]

describe('BM25Index', () => {
  let tempDir: string

  beforeEach(() => {
    tempDir = mkdtempSync(join(tmpdir(), 'bm25-test-'))
  })

  afterEach(() => {
    rmSync(tempDir, { recursive: true, force: true })
  })

  describe('build', () => {
    it('builds index and returns correct metadata', () => {
      const idx = new BM25Index()
      const meta = idx.build(sampleItems, 'test-collection')

      assert.equal(meta.collection_name, 'test-collection')
      assert.equal(meta.item_count, 5)
      assert.ok(meta.avg_doc_length > 0)
      assert.ok(meta.created_at)
      assert.deepEqual(meta.item_ids, ['item-1', 'item-2', 'item-3', 'item-4', 'item-5'])
    })

    it('handles empty items gracefully', () => {
      const idx = new BM25Index()
      const meta = idx.build([], 'empty-col')
      assert.equal(meta.item_count, 0)
      assert.equal(meta.avg_doc_length, 0)
      assert.deepEqual(meta.item_ids, [])
    })
  })

  describe('search — exact term match', () => {
    it('ranks documents with matching terms highest', () => {
      const idx = new BM25Index()
      idx.build(sampleItems, 'search-test')

      const hits = idx.search('bitcoin trading strategies')
      assert.ok(hits.length > 0, 'should return at least one hit')
      assert.equal(hits[0].item_id, 'item-1')
      assert.ok(hits[0].score > 0)
    })
  })

  describe('search — partial overlap', () => {
    it('ranks docs with more matching terms higher', () => {
      const idx = new BM25Index()
      idx.build(sampleItems, 'overlap-test')

      const hits = idx.search('election prediction models bayesian')
      assert.ok(hits.length > 0)
      assert.equal(hits[0].item_id, 'item-4')
    })
  })

  describe('search — topK', () => {
    it('limits results to topK', () => {
      const idx = new BM25Index()
      idx.build(sampleItems, 'topk-test')

      const hits = idx.search('market', 2)
      assert.ok(hits.length <= 2, 'should return at most 2 results')
    })
  })

  describe('search — empty query', () => {
    it('returns empty array for empty query', () => {
      const idx = new BM25Index()
      idx.build(sampleItems, 'empty-query')

      const hits = idx.search('')
      assert.deepEqual(hits, [])
    })

    it('returns empty array for query with only short tokens', () => {
      const idx = new BM25Index()
      idx.build(sampleItems, 'short-tokens')

      const hits = idx.search('a I')
      assert.deepEqual(hits, [])
    })
  })

  describe('search — tag filter', () => {
    it('only returns docs matching tag filter', () => {
      const idx = new BM25Index()
      idx.build(sampleItems, 'filter-test')

      const hits = idx.search('market', 10, { tags: ['crypto'] })
      for (const hit of hits) {
        const tags = hit.metadata.tags.toLowerCase().split(',').map(t => t.trim())
        assert.ok(tags.includes('crypto'), `Expected crypto tag in ${hit.metadata.tags}`)
      }
    })

    it('returns empty when no docs match tag filter', () => {
      const idx = new BM25Index()
      idx.build(sampleItems, 'no-match-filter')

      const hits = idx.search('bitcoin', 10, { tags: ['nonexistent'] })
      assert.deepEqual(hits, [])
    })
  })

  describe('persist + loadIndex round-trip', () => {
    it('persists to disk and loads back with same state', () => {
      const idx = new BM25Index()
      idx.build(sampleItems, 'persist-test')
      const indexDir = join(tempDir, 'test-index')
      idx.persist(indexDir)

      assert.ok(existsSync(join(indexDir, 'index.json')))

      const idx2 = new BM25Index()
      idx2.loadIndex(indexDir)

      const meta = idx2.getMetadata()
      assert.ok(meta)
      assert.equal(meta!.collection_name, 'persist-test')
      assert.equal(meta!.item_count, 5)

      // Search on loaded index should work identically
      const hits = idx2.search('bitcoin trading')
      assert.ok(hits.length > 0)
      assert.equal(hits[0].item_id, 'item-1')
    })
  })

  describe('isBuilt', () => {
    it('returns false before build', () => {
      const idx = new BM25Index()
      assert.equal(idx.isBuilt(), false)
    })

    it('returns true after build (in-memory)', () => {
      const idx = new BM25Index()
      idx.build(sampleItems, 'built-test')
      assert.equal(idx.isBuilt(), true)
    })

    it('returns true when index.json exists on disk', () => {
      const idx = new BM25Index()
      idx.build(sampleItems, 'disk-test')
      const indexDir = join(tempDir, 'disk-index')
      idx.persist(indexDir)

      const idx2 = new BM25Index()
      assert.equal(idx2.isBuilt(indexDir), true)
    })

    it('returns false for non-existent directory', () => {
      const idx = new BM25Index()
      assert.equal(idx.isBuilt(join(tempDir, 'no-such-dir')), false)
    })
  })

  describe('getMetadata', () => {
    it('returns null before build', () => {
      const idx = new BM25Index()
      assert.equal(idx.getMetadata(), null)
    })

    it('returns metadata after build', () => {
      const idx = new BM25Index()
      idx.build(sampleItems, 'meta-test')
      const meta = idx.getMetadata()
      assert.ok(meta)
      assert.equal(meta!.item_count, 5)
    })
  })
})

describe('bm25IndexDir', () => {
  it('returns path under baseDir/kb/vectors/<collection>', () => {
    const result = bm25IndexDir('/data/pipeline', 'my-collection')
    assert.ok(result.includes('kb'))
    assert.ok(result.includes('vectors'))
    assert.ok(result.includes('my-collection'))
  })

  it('handles collection names with special characters', () => {
    const result = bm25IndexDir('/data', 'col-with-dashes_and_underscores')
    assert.ok(result.includes('col-with-dashes_and_underscores'))
  })
})
