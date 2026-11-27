import threading
from datetime import timedelta

import pytest

from plantkg.core.contracts import ErrorCode, EvidenceEvent, Interval, PlantkgError
from plantkg.core.evidence import SqliteEvidenceSink
from plantkg.core.runs import SqliteRunStore
from plantkg.core.versions import ManifestReferences, SqliteVersionStore
from tests.versions.conftest import AS_OF, PLANT, REVIEWER, component, context


def publish(store, ref, expected, n=0):
    m = store.prepare({"rules": component(ref, n)}, "test")
    store.check_integrity(m.version_manifest_id)
    return store.activate(expected, m.version_manifest_id, REVIEWER, "发布 " + ref)


def test_prepare_verify_activate_and_audit(versions):
    m = versions.prepare({"rules": component("t:rules@v1")}, "首版")
    assert m.status == "prepared" and versions.prepare({"rules": component("t:rules@v1")}, "重试").version_manifest_id == m.version_manifest_id
    with pytest.raises(PlantkgError) as none:
        versions.active()
    assert none.value.code == ErrorCode.PRECONDITION
    assert versions.check_integrity(m.version_manifest_id).status == "verified"
    active = versions.activate(None, m.version_manifest_id, REVIEWER, "首次发布")
    assert active.status == "active" and active.ever_active and active.activated_by == REVIEWER.actor_id
    assert versions.activate(None, m.version_manifest_id, REVIEWER, "重试") == active  # 同一请求重试原样返回
    history = versions.history(m.version_manifest_id)
    assert [(h["from_status"], h["to_status"]) for h in history] == [(None, "prepared"), ("prepared", "verified"), ("verified", "active")]
    assert history[-1]["actor_id"] == REVIEWER.actor_id and history[-1]["occurred_at"]


@pytest.mark.parametrize("failure", ["missing", "corrupt"])
def test_broken_artifact_discards_and_keeps_active(versions, statuses, failure):
    v1 = publish(versions, "t:rules@v1", None)
    bad = versions.prepare({"rules": component("t:rules@v2", 2)}, "坏产物")
    statuses["t:rules@v2"] = failure
    with pytest.raises(PlantkgError):
        versions.check_integrity(bad.version_manifest_id)
    assert versions.get(bad.version_manifest_id).status == "discarded"
    with pytest.raises(PlantkgError):
        versions.activate(v1.version_manifest_id, bad.version_manifest_id, REVIEWER, "不应生效")
    assert versions.active().version_manifest_id == v1.version_manifest_id


def test_unverifiable_stays_prepared_and_corruption_after_verify_blocks_activation(versions, statuses):
    v1 = publish(versions, "t:rules@v1", None)
    unknown = versions.prepare({"rules": component("x:rules@v9")}, "无校验函数")
    with pytest.raises(PlantkgError) as exc:
        versions.check_integrity(unknown.version_manifest_id)
    assert exc.value.code == ErrorCode.PRECONDITION and versions.get(unknown.version_manifest_id).status == "prepared"
    v2 = versions.prepare({"rules": component("t:rules@v2", 2)}, "v2")
    versions.check_integrity(v2.version_manifest_id)
    statuses["t:rules@v2"] = "corrupt"  # 校验后被带外改动
    with pytest.raises(PlantkgError):
        versions.activate(v1.version_manifest_id, v2.version_manifest_id, REVIEWER, "激活前检查")
    assert versions.active().version_manifest_id == v1.version_manifest_id


def test_crash_inside_activation_rolls_back(writer, statuses):
    class Crashing(SqliteVersionStore):
        def _save(self, con, manifest):
            super()._save(con, manifest)
            if manifest.status == "active":
                raise RuntimeError("模拟激活中途崩溃")
    store = Crashing(writer, PLANT)
    store.register_verifier("t:", lambda c: "complete")
    plain = SqliteVersionStore(writer, PLANT)
    plain.register_verifier("t:", lambda c: "complete")
    v1 = publish(plain, "t:rules@v1", None)
    v2 = plain.prepare({"rules": component("t:rules@v2", 2)}, "v2")
    plain.check_integrity(v2.version_manifest_id)
    with pytest.raises(RuntimeError):
        store.activate(v1.version_manifest_id, v2.version_manifest_id, REVIEWER, "崩溃")
    assert plain.active().version_manifest_id == v1.version_manifest_id
    assert plain.get(v1.version_manifest_id).status == "active" and plain.get(v2.version_manifest_id).status == "verified"


