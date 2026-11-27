"""Select already indexed document paths from a fixed run input allowlist."""
from plantkg.core.contracts import ErrorCode, PlantkgError


def visible_document_paths(clock, runs, context, assets, indexed_paths):
    """Return only paths of allowed assets known by the trusted cutoff.

    ``indexed_paths`` maps asset version IDs to their already-indexed paths;
    it is not a retrieval interface. Evaluation assets must never be included
    in the run's frozen ``online_document_ids`` input.
    """
    run, _, _ = clock.current(context)
    allowed = runs.run_inputs(run.run_id)["inputs"].get("online_document_ids")
    if not isinstance(allowed, list) or not all(isinstance(x, str) for x in allowed):
        raise PlantkgError(ErrorCode.PRECONDITION, "运行没有固定的在线资料清单")
    return tuple(indexed_paths[a.asset_version_id] for a in assets
                 if a.asset_version_id in allowed and a.available_at <= context.as_of
                 and a.asset_version_id in indexed_paths)
