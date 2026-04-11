// ── Field mapping for auto-detecting JSON schema ────────────────

export interface FieldMap {
  items_key: string
  id_field: string
  content_field: string
  title_field: string
  tags_field: string
  author_field?: string
  date_field?: string
  url_field?: string
}

// ── Normalized research item ────────────────────────────────────

export interface ResearchItem {
  id: string
  title: string
  content: string
  url?: string
  date?: string
  author?: { name: string; handle?: string; bio?: string }
  tags: string[]
  metadata: Record<string, unknown>
}

export interface ResearchCollection {
  name: string
  file_path: string
  items: ResearchItem[]
  field_map: FieldMap
  available_tags: string[]
}

// ── Knowledge base concepts ─────────────────────────────────────

export interface Concept {
  name: string
  category: string
  description: string
  key_details: string[]
  source_items: string[]
  relationships: string[]
}

export interface Theme {
  name: string
  description: string
  concept_names: string[]
}

// ── Overview output ─────────────────────────────────────────────

export type OverviewStatus = 'draft' | 'complete'

export interface KnowledgeOverview {
  overview_id: string
  title: string
  created_date: string
  status: OverviewStatus
  sources: OverviewSource[]
  summary: string
  concepts: Concept[]
  themes: Theme[]
  key_findings: string[]
  knowledge_gaps: string[]
  open_questions: string[]
}

export interface OverviewSource {
  collection: string
  item_id: string
  title: string
}
