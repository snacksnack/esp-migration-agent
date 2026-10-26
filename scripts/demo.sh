#!/usr/bin/env bash
# RC1-547: the scripted demo arc, end to end.
#
#   export -> discovery -> proposal + open questions -> human review ->
#   approve -> dry-run diff -> execute -> reconciliation -> cutover plan
#
# Usage:
#   bash scripts/demo.sh                 # listmonk target, fast sample (~2 min)
#   DEMO_FAST=0 bash scripts/demo.sh     # full 4,255-subscriber fixture
#   ESP_DEMO_TARGET=brevo bash scripts/demo.sh   # third-party target
#
# Target setup: docs/demo.md. The demo never sends mail on any target.
set -euo pipefail
cd "$(dirname "$0")/.."

TARGET="${ESP_DEMO_TARGET:-listmonk}"
PAUSE="${DEMO_PAUSE:-2}"
FAST="${DEMO_FAST:-1}"
WORK=/tmp/esp-demo
REVIEWER="${DEMO_REVIEWER:-Reid Collins}"

step() {
  echo
  echo "=============================================================="
  echo "  $1"
  echo "=============================================================="
  sleep "$PAUSE"
}

rm -rf "$WORK" && mkdir -p "$WORK"

AUDIENCE=fixtures/mailchimp_export/audience
if [ "$FAST" = "1" ]; then
  # Deterministic sample: header + first 200 rows of each consent bucket,
  # so the full arc fits a two-minute clip. DEMO_FAST=0 migrates everything.
  AUDIENCE="$WORK/audience_sample"
  mkdir -p "$AUDIENCE"
  for f in fixtures/mailchimp_export/audience/*.csv; do
    head -201 "$f" > "$AUDIENCE/$(basename "$f")"
  done
fi

step "1/7  The customer's export bundle (synthetic fixture: real Mailchimp format)"
ls -la "$AUDIENCE" fixtures/api_supplement

step "2/7  Discovery: inventory, audience profile, risk register, proposal (deterministic)"
uv run python -m discovery.report --audience-dir "$AUDIENCE" --out "$WORK/discovery.md"
sed -n '/## Risk register/,/## Scope/p' "$WORK/discovery.md" | head -24

step "3/7  Mapping proposal: the LLM's draft - every ambiguity flagged, never guessed"
uv run python -m mapping.review status specs/drafts/mailchimp_to_listmonk.yaml

step "4/7  Human review: the reviewer resolves each open question in the YAML"
git diff --no-index specs/drafts/mailchimp_to_listmonk.yaml \
  specs/drafts/mailchimp_to_listmonk.resolved.yaml | head -40 || true
uv run python -m mapping.review approve specs/drafts/mailchimp_to_listmonk.resolved.yaml \
  --by "$REVIEWER" --out "$WORK/approved.yaml"

step "5/7  Dry-run: the full phase-ordered diff, before any write (target: $TARGET)"
uv run python -m executor.run --spec "$WORK/approved.yaml" --audience-dir "$AUDIENCE" \
  --target "$TARGET"

step "6/7  Execute: suppressions first, idempotent upserts (target: $TARGET)"
uv run python -m executor.run --spec "$WORK/approved.yaml" --audience-dir "$AUDIENCE" \
  --target "$TARGET" --execute

step "7/7  Reconciliation + cutover plan"
uv run python -m executor.reconcile --spec "$WORK/approved.yaml" \
  --audience-dir "$AUDIENCE" --target "$TARGET" --out "$WORK/reconciliation.md"
sed -n '1,14p' "$WORK/reconciliation.md"
uv run python -m executor.cutover --spec "$WORK/approved.yaml" \
  --audience-dir "$AUDIENCE" --out "$WORK/cutover_plan.md"
sed -n '/## Rollback/,/## Sending ramp/p' "$WORK/cutover_plan.md"

echo
echo "Done. Artifacts in $WORK: discovery.md, approved.yaml, reconciliation.md, cutover_plan.md"
