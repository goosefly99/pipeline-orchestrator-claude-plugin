// bm25.ts — Pure-TypeScript BM25 (Okapi BM25) ranked text search

import { writeFileSync, readFileSync, existsSync, mkdirSync } from 'node:fs'
import { join, resolve } from 'node:path'
import type { ResearchItem } from './cc-types.ts'

// ── Types ────────────────────────────────────────────────────

export interface SearchHit {
  item_id: string
  score: number
  metadata: { title: string; tags: string }
}

export interface BM25IndexMetadata {
  collection_name: string
  item_count: number
  avg_doc_length: number
  created_at: string
  item_ids: string[]
}

interface DocEntry {
  item_id: string
  title: string
  tags: string
  length: number
  terms: Record<string, number>
}

interface PersistedIndex extends BM25IndexMetadata {
  docs: DocEntry[]
  idf: Record<string, number>
}

// ── Tokenizer ────────────────────────────────────────────────

function tokenize(text: string): string[] {
  return text
    .toLowerCase()
    .split(/[^a-z0-9]+/)
    .filter(t => t.length >= 2)
}

function termFrequencies(tokens: string[]): Record<string, number> {
  const tf: Record<string, number> = {}
  for (const t of tokens) {
    tf[t] = (tf[t] ?? 0) + 1
  }
  return tf
}

// ── BM25Index ────────────────────────────────────────────────

const K1 = 1.5
const B = 0.75

export class BM25Index {
  private docs: DocEntry[] = []
  private idf: Record<string, number> = {}
  private avgdl = 0
  private metadata: BM25IndexMetadata | null = null
  private indexDir: string | null = null

  build(items: ResearchItem[], collectionName: string): BM25IndexMetadata {
    const N = items.length

    this.docs = items.map(item => {
      const text = `${item.title || ''} ${item.tags.join(' ')} ${item.content}`
      const tokens = tokenize(text)
      return {
        item_id: item.id,
        title: item.title || '',
        tags: item.tags.join(','),
        length: tokens.length,
        terms: termFrequencies(tokens),
      }
    })

    const totalLength = this.docs.reduce((sum, d) => sum + d.length, 0)
    this.avgdl = N > 0 ? totalLength / N : 0

    const df: Record<string, number> = {}
    for (const doc of this.docs) {
      for (const term of Object.keys(doc.terms)) {
        df[term] = (df[term] ?? 0) + 1
      }
    }
    this.idf = {}
    for (const [term, docFreq] of Object.entries(df)) {
      this.idf[term] = Math.log((N - docFreq + 0.5) / (docFreq + 0.5) + 1)
    }

    this.metadata = {
      collection_name: collectionName,
      item_count: N,
      avg_doc_length: this.avgdl,
      created_at: new Date().toISOString(),
      item_ids: items.map(item => item.id),
    }

    return this.metadata
  }

  persist(indexDir: string): void {
    if (!this.metadata) throw new Error('Cannot persist: index not built')
    if (!existsSync(indexDir)) mkdirSync(indexDir, { recursive: true })
    this.indexDir = indexDir

    const data: PersistedIndex = {
      ...this.metadata,
      docs: this.docs,
      idf: this.idf,
    }
    writeFileSync(join(indexDir, 'index.json'), JSON.stringify(data, null, 2), 'utf-8')
  }

  loadIndex(indexDir: string): void {
    this.indexDir = indexDir
    const data = JSON.parse(readFileSync(join(indexDir, 'index.json'), 'utf-8')) as PersistedIndex
    this.docs = data.docs
    this.idf = data.idf
    this.avgdl = data.avg_doc_length
    this.metadata = {
      collection_name: data.collection_name,
      item_count: data.item_count,
      avg_doc_length: data.avg_doc_length,
      created_at: data.created_at,
      item_ids: data.item_ids,
    }
  }

  isBuilt(indexDir?: string): boolean {
    const dir = indexDir ?? this.indexDir
    if (!dir) return this.metadata !== null
    return existsSync(join(dir, 'index.json'))
  }

  getMetadata(): BM25IndexMetadata | null {
    return this.metadata
  }

  search(query: string, topK = 10, filter?: { tags?: string[] }): SearchHit[] {
    const queryTerms = tokenize(query)
    if (queryTerms.length === 0) return []

    let candidates = this.docs
    if (filter?.tags && filter.tags.length > 0) {
      const filterTags = new Set(filter.tags.map(t => t.toLowerCase()))
      candidates = candidates.filter(doc => {
        const docTags = doc.tags.toLowerCase().split(',').map(t => t.trim())
        return docTags.some(t => filterTags.has(t))
      })
    }

    const scored: SearchHit[] = candidates.map(doc => {
      let score = 0
      for (const term of queryTerms) {
        const tf = doc.terms[term] ?? 0
        if (tf === 0) continue
        const idf = this.idf[term] ?? 0
        const numerator = tf * (K1 + 1)
        const denominator = tf + K1 * (1 - B + B * doc.length / (this.avgdl || 1))
        score += idf * (numerator / denominator)
      }
      return {
        item_id: doc.item_id,
        score,
        metadata: { title: doc.title, tags: doc.tags },
      }
    })

    return scored
      .filter(h => h.score > 0)
      .sort((a, b) => b.score - a.score)
      .slice(0, topK)
  }
}

// ── Index path resolver ──────────────────────────────────────

export function bm25IndexDir(baseDir: string, collectionName: string): string {
  return resolve(join(baseDir, 'kb', 'vectors', collectionName))
}
