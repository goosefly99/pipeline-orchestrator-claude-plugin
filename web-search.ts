// web-search.ts — Web search and content fetching for pipeline_web_search tool

import { readFileSync, writeFileSync } from 'node:fs'
import { randomBytes } from 'node:crypto'
import { stripHtmlTags } from './ingest.ts'
import type { RawItem, SourceRun, RawCollection } from './ingest.ts'

// ── Types ────────────────────────────────────────────────────

export interface SearchResult {
  title: string
  url: string
  snippet: string
}

export interface FetchResult {
  url: string
  title: string
  content: string
  status: number
  contentLength: number
  error?: string
}

// ── Search ───────────────────────────────────────────────────

const FETCH_TIMEOUT_MS = 10_000

/**
 * Execute web searches via a configurable search provider.
 *
 * Provider is selected by the PIPELINE_SEARCH_API env var:
 *   - "brave"  — Brave Search API (requires BRAVE_API_KEY)
 *   - default  — DuckDuckGo HTML lite (no key required)
 */
export async function webSearch(
  queries: string[],
  maxResultsPerQuery: number,
): Promise<SearchResult[]> {
  const provider = process.env.PIPELINE_SEARCH_API ?? 'duckduckgo'
  const results: SearchResult[] = []

  for (const query of queries) {
    let batch: SearchResult[]
    switch (provider) {
      case 'brave':
        batch = await searchBrave(query, maxResultsPerQuery)
        break
      default:
        batch = await searchDuckDuckGo(query, maxResultsPerQuery)
    }
    results.push(...batch)
  }

  return results
}

async function searchBrave(query: string, max: number): Promise<SearchResult[]> {
  const apiKey = process.env.BRAVE_API_KEY
  if (!apiKey) throw new Error('BRAVE_API_KEY env var required when PIPELINE_SEARCH_API=brave')

  const params = new URLSearchParams({ q: query, count: String(max) })
  const resp = await fetch(`https://api.search.brave.com/res/v1/web/search?${params}`, {
    headers: { 'X-Subscription-Token': apiKey, Accept: 'application/json' },
    signal: AbortSignal.timeout(FETCH_TIMEOUT_MS),
  })

  if (!resp.ok) throw new Error(`Brave search failed: ${resp.status} ${resp.statusText}`)

  const body = (await resp.json()) as {
    web?: { results?: Array<{ title: string; url: string; description: string }> }
  }

  return (body.web?.results ?? []).slice(0, max).map((r) => ({
    title: r.title,
    url: r.url,
    snippet: r.description,
  }))
}

async function searchDuckDuckGo(query: string, max: number): Promise<SearchResult[]> {
  const params = new URLSearchParams({ q: query, format: 'json', no_redirect: '1' })
  const resp = await fetch(`https://api.duckduckgo.com/?${params}`, {
    signal: AbortSignal.timeout(FETCH_TIMEOUT_MS),
  })

  if (!resp.ok) throw new Error(`DuckDuckGo search failed: ${resp.status} ${resp.statusText}`)

  const body = (await resp.json()) as {
    RelatedTopics?: Array<{
      Text?: string
      FirstURL?: string
      Result?: string
    }>
  }

  return (body.RelatedTopics ?? [])
    .filter((t) => t.FirstURL && t.Text)
    .slice(0, max)
    .map((t) => ({
      title: t.Text!.slice(0, 120),
      url: t.FirstURL!,
      snippet: t.Text!,
    }))
}

// ── Fetch and extract ────────────────────────────────────────

/**
 * Extract the <title> tag content from HTML before stripping all tags.
 */
function extractHtmlTitle(html: string): string {
  const match = html.match(/<title[^>]*>([\s\S]*?)<\/title>/i)
  return match ? match[1].replace(/\s+/g, ' ').trim() : ''
}

/**
 * Fetch URLs and extract text content from HTML.
 * Individual failures are recorded but do not abort the batch.
 */
export async function fetchAndExtract(urls: string[]): Promise<FetchResult[]> {
  const results: FetchResult[] = []

  for (const url of urls) {
    try {
      const resp = await fetch(url, {
        signal: AbortSignal.timeout(FETCH_TIMEOUT_MS),
        headers: { 'User-Agent': 'PipelineMCP/1.0' },
      })

      const html = await resp.text()
      const title = extractHtmlTitle(html)
      const content = stripHtmlTags(html)

      results.push({
        url,
        title: title || url,
        content,
        status: resp.status,
        contentLength: content.length,
      })
    } catch (err) {
      results.push({
        url,
        title: url,
        content: '',
        status: 0,
        contentLength: 0,
        error: err instanceof Error ? err.message : String(err),
      })
    }
  }

  return results
}

