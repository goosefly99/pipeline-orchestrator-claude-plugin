import { readFileSync, readdirSync } from 'node:fs'
import { join } from 'node:path'
import Ajv2020Import, { type ValidateFunction } from 'ajv/dist/2020.js'
import addFormatsImport from 'ajv-formats'

// ajv and ajv-formats ship CJS with dual typings that don't match the runtime
// default-export shape under NodeNext moduleResolution. Cast to usable forms.
const Ajv2020 = Ajv2020Import as unknown as new (opts?: {
  allErrors?: boolean
  strict?: boolean | 'log'
}) => { compile: (schema: unknown) => ValidateFunction }
const addFormats = addFormatsImport as unknown as (ajv: unknown) => void

export interface ValidationResult {
  valid: boolean
  errors: string[]
}

export type SchemaMap = Record<string, object>

// Module-level cached Ajv instance and compiled validators
const ajv = new Ajv2020({ allErrors: true, strict: false })
addFormats(ajv)
const validatorCache = new Map<string, ValidateFunction>()

export function loadSchemas(schemasDir: string): SchemaMap {
  const schemas: SchemaMap = {}
  const files = readdirSync(schemasDir).filter(f => f.endsWith('.json'))

  for (const file of files) {
    const content = readFileSync(join(schemasDir, file), 'utf-8')
    schemas[file] = JSON.parse(content) as object
  }

  return schemas
}

export function validateArtifact(
  schemas: SchemaMap,
  schemaFile: string,
  artifact: unknown,
): ValidationResult {
  const schema = schemas[schemaFile]
  if (!schema) {
    throw new Error(`Schema "${schemaFile}" not found. Available: ${Object.keys(schemas).join(', ')}`)
  }

  const cached = validatorCache.get(schemaFile)
  let validate: ValidateFunction
  if (cached) {
    validate = cached
  } else {
    validate = ajv.compile(schema)
    validatorCache.set(schemaFile, validate)
  }

  const valid = validate(artifact) as boolean

  const errors = valid
    ? []
    : (validate.errors ?? []).map(e => {
        const path = e.instancePath || '/'
        return `${path}: ${e.message ?? 'unknown error'}${e.params ? ` (${JSON.stringify(e.params)})` : ''}`
      })

  return { valid, errors }
}
