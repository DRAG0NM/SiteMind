---
name: correction-evolve
description: Turn operator corrections and sampled outcomes into trial-tested, reviewable knowledge releases.
allowed-tools:
  - plantkg_propose_candidates_evo
  - plantkg_backtest
  - plantkg_get_job
  - plantkg_propose_publish
  - plantkg_propose_rollback
---

# Correction Evolution

Use this workflow when an operator corrects an event ticket or approves a sampled result.

1. Record the event ticket, the correction, and any source asset reference with `propose_candidates_evo`.
2. Publish through `propose_publish`: it runs a fast backtest automatically (the candidate against the short demo window) and only proposes the action when the report passes; a one-sentence correction should return in seconds. Use `backtest` only when you need the full scenario set: it runs as a background job, so poll `get_job` with the same `job_id` (wait up to 40 seconds per call) until the report is ready. Trial and evaluation results use registered cached sources when available and deterministic sources otherwise; the report records deterministic coverage gaps.
3. Do not treat one correction as an active rule. A candidate can be proposed for publication only after its backtest passes and its base manifest is still active.
4. A reviewer confirms publication through the shared pending action flow. The new manifest creates the next knowledge version and a timeline record.
5. To undo a poor release, propose a rollback to a manifest that was active before and have a reviewer confirm it. Rollback changes the active reference and preserves version history.

Temporary rules are run-scoped and must not be included in a long-lived knowledge candidate.
