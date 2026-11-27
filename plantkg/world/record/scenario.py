"""Private, fixed-run scenario inputs; never install this reader as an agent tool."""
from datetime import datetime, timedelta

from plantkg.core.contracts import ErrorCode, PlantkgError
from plantkg.world.replay.clock import STEP
from plantkg.world.replay.series import online_record_hash


class ScenarioInputs:
    """Read explicitly frozen ex-post evidence for one candidate slot.

    A consumer must keep all results and derived state out of online interfaces
    until their source cutoff is visible. This reader itself publishes nothing.
    """

    def __init__(self, clock, series, runs):
        self.clock, self.series, self.runs = clock, series, runs

    def candidate(self, context, quantity, window, expected_seq):
        if not context.can_write or context.channel != "offline":
            raise PlantkgError(ErrorCode.FORBIDDEN, "场景数据需要可信离线上下文")
        run, seq, current = self.clock.current(context)
        if run.kind not in ("replay_day", "replay_week") or run.status not in ("created", "running"):
            raise PlantkgError(ErrorCode.PRECONDITION, "场景读取需要有效回放运行")
        if type(expected_seq) is not int or seq != expected_seq:
            raise PlantkgError(ErrorCode.STATE_CONFLICT, "回放时钟版本冲突")
        if (window.start != current or window.end != current + STEP or window.end > run.clock.end
                or (current - run.clock.start) % STEP):
            raise PlantkgError(ErrorCode.INVALID_REQUEST, "只能读取当前候选槽")
        inputs = self.runs.run_inputs(run.run_id)["inputs"]
        sources = inputs.get("execution_truth_source_ids")
        hashes = inputs.get("execution_truth_record_hashes")
        online = inputs.get("online_record_hashes")
        if (not isinstance(sources, list) or not sources or not all(isinstance(x, str) and x for x in sources)
                or not isinstance(hashes, list) or not hashes
                or not all(isinstance(x, str) and len(x) == 64 for x in hashes)
                or not isinstance(online, list) or not all(isinstance(x, str) for x in online)
                or set(hashes) & set(online)):
            raise PlantkgError(ErrorCode.PRECONDITION, "场景来源或指纹未固定，或与在线输入重叠")
        try:
            cutoff = datetime.fromisoformat(inputs["execution_truth_as_of"])
            if cutoff.utcoffset() != timedelta(0) or cutoff < window.end:
                raise ValueError()
        except (KeyError, TypeError, ValueError):
            raise PlantkgError(ErrorCode.PRECONDITION, "缺少有效的事后场景截止时间") from None
        # The fixed manifest still resolves the partition. The separate fixed
        # cutoff and fingerprints constrain this private, ex-post query.
        bundle = self.series.query(context.model_copy(update={"as_of": cutoff}), quantity, window)
        if bundle.source_id not in sources or any(r.source_id not in sources for r in bundle.records):
            raise PlantkgError(ErrorCode.NOT_VISIBLE, "场景数据不可用")
        selected = tuple(r for r in bundle.records if online_record_hash(r) in hashes and r.interval == window
                         and r.nature in ("configured", "derived") and r.quality.flag in ("ok", "estimated")
                         and r.value is not None and r.available_at <= cutoff)
        if len(selected) != 1:
            raise PlantkgError(ErrorCode.NOT_VISIBLE, "场景数据缺失或不唯一")
        return bundle.model_copy(update={"records": selected, "next_cursor": None})
