"""反事实评分任务：数据提供者负责冻结划分、校准概率与同日比较输入。"""
from datetime import timedelta
from zoneinfo import ZoneInfo
from plantkg.core.contracts import ErrorCode, JobKind, PlantkgError, ToolResult
from plantkg.core.ports import AssetRepository, JobHandlerRegistrar, JobRunner, VersionStore
from plantkg.core.versions import digest
from plantkg.knowledge.rules.scoring import CounterfactualScorer, DayComparison, ReviewScore


def install_score_job(registry, candidates, settings):
    assets = registry.resolve(AssetRepository)

    def execute(payload, context, job_id):
        candidate = candidates.get(payload["candidate_id"])
        assets.get(context, candidate.source_asset_version)
        providers = registry.hooks_for("knowledge.counterfactual.inputs")
        comparators = registry.hooks_for("knowledge.counterfactual.tariff")
        inputs = providers[-1](context, candidate, payload["alternative"]) if providers else None
        if inputs is None:
            candidates.record_score(candidate.candidate_id, ReviewScore("uncalibrated", None, ()),
                                    manifest_id=context.version_manifest_id,
                                    provenance={"job_id": job_id, "reason": "尚无冻结评分数据与校准概率提供者"})
            return "b30-score:" + candidate.candidate_id
        split = inputs["split"]
        if inputs["probability_b"] is not None and not comparators:
            raise PlantkgError(ErrorCode.DEPENDENCY_UNAVAILABLE, "缺少电价反事实比较器")
        if inputs["probability_b"] is not None and not inputs.get("calibration_version"):
            raise PlantkgError(ErrorCode.PRECONDITION, "备选解释的概率缺少校准版本")
        if split.frozen_at > context.as_of:
            raise PlantkgError(ErrorCode.NOT_VISIBLE, "评分划分在本次时点尚未冻结")
        def compare(day, as_of):
            if as_of > context.as_of:
                raise PlantkgError(ErrorCode.NOT_VISIBLE, "评分日的预测截止时点尚未到达")
            kwargs = inputs["day_inputs"](day, as_of)
            if kwargs["context"].as_of != as_of:
                raise PlantkgError(ErrorCode.PRECONDITION, "评分输入未绑定评分日前一天的截止时点")
            if (kwargs["context"].version_manifest_id != context.version_manifest_id
                    or kwargs["context"].plant_id != context.plant_id):
                raise PlantkgError(ErrorCode.PRECONDITION, "评分输入的厂区或版本不匹配")
            if not kwargs.get("solver_budget") or kwargs["solver_budget"].mip_rel_gap != inputs["mip_rel_gap"]:
                raise PlantkgError(ErrorCode.PRECONDITION, "求解容差与评分容差不匹配")
            zone = ZoneInfo(settings.timezone)
            start = kwargs["horizon"].start.astimezone(zone)
            end = kwargs["horizon"].end.astimezone(zone)
            if (start.date().isoformat() != day or (start.hour, start.minute, start.second, start.microsecond) != (0, 0, 0, 0)
                    or end.date() != start.date() + timedelta(days=1)
                    or (end.hour, end.minute, end.second, end.microsecond) != (0, 0, 0, 0)):
                raise PlantkgError(ErrorCode.PRECONDITION, "评分规划区间须覆盖当地评分日")
            try:
                return DayComparison(**comparators[-1](**kwargs))
            except PlantkgError as exc:
                if exc.code not in (ErrorCode.INFEASIBLE, ErrorCode.PRECONDITION):
                    raise
                return DayComparison("incomparable", "incomparable", 0.0, 0.0, 0.0)
        score = CounterfactualScorer(compare).score(
            manifest_id=context.version_manifest_id, candidate_id=candidate.candidate_id,
            alternative=payload["alternative"], probability_b=inputs["probability_b"], split=split,
            plant_timezone=settings.timezone, mip_rel_gap=inputs["mip_rel_gap"])
        candidates.record_score(candidate.candidate_id, score, manifest_id=context.version_manifest_id,
            provenance={"job_id": job_id, "alternative_hash": "sha256:" + digest(payload["alternative"]), "split_id": split.split_id,
                        "split_hash": split.content_hash, "probability_b": inputs["probability_b"],
                        "calibration_version": inputs.get("calibration_version"), "mip_rel_gap": inputs["mip_rel_gap"],
                        "nature": inputs.get("nature", "unspecified")})
        return "b30-score:" + candidate.candidate_id

    if JobHandlerRegistrar in registry.ports:
        registry.resolve(JobHandlerRegistrar).register(JobKind.counterfactual_score, execute, fallback=True)

    def submit(context, candidate_id, alternative):
        import secrets
        candidate = candidates.get(candidate_id)
        assets.get(context, candidate.source_asset_version)
        if not context.version_manifest_id:
            raise PlantkgError(ErrorCode.PRECONDITION, "评分须绑定知识版本")
        if not registry.resolve(VersionStore).get(context.version_manifest_id).ever_active:
            raise PlantkgError(ErrorCode.NOT_VISIBLE, "评分须绑定曾经发布的知识版本")
        job = registry.resolve(JobRunner).submit(JobKind.counterfactual_score,
                    {"candidate_id": candidate_id, "alternative": alternative},
                    context.idempotency_key or "b30-score-" + secrets.token_hex(12), context)
        return ToolResult(ok=True, business_status="pending", job_id=job.job_id)

    registry.register_tool("score_review_candidate", submit)
