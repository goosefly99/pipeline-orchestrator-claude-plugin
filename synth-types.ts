// synth-types.ts — Design spec types for embedded synth tools

export interface SpecSource {
  collection: string
  item_id: string
  title: string
  relevance: string
}

export interface SpecComponent {
  name: string
  purpose: string
  inputs: string[]
  outputs: string[]
  dependencies: string[]
}

export interface SpecPhase {
  phase: number
  name: string
  tasks: string[]
  deliverables: string[]
}

export interface SpecRisk {
  description: string
  severity: 'low' | 'medium' | 'high'
  mitigation: string
}

export type SpecType = 'implementation' | 'architecture' | 'research' | 'comparison'
export type SpecStatus = 'draft' | 'review' | 'approved' | 'implemented'

export interface DesignSpec {
  spec_id: string
  title: string
  created_date: string
  updated_date: string
  version: string
  status: SpecStatus
  domain?: string
  spec_type: SpecType

  sources: SpecSource[]

  overview: {
    description: string
    objectives: string[]
    constraints: string[]
    assumptions: string[]
  }

  architecture: {
    components: SpecComponent[]
    data_flow: string
    integration_points: string[]
  }

  implementation: {
    phases: SpecPhase[]
    tech_stack: string[]
    complexity: 'low' | 'medium' | 'high'
  }

  risks: SpecRisk[]
  success_criteria: string[]
  notes: string
}