def test_competing_publishers_only_matching_expected_wins(versions):
    v1 = publish(versions, "t:rules@v1", None)
    candidates = []
    for i in range(8):
        m = versions.prepare({"rules": component(f"t:rules@c{i}", 100 + i)}, "竞争")
        versions.check_integrity(m.version_manifest_id)
        candidates.append(m.version_manifest_id)
    results, barrier = [], threading.Barrier(len(candidates))

    def race(candidate):
        barrier.wait()
        try:
            results.append(("ok", versions.activate(v1.version_manifest_id, candidate, REVIEWER, "竞争发布").version_manifest_id))
        except PlantkgError as exc:
            results.append((exc.code, candidate))
    threads = [threading.Thread(target=race, args=(c,)) for c in candidates]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    winners = [r[1] for r in results if r[0] == "ok"]
    assert len(winners) == 1 and versions.active().version_manifest_id == winners[0]
    assert {r[0] for r in results if r[0] != "ok"} == {ErrorCode.STATE_CONFLICT}
    assert sum(m.status == "active" for m in versions.list(page_size=50)[0]) == 1


def test_rollback_reactivates_retired_without_touching_history(writer, versions):
    runs, sink = SqliteRunStore(writer, PLANT, versions), SqliteEvidenceSink(writer, PLANT)
    v1 = publish(versions, "t:rules@v1", None)
    clock = Interval(start=AS_OF, end=AS_OF + timedelta(days=1))
    run = runs.create_run(context(), "replay_day", clock, seed=7, inputs={"series": "series:v1:" + "a" * 64})
    event = sink.append(EvidenceEvent(event_id="ev-1", seq=0, occurred_at=AS_OF, run_id=run.run_id, trace_id="tr-1", event_type="rule_applied",
                                      rule_ids=("r-1",), version_manifest_id=v1.version_manifest_id, result_status="ok"))
    v2 = publish(versions, "t:rules@v2", v1.version_manifest_id, 2)
    assert versions.get(v1.version_manifest_id).status == "retired"
    new_run = runs.create_run(context(), "replay_day", clock)
    assert new_run.version_manifest_id == v2.version_manifest_id
    assert runs.get_run(run.run_id) == run and runs.run_inputs(run.run_id)["seed"] == 7
    assert sink.list(run.run_id, None, context()).items == (event,)
    # 回滚只能到 retired；discarded 不行
    rolled = versions.activate(v2.version_manifest_id, v1.version_manifest_id, REVIEWER, "回滚")
    assert rolled.status == "active" and rolled.previous_active_id == v2.version_manifest_id
    assert runs.get_run(new_run.run_id).version_manifest_id == v2.version_manifest_id
    discarded = versions.prepare({"rules": component("t:rules@v3", 3)}, "v3")
    versions.discard(discarded.version_manifest_id, REVIEWER, "放弃")
    with pytest.raises(PlantkgError) as exc:
        versions.activate(v1.version_manifest_id, discarded.version_manifest_id, REVIEWER, "不能回到丢弃的")
    assert exc.value.code == ErrorCode.STATE_CONFLICT


def test_runs_bind_candidates_but_not_unverified_and_record_transitions(writer, versions):
    runs = SqliteRunStore(writer, PLANT, versions)
    v1 = publish(versions, "t:rules@v1", None)
    candidate = versions.prepare({"rules": component("t:rules@cand", 5)}, "候选")
    clock = Interval(start=AS_OF, end=AS_OF.replace(hour=11))
    with pytest.raises(PlantkgError) as exc:
        runs.create_run(context(version_manifest_id=candidate.version_manifest_id), "backtest", clock)
    assert exc.value.code == ErrorCode.PRECONDITION
    versions.check_integrity(candidate.version_manifest_id)
    backtest = runs.create_run(context(version_manifest_id=candidate.version_manifest_id), "backtest", clock)
    assert backtest.version_manifest_id == candidate.version_manifest_id != v1.version_manifest_id
    with pytest.raises(PlantkgError):
        runs.create_run(context(actor=None, channel="none", agent_run_id=None), "replay_day", clock)
    runs.append_event(backtest.run_id, "started", REVIEWER, to_status="running")
    runs.append_event(backtest.run_id, "note", REVIEWER, {"msg": "x"})
    with pytest.raises(PlantkgError) as bad:
        runs.append_event(backtest.run_id, "restart", REVIEWER, to_status="created")
    assert bad.value.code == ErrorCode.STATE_CONFLICT
    assert runs.append_event(backtest.run_id, "done", REVIEWER, to_status="finished").status == "finished"
    events = runs.events(backtest.run_id)
    assert [e["seq"] for e in events] == [1, 2, 3, 4]
    assert all(e["actor_id"] == REVIEWER.actor_id and e["version_manifest_id"] == candidate.version_manifest_id and e["occurred_at"] for e in events)
    forked = runs.create_run(context(), "replay_day", clock, forked_from=backtest.run_id)
    assert forked.forked_from == backtest.run_id


