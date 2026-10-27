import type { MappingView } from '../api'

export function view(overrides: Partial<MappingView> = {}): MappingView {
  return {
    kind: 'field',
    index: 0,
    source: 'ZIP',
    context: 'text',
    samples: ['ask reid', '00597'],
    confidence: 0.3,
    rationale: 'Formats are mixed.',
    status: 'flagged',
    openQuestion: 'How should the truncated ZIPs be handled?',
    target: null,
    transform: 'Pending reviewer decision.',
    ...overrides,
  }
}
