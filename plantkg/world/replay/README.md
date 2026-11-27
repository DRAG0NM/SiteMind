# B07 replay boundaries

`ReplayClock` stores a monotonically increasing B07 clock revision in the
`replay_clock_history` table. It is not the B04 `run_events.seq`. Initialization
is explicit; `RunInfo.clock` remains the immutable run interval. `at` does not
write and refuses future views. It uses `min(context.as_of, instant)` as its
knowledge cutoff, and rejects a trusted context whose cutoff exceeds the current
committed cursor. Slots are 15-minute boundaries in already-mapped UTC time.
The empty `pack_states` is the current minimal projection; B16 owns execution
state. `advance` requires a write context and checks sequence and run status in
the same SQLite short transaction as the inserted revision.

`fork_run_at` copies the parent's fixed inputs, seed, interval and manifest.
The new run and its initial clock revision commit or roll back together through
the B04 transaction hook. Clock initialization cannot repair a partial fork;
the atomic operation ensures no such partial fork is committed.

Online series require explicit immutable `inputs.online_source_ids` and
`inputs.online_record_hashes` lists when creating a run. Compute the latter
with `online_record_hash` for each record approved for online use. The referenced manifest and B03 resolver still determine
the actual partition. Absence of this list denies access; it never selects the
active manifest or an entire dataset. Evaluation data must not be placed on
this allowlist. Bundle notes and license text are not returned online.
`visible_document_paths` similarly requires `inputs.online_document_ids` and
an already indexed asset-to-path mapping; it is not a retrieval engine.
Evaluation data classification and creation of those trusted input lists belong
to the caller. Unapproved records remain excluded even if they share a partition
with approved records; identifiers and provenance of approved records are returned
as provided by B03 and must themselves be suitable for online disclosure.
`EvaluationTruth` is an internal-only reader for evaluation/backtest runs with
an explicit frozen `evaluation_source_ids` list. It is not installed in the
tool or REST registries; its trusted context controls the evaluation cutoff.

## Internal execution preparation (B16)

`ReplaySeries.candidate_observed(context, quantity, window, expected_seq)` is
an internal-only preparation method, never a REST/MCP/agent tool. It requires a
trusted write context, a replay run, the current B07 sequence and exactly the
next UTC quarter-hour. It reads the fixed manifest and both frozen online
source and record allowlists. Exactly one nonmissing, good-quality measured
record for the whole slot must be available by the slot end; otherwise it fails
closed. Its result is confidential to the executor until the candidate commits.
The method does not advance the clock and its data must not be published before
the transaction succeeds. Ordinary queries retain their original cutoff.

`ReplayClock.advance_in_transaction(con, context, expected_seq, next_instant)`
requires the same writer's already-open transaction and advances exactly one
slot, returning `(seq, instant)` without another connection or commit. After
preparing inputs outside the write transaction and calling B15 `executable`
for lazy approval finalization, B16 must recheck B15 eligibility and persist
its facts plus this CAS in one `atomic(writer, fn)` transaction. A stale
candidate must be discarded and prepared again. B07 sequence is not B04 event
sequence. The caller is responsible for ensuring `con` is the shared writer's
connection and for preserving isolation of the prepared data.

There is no offline execution truth mode here. Observations delayed beyond slot
end are rejected; exposing ex-post truth or derived state requires a separate
visibility architecture. No migration or B16 execution implementation is added.

Gateway code can bind a trusted run and manifest when it registers a B04 memory
session. An unbound MCP session is rejected for series reads; model parameters
cannot specify these values. REST view binds the path's run to its persisted
manifest, clamps the cutoff to the earlier of the default and current cursor,
and remains read-only. The existing anonymous REST context is retained.