def test_evidence_seq_idempotency_and_paging(writer):
    sink = SqliteEvidenceSink(writer, PLANT)
    def ev(i, run="run-a", **kw):
        return EvidenceEvent(event_id=f"ev-{i}", seq=99, occurred_at=AS_OF, run_id=run, trace_id="tr-x", event_type="rule_applied",
                             action_id=kw.get("action"), result_status="ok", rule_ids=(f"r-{i}",))
    stored = [sink.append(ev(i, run="run-a" if i % 2 else "run-b", action="act-1" if i == 3 else None)) for i in range(1, 8)]
    assert [s.seq for s in stored] == sorted(s.seq for s in stored) and len({s.seq for s in stored}) == 7
    assert sink.append(ev(1)) == stored[0]
    with pytest.raises(PlantkgError) as exc:
        sink.append(ev(1).model_copy(update={"rule_ids": ("other",)}))
    assert exc.value.code == ErrorCode.IDEMPOTENCY_MISMATCH
    page = sink.list("run-a", None, context(), page_size=2)
    rest = sink.list("run-a", page.next_cursor, context(), page_size=2)
    assert [e.event_id for e in page.items + rest.items] == ["ev-1", "ev-3", "ev-5", "ev-7"]
    assert [e.event_id for e in sink.list(None, None, context(), action_id="act-1").items] == ["ev-3"]
    with pytest.raises(PlantkgError):
        sink.list(None, None, context(plant_id="other"))
    seen = []
    def consume():
        for e in sink.subscribe("tr-x", poll_s=0.05):
            seen.append(e.event_id)
    t = threading.Thread(target=consume)
    t.start()
    sink.append(ev(8))
    sink.close()
    t.join(5)
    assert not t.is_alive() and seen[-1] == "ev-8" and len(seen) == 8


def test_manifest_references_resolve_only_fixed_manifest(writer, versions):
    from plantkg.storage.graph import OxigraphStore
    refs = ManifestReferences()
    refs.bind(versions)
    graph = OxigraphStore(writer.path.parent / "graph", writer, PLANT, refs.graph)
    try:
        ref = graph.prepare("plant", "<urn:a> <urn:b> <urn:c> .")
        with writer.read_only() as con:
            checksum = con.execute("SELECT checksum FROM storage_artifacts WHERE reference=?", (ref,)).fetchone()[0]
        from plantkg.bootstrap import _storage_verifier
        storage = type("S", (), {"writer": writer, "graph": graph, "series": None})()
        versions.register_verifier("graph:v1:", _storage_verifier(storage, PLANT))
        wrong = versions.prepare({"ontology": {"ref": ref, "content_hash": "sha256:" + "0" * 64}}, "散列不符")
        with pytest.raises(PlantkgError):
            versions.check_integrity(wrong.version_manifest_id)
        assert versions.get(wrong.version_manifest_id).status == "discarded"
        m = versions.prepare({"ontology": {"ref": ref, "content_hash": "sha256:" + checksum}}, "本体")
        ctx = context(version_manifest_id=m.version_manifest_id)
        assert refs.graph(ctx, "plant") is None  # prepared 不可用
        versions.check_integrity(m.version_manifest_id)
        assert refs.graph(ctx, "plant") == ref and refs.graph(ctx, "other") is None
        assert refs.graph(context(), "plant") is None  # 未固定清单不回退到最新
        rows, _ = graph.read_named_graph(ctx, "plant", "triples", {})
        assert len(rows) == 1
    finally:
        graph.close()


def test_series_map_requires_bound_reader(versions):
    versions.register_verifier("series-map:", lambda _: "complete")
    manifest = versions.prepare({"data_sources": component("series-map:v1:test")}, "test")
    versions.check_integrity(manifest.version_manifest_id)
    refs = ManifestReferences()
    refs.bind(versions)
    ctx = context(version_manifest_id=manifest.version_manifest_id)

    with pytest.raises(PlantkgError) as missing:
        refs.series(ctx, "baseline_load")
    assert missing.value.code == ErrorCode.DEPENDENCY_UNAVAILABLE

    class SourceMaps:
        def read_series_map(self, source):
            assert source.ref == "series-map:v1:test"
            return {"baseline_load": "series:v1:baseline"}

    refs.bind_source_maps(SourceMaps())
    assert refs.series(ctx, "baseline_load") == ("series:v1:baseline",)
    assert refs.series(ctx, "unknown") == ()
    assert refs.series(context(), "baseline_load") == ()
