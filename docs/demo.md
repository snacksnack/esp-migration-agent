# The two-minute demo (RC1-547)

The arc: a customer's export bundle goes in; a reviewed, approved, verified
migration comes out - with a human gate in the middle and the suppression
invariant enforced in code throughout. Nothing is ever sent on any target.

## Setup (once)

**Listmonk (default target, local):**

```bash
docker compose up -d
eval "$(uv run python -m executor.bootstrap --export)"
```

**Brevo (third-party target):** create a free account (brevo.com), add
`ESP_BREVO_API_KEY` to `.env` (or export it). The account's default sender is
used for draft campaigns; drafts never send.

## Run

```bash
bash scripts/demo.sh                        # listmonk, fast sample, ~2 min
ESP_DEMO_TARGET=brevo bash scripts/demo.sh  # same arc, third-party target
DEMO_FAST=0 bash scripts/demo.sh            # full 4,255-subscriber fixture
```

`DEMO_FAST=1` (default) migrates a deterministic 600-subscriber sample so the
full arc fits a short recording; `DEMO_FAST=0` is the real thing (the full
fixture takes ~15 minutes of write time per fresh target).

To reset the Listmonk target between takes: `docker compose down -v && docker
compose up -d`, then re-run the bootstrap eval line.

A rendered clip of the full arc lives at [docs/demo.gif](demo.gif)
(asciinema cast -> agg, idle time capped at 2s).

## Recording

- `asciinema rec demo.cast -c "bash scripts/demo.sh"` for a terminal cast, or
  a screen recorder over the terminal plus a browser tab on
  http://localhost:9000 (show the blocklisted 411 and the imported audience
  after step 6).
- `DEMO_PAUSE` controls the beat between steps (seconds, default 2).
- The arc to narrate: *"The export lands (1); discovery prices the engagement
  and finds the risks (2); the model proposes a mapping and asks instead of
  guessing (3); a human answers and approves - nothing downstream reads an
  unapproved spec (4); the diff before any write (5); suppressions migrate
  first, re-runs converge (6); reconciliation proves it, the cutover plan
  ships it (7)."*

## Demo safety

Synthetic subscribers only (ADR-0001); campaigns are created as drafts;
suppressed contacts land blocklisted/emailBlacklisted before any subscriber
is written; reconciliation enumerates suppression integrity at 100% on every
target.
