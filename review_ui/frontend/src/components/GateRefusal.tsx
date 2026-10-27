import './GateRefusal.css'

interface GateRefusalProps {
  detail: string // the backend's error detail, usually the gate's refusal
}

// The gate's refusal, laid out instead of inlined: headline, one bullet per
// unresolved mapping, then the instruction. Every word is still the gate's
// (mapping.gate.approve) - this component only formats the known shape, and
// anything else renders as plain text so unexpected errors are never hidden.
const REFUSAL = /^cannot approve: (\d+) unresolved mapping\(s\) \((.+)\)\.\s*(.*)$/s

// mapping_label() emits "field 'X'" / "segment 'X'" / "status 'X'", so the
// lookahead split survives commas and parens inside the quoted names.
const LABEL_SPLIT = /,\s(?=(?:field|segment|status)\s')/

export default function GateRefusal({ detail }: GateRefusalProps) {
  const match = REFUSAL.exec(detail)
  if (!match) {
    return (
      <p className="gr gr-plain" role="alert">
        {detail}
      </p>
    )
  }
  const [, count, items, instruction] = match
  return (
    <div className="gr" role="alert">
      <p className="gr-head">
        cannot approve: {count} unresolved mapping{count === '1' ? '' : 's'}
      </p>
      <ul className="gr-list">
        {items.split(LABEL_SPLIT).map((label) => (
          <li key={label}>{label}</li>
        ))}
      </ul>
      <p className="gr-instruction">{instruction}</p>
    </div>
  )
}
