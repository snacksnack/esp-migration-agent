import { ApiError, approveSpec, mappingViews, type Spec } from '../api'

const SPEC: Spec = {
  source: 'mailchimp',
  target: 'listmonk',
  status: 'draft',
  generated_at: '2026-10-26T18:03:09Z',
  merge_tag_mappings: [],
  field_mappings: [
    {
      confidence: 0.3,
      rationale: 'Mixed formats.',
      status: 'flagged',
      open_question: 'Pad the ZIPs?',
      source_field: 'ZIP',
      source_type: 'text',
      sample_values: ['1576'],
      target_field: null,
      transform: 'Pending reviewer decision.',
    },
  ],
  segment_mappings: [
    {
      confidence: 0.4,
      rationale: 'Depends on ZIP.',
      status: 'flagged',
      open_question: 'Confirm the query.',
      source_segment: 'Brooklyn subscribers',
      source_logic: "ZIP starts with '112'.",
      target_query: null,
    },
  ],
  consent_mappings: [
    {
      confidence: 0.85,
      rationale: 'Opted in.',
      status: 'proposed',
      open_question: null,
      source_status: 'subscribed',
      target_status: 'enabled, confirmed',
    },
  ],
  approval: null,
}

describe('mappingViews', () => {
  it('normalizes the three mapping kinds into one card shape', () => {
    const views = mappingViews(SPEC)
    expect(views.map((v) => [v.kind, v.source, v.target])).toEqual([
      ['field', 'ZIP', null],
      ['segment', 'Brooklyn subscribers', null],
      ['consent', 'subscribed', 'enabled, confirmed'],
    ])
    expect(views[0].transform).toBe('Pending reviewer decision.')
    expect(views[1].transform).toBeNull()
    expect(views[1].context).toBe("ZIP starts with '112'.")
  })
})

describe('approveSpec', () => {
  it('throws an ApiError carrying the 409 detail from the gate', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ detail: 'cannot approve: 2 unresolved mapping(s)' }), {
          status: 409,
        }),
      ),
    )
    const error = await approveSpec('Reid Collins').catch((e: unknown) => e)
    expect(error).toBeInstanceOf(ApiError)
    expect((error as ApiError).status).toBe(409)
    expect((error as ApiError).message).toBe('cannot approve: 2 unresolved mapping(s)')
    vi.unstubAllGlobals()
  })
})
