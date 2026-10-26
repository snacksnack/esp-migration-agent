# Cutover plan: mailchimp -> listmonk

Approved by Reid Collins on 2026-10-26 (content sha256 `af61b3ee519f...`). Executor runs exactly this content; any spec edit requires
re-approval before the next run.

## Scope

- Suppressions (phase 1, gating): 411
- Subscribers (phase 2): 3,844
- Draft campaigns (phase 3): 3
- Segment verification queries: 4
- Open mapping questions at approval: 0 (must be 0 - enforced)

## Freeze window (24h)

1. Announce the freeze: no audience edits, imports, signup-form changes, or
   sends in the source account during the window.
2. Pull a fresh source export at freeze start; re-run discovery against it
   and diff the counts against this plan's scope (deltas since the planning
   export migrate in this run; deltas after it do not exist by definition).
3. Keep source signup forms live - new signups during the freeze land in the
   source and migrate in the final delta pass (step 4 of the checklist).

## Migration order (the executor enforces phases 1-3)

1. **Suppressions first.** Every suppressed address lands non-subscribable
   before any subscriber is written. This ordering is structural in the
   executor and the spec schema; it is not an operator step that can be
   missed.
2. Subscribers with attributes, consent states per the approved spec.
3. Draft campaigns with translated merge tags (nothing is sent).
4. Final delta pass: re-run the executor against a fresh export; upserts
   converge, so only freeze-window deltas write.

## Parallel-run validation (7 days)

- Target sending stays OFF; source continues to serve any transactional mail.
- Run the reconciliation report daily
  (`python -m executor.reconcile --spec <approved spec>`): consent counts,
  100% suppression integrity, segment parity, sampled checksums, campaign
  render comparison. Any FAIL stops the cutover clock.
- Unsubscribes collected in the source during the parallel run are re-synced
  by re-running the executor (suppression phase picks them up first).

## Cutover checklist

- [ ] Freeze announced; fresh export pulled; discovery diff reviewed
- [ ] Executor dry-run diff reviewed (no unexpected creates/updates)
- [ ] Executor run complete; write summary archived
- [ ] Reconciliation report PASS archived
- [ ] Second executor run converges to zero writes (idempotency proof)
- [ ] Final delta pass complete after freeze end
- [ ] Signup forms and integrations repointed to the target
- [ ] First target send per the ramp below; bounce/complaint monitoring live
- [ ] Source account set read-only (retain for the retention period, then close)

## Rollback

**What rolls back, and how.** The source is never written: the executor has
no write path to mailchimp, so the source account remains a complete,
untouched system of record throughout. Rollback is therefore: repoint signup
forms and sending back to the source, delete the target list and its
subscribers (one API call each), and schedule a re-run. Suppression events
collected ONLY in the target during parallel run must be exported back to the
source before the wipe - this is the one data path that must be preserved.

**What cannot be rolled back.** Mail already sent from the target (including
any opt-in confirmations), and the reputation signals those sends generated.
This is why target sending stays off until the parallel-run reconciliation
passes.

## Sending ramp

The source account has no usable engagement signal (uniform member rating), so an engagement-cohort ramp cannot be derived from this export. Recommend ramping by signup recency instead (newest cohorts first) and tightening once the target accumulates its own opens.
