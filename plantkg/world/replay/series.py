"""Run-bound online query over the existing fixed-manifest SeriesStore."""
import base64
import hashlib
import json
from datetime import timedelta

from plantkg.core.contracts import ErrorCode, PlantkgError
from plantkg.world.replay.clock import STEP


def online_record_hash(record):
    """Stable identifier for a record explicitly approved as online input."""
    raw = json.dumps(record.model_dump(mode="json"), ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class ReplaySeries:
    def __init__(self, clock, series, runs):
        self.clock, self.series, self.runs = clock, series, runs

    def candidate_observed(self, context, quantity, window, expected_seq):
        """Internal preparation only; the caller must CAS before publishing results."""
        if not context.can_write:
            raise PlantkgError(ErrorCode.NO_CTX, "候选读取需要可信写上下文")
        run, seq, current = self.clock.current(context)
        if run.kind not in ("replay_day", "replay_week") or run.status not in ("created", "running"):
            raise PlantkgError(ErrorCode.PRECONDITION, "需要有效回放运行")
        if type(expected_seq) is not int or seq != expected_seq:
            raise PlantkgError(ErrorCode.STATE_CONFLICT, "回放时钟版本冲突")
        if (window.start.utcoffset() != timedelta(0) or window.end.utcoffset() != timedelta(0)
                or window.start != current or window.end != current + STEP or window.end > run.clock.end
                or (current - run.clock.start) % STEP):
            raise PlantkgError(ErrorCode.INVALID_REQUEST, "候选窗口必须是当前完整 UTC 槽")
        inputs = self.runs.run_inputs(run.run_id)["inputs"]
        allowed = inputs.get("online_source_ids")
        hashes = inputs.get("online_record_hashes")
        if not isinstance(allowed, list) or not allowed or not all(isinstance(x, str) and x for x in allowed):
            raise PlantkgError(ErrorCode.PRECONDITION, "运行没有固定的在线来源清单")
        if not isinstance(hashes, list) or not all(isinstance(x, str) and len(x) == 64 for x in hashes):
            raise PlantkgError(ErrorCode.PRECONDITION, "运行没有固定的在线记录清单")
        bundle = self.series.query(context.model_copy(update={"as_of": window.end}), quantity, window)
        if bundle.source_id not in allowed or any(r.source_id not in allowed for r in bundle.records):
            raise PlantkgError(ErrorCode.NOT_VISIBLE, "候选观测不可用")
        records = tuple(r for r in bundle.records if online_record_hash(r) in hashes and r.nature == "measured"
                        and r.interval == window and r.available_at <= window.end and r.value is not None
                        and r.quality.flag == "ok")
        if len(records) != 1:
            raise PlantkgError(ErrorCode.NOT_VISIBLE, "候选观测不可用或不唯一")
        return bundle.model_copy(update={"records": records, "next_cursor": None, "note": None, "license": None})

    def query(self, context, quantity, window, cursor=None, *, page_size=50):
        if isinstance(page_size, bool) or not isinstance(page_size, int) or not 1 <= page_size <= 50:
            raise PlantkgError(ErrorCode.INVALID_REQUEST, "page_size 须在 1～50")
        run, _, _ = self.clock.current(context)
        # Explicit frozen allowlist separates online input from evaluation truth.
        inputs = self.runs.run_inputs(run.run_id)["inputs"]
        allowed = inputs.get("online_source_ids")
        if not isinstance(allowed, list) or not allowed or not all(isinstance(x, str) for x in allowed):
            raise PlantkgError(ErrorCode.PRECONDITION, "运行没有固定的在线来源清单")
        record_hashes = inputs.get("online_record_hashes")
        if not isinstance(record_hashes, list) or not all(isinstance(x, str) and len(x) == 64 for x in record_hashes):
            raise PlantkgError(ErrorCode.PRECONDITION, "运行没有固定的在线记录清单")
        record_hashes = frozenset(record_hashes)
        key = json.dumps([run.run_id, run.version_manifest_id, quantity, window.start.isoformat(), window.end.isoformat(), context.as_of.isoformat(), "event_time_record_v1"], ensure_ascii=True, separators=(",", ":"))
        binding = hashlib.sha256(key.encode()).hexdigest()
        offset = 0
        if cursor:
            try:
                payload = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
                if payload["binding"] != binding or type(payload["offset"]) is not int or payload["offset"] < 0:
                    raise ValueError()
                offset = payload["offset"]
            except (ValueError, KeyError, TypeError, UnicodeError):
                raise PlantkgError(ErrorCode.INVALID_REQUEST, "分页游标无效或不属于当前查询") from None
        bundle = self.series.query(context, quantity, window)
        if bundle.source_id not in allowed or any(r.source_id not in allowed for r in bundle.records):
            raise PlantkgError(ErrorCode.NOT_VISIBLE, "来源不属于在线输入")
        visible = tuple(record for record in bundle.records if online_record_hash(record) in record_hashes)
        records = visible[offset:offset + page_size]
        end = offset + len(records)
        next_cursor = base64.urlsafe_b64encode(json.dumps({"binding": binding, "offset": end}, separators=(",", ":")).encode()).decode() if end < len(visible) else None
        return bundle.model_copy(update={"records": records, "next_cursor": next_cursor, "note": None, "license": None})