// ── Merge into collection ────────────────────────────────────

function shortHex(): string {
  return randomBytes(4).toString('hex')
}

/**
 * Build RawItem entries from fetched web content.
 */
function buildWebItems(
  fetched: FetchResult[],
  searchQuery: string | undefined,
  searchSnippets: Map<string, string>,
): { items: RawItem[]; sourceRuns: SourceRun[] } {
  const now = new Date().toISOString()
  const items: RawItem[] = []
  const sourceRuns: SourceRun[] = []

  for (const f of fetched) {
    if (f.error) continue

    const item: RawItem = {
      id: `web_${shortHex()}`,
      source_type: 'web',
      source_ref: `url:${f.url}`,
      title: f.title,
      content: f.content,
      url: f.url,
      date: now,
      tags: [],
      metadata: {
        search_query: searchQuery ?? '',
        search_snippet: searchSnippets.get(f.url) ?? '',
        fetch_status: f.status,
        content_length: f.contentLength,
      },
    }
    items.push(item)

    sourceRuns.push({
      source_type: 'web',
      query: searchQuery,
      url: f.url,
      executed_at: now,
      items_found: 1,
      items_stored: 1,
    })
  }

  return { items, sourceRuns }
}

/**
 * Read an existing raw-collection JSON file, append new items and source_runs,
 * and write it back to disk.
 */
export function mergeIntoCollection(
  collectionPath: string,
  newItems: RawItem[],
  newSourceRuns: SourceRun[],
): void {
  const raw = readFileSync(collectionPath, 'utf-8')
  const collection = JSON.parse(raw) as RawCollection

  collection.items.push(...newItems)
  collection.source_runs.push(...newSourceRuns)

  writeFileSync(collectionPath, JSON.stringify(collection, null, 2))
}

// ── Public orchestrator ──────────────────────────────────────

export interface WebSearchParams {
  queries?: string[]
  collection_path: string
  max_results_per_query?: number
  fetch_urls?: string[]
}

export interface WebSearchResponse {
  search_results?: SearchResult[]
  result_count?: number
  items_added?: number
  items_failed?: number
  errors?: string[]
  collection_path?: string
  fetched_urls?: string[]
}

/**
 * Orchestrate the two-mode web search tool.
 * Returns the response data and a next_step hint for the agent.
 */
export async function executeWebSearch(
  params: WebSearchParams,
): Promise<{ data: WebSearchResponse; next_step: string }> {
  const { queries, collection_path, fetch_urls } = params
  const maxResults = params.max_results_per_query ?? 5

  let searchResults: SearchResult[] | undefined
  const snippetMap = new Map<string, string>()

  // Search mode
  if (queries && queries.length > 0) {
    searchResults = await webSearch(queries, maxResults)
    for (const r of searchResults) {
      snippetMap.set(r.url, r.snippet)
    }
  }

  // Fetch mode
  if (fetch_urls && fetch_urls.length > 0) {
    const fetched = await fetchAndExtract(fetch_urls)
    const searchQuery = queries?.[0]
    const { items, sourceRuns } = buildWebItems(fetched, searchQuery, snippetMap)

    const failed = fetched.filter((f) => f.error)

    mergeIntoCollection(collection_path, items, sourceRuns)

    return {
      data: {
        items_added: items.length,
        items_failed: failed.length,
        errors: failed.map((f) => `${f.url}: ${f.error}`),
        collection_path,
        fetched_urls: fetch_urls,
        // Include search results if both modes were used in one call
        ...(searchResults ? { search_results: searchResults, result_count: searchResults.length } : {}),
      },
      next_step: 'Call pipeline_register_artifact to register the updated collection.',
    }
  }

  // Search-only mode
  if (searchResults) {
    return {
      data: {
        search_results: searchResults,
        result_count: searchResults.length,
      },
      next_step: 'Review results and call again with fetch_urls to ingest selected pages.',
    }
  }

  throw new Error('Either queries or fetch_urls (or both) must be provided.')
}
