// Types mirroring the backend's pydantic models (specs/model.py,
// discovery/report.py) plus thin fetch helpers. Components never fetch;
// App wires these in so tests can hand components plain data and mocks.

export type MappingStatus = 'proposed' | 'approved' | 'edited' | 'flagged'
export type MappingKind = 'field' | 'segment' | 'consent'

interface ReviewedBase {
  confidence: number
  rationale: string
  status: MappingStatus
  open_question: string | null
}

export interface FieldMapping extends ReviewedBase {
  source_field: string
  source_type: string
  sample_values: string[]
  target_field: string | null
  transform: string
}

export interface SegmentMapping extends ReviewedBase {
  source_segment: string
  source_logic: string
  target_query: string | null
}

export interface ConsentMapping extends ReviewedBase {
  source_status: string
  target_status: string | null
}

// How the approver authenticated (RC1-554). verified only for IdP sign-in.
export interface ApproverIdentity {
  method: 'oidc' | 'password'
  verified: boolean
  issuer: string | null
  subject: string | null
  email: string | null
  name: string | null
}

export interface Approval {
  approved_by: string
  approved_at: string
  content_sha256: string
  identity?: ApproverIdentity | null
}

export interface Spec {
  source: string
  target: string
  status: string
  generated_at: string
  field_mappings: FieldMapping[]
  segment_mappings: SegmentMapping[]
  consent_mappings: ConsentMapping[]
  merge_tag_mappings: { source_tag: string; target_tag: string }[]
  approval: Approval | null
}

export interface SpecResponse {
  path: string
  spec: Spec
}

export interface ApproveResult {
  out: string
  approved_by: string
  approved_at: string
  content_sha256: string
  identity?: ApproverIdentity | null
}

export interface AuditRecord {
  seq: number
  at: string
  event: string
  actor: {
    method: string
    verified: boolean
    email?: string
    name?: string
    issuer?: string
  }
  detail: Record<string, unknown>
  sha256: string
}

export interface Report {
  source: string
  target: string
  as_of: string
  inventory: {
    members_total: number
    suppression_count: number
    segments: { name: string }[]
    templates: string[]
  }
  profile: {
    consent: Record<string, number>
    domain_class: Record<string, number>
    top_domains: Record<string, number>
    states: Record<string, number>
    tenure_cohorts: Record<string, number>
    member_rating: Record<string, number>
  }
  risks: { code: string; severity: string; title: string; detail: string; count: number | null }[]
}

// One normalized shape per reviewed mapping so cards and forms don't branch
// on the three pydantic subclasses.
export interface MappingView {
  kind: MappingKind
  index: number
  source: string
  context: string // source_type / source_logic / '' - the second line of the card
  samples: string[]
  confidence: number
  rationale: string
  status: MappingStatus
  openQuestion: string | null
  target: string | null
  transform: string | null // field mappings only
}

export function mappingViews(spec: Spec): MappingView[] {
  const fields: MappingView[] = spec.field_mappings.map((m, index) => ({
    kind: 'field',
    index,
    source: m.source_field,
    context: m.source_type,
    samples: m.sample_values,
    confidence: m.confidence,
    rationale: m.rationale,
    status: m.status,
    openQuestion: m.open_question,
    target: m.target_field,
    transform: m.transform,
  }))
  const segments: MappingView[] = spec.segment_mappings.map((m, index) => ({
    kind: 'segment',
    index,
    source: m.source_segment,
    context: m.source_logic,
    samples: [],
    confidence: m.confidence,
    rationale: m.rationale,
    status: m.status,
    openQuestion: m.open_question,
    target: m.target_query,
    transform: null,
  }))
  const consent: MappingView[] = spec.consent_mappings.map((m, index) => ({
    kind: 'consent',
    index,
    source: m.source_status,
    context: '',
    samples: [],
    confidence: m.confidence,
    rationale: m.rationale,
    status: m.status,
    openQuestion: m.open_question,
    target: m.target_status,
    transform: null,
  }))
  return [...fields, ...segments, ...consent]
}

export interface Resolution {
  status: 'approved' | 'edited'
  target?: string
  transform?: string
}

export class ApiError extends Error {
  constructor(
    public status: number,
    detail: string,
  ) {
    super(detail)
  }
}

async function unwrap<T>(resp: Response): Promise<T> {
  if (!resp.ok) {
    let detail = `${resp.status} ${resp.statusText}`
    try {
      const body = await resp.json()
      if (typeof body.detail === 'string') detail = body.detail
      else if (body.detail) detail = JSON.stringify(body.detail)
    } catch {
      // non-JSON error body; keep the status line
    }
    throw new ApiError(resp.status, detail)
  }
  return resp.json() as Promise<T>
}

export interface Me {
  authenticated: boolean
  auth_enabled: boolean
  password_enabled?: boolean
  sso_provider?: string | null
  identity?: ApproverIdentity | null
}

// Full-page navigation, not fetch: the IdP's login page is the customer's.
export const SSO_START_URL = '/api/auth/sso/start'

export function logout(): Promise<{ ok: boolean }> {
  return fetch('/api/logout', { method: 'POST' }).then((r) => unwrap<{ ok: boolean }>(r))
}

export function getAudit(): Promise<{ records: AuditRecord[] }> {
  return fetch('/api/audit?limit=50').then((r) => unwrap<{ records: AuditRecord[] }>(r))
}

export function getMe(): Promise<Me> {
  return fetch('/api/me').then((r) => unwrap<Me>(r))
}

export function login(password: string): Promise<{ ok: boolean }> {
  return fetch('/api/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ password }),
  }).then((r) => unwrap<{ ok: boolean }>(r))
}

export function getSpec(): Promise<SpecResponse> {
  return fetch('/api/spec').then((r) => unwrap<SpecResponse>(r))
}

export function getReport(): Promise<Report> {
  return fetch('/api/report').then((r) => unwrap<Report>(r))
}

export function resolveMapping(view: MappingView, resolution: Resolution): Promise<SpecResponse> {
  return fetch(`/api/mappings/${view.kind}/${view.index}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ source: view.source, ...resolution }),
  }).then((r) => unwrap<SpecResponse>(r))
}

// approvedBy is omitted under a verified SSO identity: the IdP's name is the name.
export function approveSpec(approvedBy?: string): Promise<ApproveResult> {
  return fetch('/api/approve', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(approvedBy ? { approved_by: approvedBy } : {}),
  }).then((r) => unwrap<ApproveResult>(r))
}
