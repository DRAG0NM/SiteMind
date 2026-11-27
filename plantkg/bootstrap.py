"""唯一装配层：按配置构建注册表、安全壳与应用。各平面只经端口在这里接线。

装配规则：先加载领域包与模块的真实实现，mock/dev 再用 Fake 补齐空缺槽位；真实实现无论先后都替换 Fake。
prod 不装 Fake，缺必需端口即启动失败。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Mapping

from starlette.applications import Starlette

from plantkg.core.config import Settings, secret_values
from plantkg.core.contracts import Actor, AgentResultKind, Context, ContextChannel, PendingActionKind
from plantkg.core import actions, evidence, jobs, runs, sessions, versions
from plantkg.core.ports import AgentResultSource, EvidenceSink, Executor, JobHandlerRegistrar, JobRunner, PendingActionStore, RunSessionStore, RunStore, VersionStore
from plantkg.core.registry import Registry, RegistryError
from plantkg.core.security import SessionCtxResolver, ToolGuard

PACKS_DIR = Path(__file__).resolve().parent / "packs"
REQUIRED_PORTS = (RunSessionStore,)  # 安全壳必需；B04 正式实现随存储启用（storage.enabled）装配
# 本地装配（装 Fake、允许 live 回退到 deterministic）：mock 离线、dev 开发、demo 演示（另由 configs/demo.yaml 开存储）
LOCAL_PROFILES = ("mock", "dev", "demo")
# v0.8 运行模式自检（B46）：prod 的 live 模式与 evaluation 必须能取到每类 Agent 结果
MODE_REQUIREMENTS = {AgentResultSource: tuple(k.value for k in AgentResultKind)}


@dataclass(frozen=True)
class Services:
    settings: Settings
    registry: Registry
    resolver: SessionCtxResolver
    guard: ToolGuard
    storage: object | None = None
    version_run: object | None = None  # B04 装配结果：operator（本机运维令牌）与 close()
    gate: object | None = None  # B15 持久化计划与门控服务；运行上下文必须显式绑定
    executor: object | None = None  # B16 内部服务；缺少批准快照时失败关闭
    nexent: object | None = None  # B23 网关与运行记录（nexent.enabled=false 时 gateway 为空）

    def close(self) -> None:
        for close_watchdog in self.registry.hooks_for("watchdog.close"):
            close_watchdog()
        try:
            if self.version_run is not None:
                self.version_run.close()
        finally:
            if self.storage is not None:
                self.storage.close()


def build_services(settings: Settings, packs_dir: Path | None = None, *, storage_resolver=None) -> Services:
    registry = Registry()
    packs_dir = packs_dir or PACKS_DIR
    from plantkg.storage.assembly import StorageResources
    references = None if storage_resolver is not None else versions.ManifestReferences()
    storage = StorageResources(settings, registry, storage_resolver or references)
    version_run = None
    try:
        if storage.enabled:
            version_run = VersionRun(settings, registry, storage, references)
            from plantkg.world.replay.providers import install as install_replay
            replay_clock, replay_series = install_replay(registry, storage.writer, version_run.runs, storage.series)
            version_run.evidence.run_clock = _evidence_run_clock(version_run, replay_clock)  # /evidence?run_id= 按运行时钟过滤（#122/#98）
            _register_assets(settings, registry, storage)
            from plantkg.decision.gate import install as install_gate
            version_run.gate = install_gate(registry, storage.writer, settings.plant_id, version_run.actions,
                                            version_run.runs, version_run.versions, version_run.evidence,
                                            run_clock=replay_clock.current_for_run)
            from plantkg.world.record.executor import RecordExecutor
            version_run.executor = RecordExecutor(storage.writer, replay_clock, version_run.gate, replay_series, version_run.runs)
            registry.register(Executor, version_run.executor)
            from plantkg.world.record.providers import install as install_ledger
            install_ledger(registry, storage.writer, replay_clock, version_run.runs)
        services = _finish_services(settings, packs_dir, registry, storage, version_run)
        if version_run is not None:
            version_run.actions.set_action_clock(_action_clock(services, replay_clock))
            if settings.profile == "demo":
                _seed_demo_pending_action(services)
            # B14 depends on replay ports installed above and on the completed
            # Services bundle. Missing rule/cost material remains fail-closed.
            from plantkg.decision.forecasting import ForecastService
            from plantkg.decision.gate.materials import B14MaterialsProvider
            version_run.gate.materials_provider = B14MaterialsProvider(ForecastService.from_services(services))
        return services
    except BaseException:
        try:
            if version_run is not None:
                version_run.close()
        finally:
            storage.close()
        raise


B47_PENDING_KINDS = frozenset({"work_order", "human_decision", "correction", "sampling", "review"})


def _action_clock(services: Services, replay_clock: object):
    """待确认动作的时钟：计划确认走回放游标；B47 工单与队列项走所在场景运行的当前时刻，避免拿墙钟排场景动作。

    场景运行没有回放游标；找不到绑定运行时退回墙钟，时钟缺失不能挡住开单（#169）。
    """
    def clock(kind: str, run_id: str | None):
        if kind == "plan_confirmation":
            return replay_clock.current_for_run(run_id)
        if kind in B47_PENDING_KINDS:
            for hook in services.registry.hooks_for("router.urgent"):
                owner = getattr(hook, "__self__", None)
                find = getattr(owner, "_runner_for_run", None)
                runner = find(run_id) if callable(find) else None
                if runner is not None:
                    return runner.now
        return actions.utcnow()
    return clock


DEMO_SEED_TARGET = "demo:act-183"
DEMO_SEED_ACTION_ID = "act-demo-183"
DEMO_SEED_KEY = "demo-seed-act-183"


def _seed_demo_pending_action(services: Services) -> None:
    """demo 演示：启动时放一条标 synthetic 的待确认动作，README 的确认流程才有操作对象（#183 D1）。

    只按 target_ref 判断 demo 种子是否存在，库里其他待确认动作不挡种子；写入走存储层的幂等插入
    （固定动作编号、固定幂等键，payload 不带启动时间），并发启动最终也只有一条（#203 复审）。
    """
    store = services.registry.resolve(PendingActionStore)
    if any(action.target_ref == DEMO_SEED_TARGET for action in store.list(status="proposed")[0]):
        return
    settings = services.settings
    context = Context(plant_id=settings.plant_id, industry_pack=settings.industry_pack,
                      actor=Actor(actor_id="demo-seed", roles=()), channel=ContextChannel.offline,
                      trace_id="demo-synthetic-183", as_of=versions.utcnow(), idempotency_key=DEMO_SEED_KEY)
    store.propose(PendingActionKind.correction, {
        "target_ref": DEMO_SEED_TARGET, "item_id": "demo-183", "synthetic": True,
        "note": "demo 启动写入的演示待确认动作（synthetic），确认不执行真实工单"}, context,
        action_id=DEMO_SEED_ACTION_ID)


def _evidence_run_clock(version_run: object, replay_clock: object):
    """证据查询的运行时钟：回放运行取回放游标；场景运行取已写入事件的最新时刻；其它运行用只读上下文的 as_of。"""
    def clock(run_id: str):
        try:
            return replay_clock.current_for_run(run_id)
        except Exception:  # noqa: BLE001 — 场景运行没有回放游标
            pass
        run = version_run.runs.get_run(run_id)
        if run.kind != "scenario":
            return None
        return version_run.evidence.latest_event_at(run_id)
    return clock


class VersionRun:
    """B04 装配：版本清单、运行、证据、会话、后台任务与待确认动作共用 B03 的 SQLite 写者。"""

    def __init__(self, settings: Settings, registry: Registry, storage: object, references: object | None) -> None:
        writer, plant = storage.writer, settings.plant_id
        config = settings.section("actions") or {}
        self.versions = versions.SqliteVersionStore(writer, plant)
        for prefix in ("graph:v1:", "series:v1:"):
            self.versions.register_verifier(prefix, _storage_verifier(storage, plant))
        from plantkg.storage.integration_artifacts import IntegrationArtifacts
        self.integration_artifacts = IntegrationArtifacts(writer, storage.series, plant, writer.path.parent / "integration_artifacts")
        self.versions.register_verifier("series-map:v1:", self.integration_artifacts.verify_map)
        self.versions.register_verifier("parameters:v1:", self.integration_artifacts.verify_parameters)
        if references is not None:
            references.bind(self.versions)
            references.bind_source_maps(self.integration_artifacts)
        self.runs = runs.SqliteRunStore(writer, plant, self.versions)
        self.evidence = evidence.SqliteEvidenceSink(writer, plant)
        self.operator = actions.OperatorAuth(config.get("reviewers", ()))
        budgets = {"run_day": settings.planning.solver_budget.time_limit_s}
        self.jobs = jobs.SqliteJobRunner(writer, plant, budgets | {k: settings.planning.background_budget["time_limit_s"]
                                                                   for k in ("run_week", "backtest", "counterfactual_score", "knowledge_eval", "ingest")})
        try:
            self._register(settings, registry, config)
        except BaseException:
            self.close()
            raise

    def _register(self, settings: Settings, registry: Registry, config: dict) -> None:
        self.actions = actions.SqlitePendingActionStore(self.versions.writer, settings.plant_id, registry.action_handlers, self.evidence,
                                                        ttl_s=int(config.get("ttl_s", 24 * 3600)))
        self.sessions = sessions.MemoryRunSessionStore(settings.industry_pack)
        for port, impl in ((VersionStore, self.versions), (RunStore, self.runs), (EvidenceSink, self.evidence), (JobRunner, self.jobs),
                           (JobHandlerRegistrar, self.jobs), (PendingActionStore, self.actions), (RunSessionStore, self.sessions)):
            registry.register(port, impl)
        providers = {("GET", p): f for module, store in ((versions, self.versions), (runs, self.runs), (evidence, self.evidence), (jobs, self.jobs))
                     for p, f in module.rest_providers(store).items()}
        for (method, path), provider in (providers | actions.rest_providers(self.actions, self.operator)).items():
            registry.register_rest(method, path, provider)
        jobs.register_tools(registry, self.jobs)

    def close(self) -> None:
        self.jobs.close()
        self.evidence.close()


def _register_assets(settings: Settings, registry: Registry, storage: object) -> None:
    """B06 资产目录：与 B04 共用 B03 写者；原文快照放在数据库旁，相对资料路径按仓库根解析（同 storage 路径规则）。"""
    from plantkg.knowledge import assets

    blob_dir = storage.writer.path.parent / "asset_blobs"
    assets.register(registry, assets.SQLiteAssetCatalog(storage.writer, settings.plant_id, blob_dir,
                                                        base_dir=Path(__file__).resolve().parents[1]))


def _storage_verifier(storage: object, plant_id: str):
    """B03 存储引用：目录登记完整，且清单里的 content_hash 等于目录登记的产物 checksum。"""
    from plantkg.storage.integrity import check_references

    def verify(component) -> str:
        status = check_references(storage.writer, storage.graph, storage.series, plant_id, [component.ref])[0].status
        if status != "complete":
            return status
        with storage.writer.read_only() as con:
            row = con.execute("SELECT checksum FROM storage_artifacts WHERE plant_id=? AND reference=?", (plant_id, component.ref)).fetchone()
        return "complete" if row and component.content_hash == "sha256:" + row[0] else "corrupt"
    return verify


def _finish_services(settings: Settings, packs_dir: Path, registry: Registry, storage: object, version_run: object | None) -> Services:
    from plantkg.backends import create_judge
    from plantkg.core.ports import Judge

    registry.register(Judge, create_judge(settings.judge))
    registry.live_fallback = settings.profile in LOCAL_PROFILES
    registry.load(sorted(p for p in packs_dir.iterdir() if p.is_dir()), settings.packs, settings)
    document_upload = None
    if version_run is not None:
        from plantkg.knowledge.documents.upload import install as install_document_upload
        document_upload = install_document_upload(registry, storage.writer, version_run.operator, settings)
    if "evolution" in registry.packs:
        registry.register_hook("evolution.backtest_evaluator",
                               lambda candidate, scenario, context: _scenario_backtest(registry, candidate, scenario, context, settings))
        registry.register_hook("evolution.fast_scenarios",
                               lambda candidate, context: _fast_scenarios(registry, candidate, settings))
        registry.register_hook("evolution.prewarm_baseline",
                               lambda candidate, context, base_manifest_id=None:
                               _warm_fast_baselines(registry, context, base_manifest_id, candidate, settings))
        registry.register_hook("b43.action_vocabulary", _action_vocabulary)
        registry.register_hook("evolution.backtest_versions", lambda: {
            "profile": settings.profile, "judge_backend": settings.judge.backend, "trial_prompt": "b32-trial-v1"})
        if document_upload is not None:
            # 必须在 evolution.fast_scenarios 注册之后：本钩子包住最后一个 fast_scenarios 实现。
            document_upload.install_local_backtest()
    if settings.profile in LOCAL_PROFILES:
        from plantkg.testing import install_fakes

        install_fakes(registry, settings)
    missing = [p.__name__ for p in REQUIRED_PORTS if p not in registry.ports]
    if missing:
        raise RegistryError(f"{settings.profile} profile 缺少必需端口 {missing}（RunSessionStore 由 B04 提供，需开启 storage.enabled）")
    resolver = SessionCtxResolver(registry.resolve(RunSessionStore))
    default_ctx = settings.security.default_ctx
    default_actor = None
    if default_ctx.enabled and settings.profile in LOCAL_PROFILES:
        default_actor = Actor(actor_id=default_ctx.actor_id, roles=default_ctx.roles)
    guard = ToolGuard(resolver, settings.plant_id, settings.industry_pack, settings.world.default_as_of, default_actor,
                      secrets_to_block=secret_values(settings))
    # B23：专家提案包装、网关与 AgentResultSource 的 live 实现（领域包加载后、模式自检前）
    from plantkg.integrations import nexent
    nexent_integration = nexent.install(registry, settings, guard, writer=storage.writer if storage.enabled else None,
                                        operator=version_run.operator if version_run is not None else None)
    if version_run is not None:  # #245 人工干预：暂停自动执行、取消与撤销动作（运维令牌鉴权）
        from plantkg.agent.execution_policy.pack import register_rest as register_override_rest
        register_override_rest(registry, version_run.operator)
    from plantkg.world.scenario.day_api import install as install_day_api
    install_day_api(registry)
    from plantkg.agent.watchdog import install as install_watchdog
    close_watchdog = install_watchdog(registry, settings, storage.writer if storage.enabled else None, nexent_integration.store,
                                      jobs=version_run.jobs if version_run is not None else None)
    if close_watchdog is not None:
        registry.register_hook("watchdog.close", close_watchdog)
    registry.check_modes(settings.profile, MODE_REQUIREMENTS)
    check_tool_catalog(registry)
    return Services(settings=settings, registry=registry, resolver=resolver, guard=guard, storage=storage, version_run=version_run,
                    gate=version_run.gate if version_run is not None else None,
                    executor=version_run.executor if version_run is not None else None, nexent=nexent_integration)


FAST_SCENARIO = Path(__file__).resolve().parents[1] / "configs" / "scenarios" / "fast" / "b32_demo.yaml"


def _action_vocabulary() -> dict[str, Any]:
    """B43 禁区命令归一：复用 B25/B28 的实际命令→概念动作词表与切断类动作集合，跨平面只在装配层取。"""
    from plantkg.knowledge.graph.graph import COMMAND_ACTIONS, CUT_SUPPLY

    return {"aliases": {str(key): tuple(value) for key, value in COMMAND_ACTIONS.items()},
            "cut_supply": tuple(sorted(CUT_SUPPLY))}


def _scenario_backtest(registry, candidate, scenario_spec, context, settings=None):
    """隔离回测：基准与候选各跑一次，比较目标类处理与其余剧本的非退化指标。

    快回测（scenario_spec["fast"]）：只推进到目标类事件处理完 + 收尾余量，或固定短窗口；
    基准结果按 (base_manifest_id, scenario_id, seed, 窗口, 场景文件摘要) 缓存复用，同一知识版本下重复发布只跑候选一侧。
    """
    import tempfile
    import time

    from plantkg.core.contracts import ComponentRef, ErrorCode, PlantkgError, Role, Rule
    from plantkg.core.ports import RuleMatcher
    from plantkg.core.versions import SqliteVersionStore
    from plantkg.evolution.artifacts import policy_artifacts, sensor_artifacts, skill_artifacts
    from plantkg.evolution.service import evolution_config
    from plantkg.evolution.store import EvolutionStore
    from plantkg.knowledge.rules.publisher import RuleArtifacts
    from plantkg.storage.migration import MigrationRunner
    from plantkg.storage.sqlite import SQLiteWriter
    from plantkg.world.scenario.__main__ import build_world
    from plantkg.world.scenario.runner import ScenarioDB, ScenarioRunner, default_registry
    from plantkg.world.scenario.script import load_scenario

    from plantkg.agent.router.router import EVENT_DOMAINS
    from plantkg.agent.router.severity import load_severity_config

    path = scenario_spec.get("path")
    if not path:
        raise PlantkgError(ErrorCode.PRECONDITION, "回测场景必须提供场景文件 path")
    scenario = load_scenario(path)
    seed = int(scenario_spec.get("seed", scenario.seed))
    scenario = scenario.model_copy(update={"seed": seed})  # 试算真正使用传入的 seed，基线缓存键里的 seed 才有意义
    scenario_sha256 = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    versions = registry.resolve(VersionStore)
    base = versions.get(candidate.base_manifest_id)
    base_refs = {_component_key(key).value: ComponentRef.model_validate(value) for key, value in base.components.items()}
    candidate_refs = base_refs | {_component_key(key).value: ComponentRef.model_validate(value)
                                  for key, value in candidate.changes.items()}
    fast = bool(scenario_spec.get("fast"))
    anchor = _candidate_anchor(versions, context.plant_id, candidate_refs, context)
    anchored = fast and scenario.scenario_id == "b32_fast_demo"
    if anchored:  # P1-B1：快演示场景锚定到候选生效时刻之后（演示日回退场景的日期与电价数据绑定，不平移）
        scenario = _anchored_fast_scenario(scenario, anchor)
    target_types, expected_levels, downgrades = _backtest_target_types(versions.writer, context.plant_id, base_refs,
                                                                      candidate_refs, scenario_spec,
                                                                      base_policy=_policy_for_components(
                                                                          versions.writer, context.plant_id, base_refs)
                                                                      or _default_policy_template(settings))
    warm_only = bool(scenario_spec.get("warm_only"))
    config = evolution_config(settings)
    window_end = _fast_window_end(scenario, target_types, config) if fast else scenario.end
    if anchored and window_end < anchor:  # 窗口终点不得早于候选 available_at
        window_end = min(scenario.end, anchor + timedelta(minutes=int(config["fast_tail_min"])))
    # 场景文件摘要进缓存键：场景内容改了，旧基准摘要不能复用（报告绑定的是当前文件）
    cache_key = "b32-baseline-v5|" + hashlib.sha1(
        "|".join((candidate.base_manifest_id, scenario.scenario_id,
                  str(seed), window_end.isoformat(), scenario_sha256)).encode("utf-8")).hexdigest()
    trial_store = EvolutionStore(versions.writer, context.plant_id)
    with tempfile.TemporaryDirectory(prefix="plantkg-b32-trial-") as root:
        copy_hooks = registry.hooks_for("evolution.copy_trial_components")
        if not copy_hooks:
            raise PlantkgError(ErrorCode.DEPENDENCY_UNAVAILABLE, "缺少候选产物隔离复制器")
        copy_components = copy_hooks[-1]
        reviewer = Actor(actor_id="b32-trial", roles=(Role.reviewer,))

        def run_side(name, candidate_changes):
            started = time.perf_counter()
            summary = _run_side(name, candidate_changes)
            summary["elapsed_s"] = round(time.perf_counter() - started, 3)
            return summary

        def _run_side(name, candidate_changes):
            writer = SQLiteWriter(Path(root) / f"{name}.db")
            try:
                MigrationRunner(writer).apply()
                trial_versions = SqliteVersionStore(writer, context.plant_id)
                for prefix in ("scenario-knowledge:", "scenario-policy:", "parameters:", "ontology:",
                               "rules:", "graph:v1:"):
                    trial_versions.register_verifier(prefix, lambda _component: "complete")
                for artifact_store in (policy_artifacts(writer, context.plant_id),
                                       skill_artifacts(writer, context.plant_id),
                                       sensor_artifacts(writer, context.plant_id)):
                    if not trial_versions.has_verifier(artifact_store.prefix):
                        trial_versions.register_verifier(artifact_store.prefix, artifact_store.verify)
                base_components = copy_components(dict(base.components), trial_versions)
                changes = copy_components(dict(candidate.changes), trial_versions, candidate_changes=candidate_changes)
                base_refs_trial = {_component_key(key).value: ComponentRef.model_validate(value)
                                   for key, value in base_components.items()}
                candidate_refs_trial = base_refs_trial | {_component_key(key).value: ComponentRef.model_validate(value)
                                                          for key, value in changes.items()}
                trial_base = trial_versions.prepare(base_refs_trial, "isolated backtest baseline")
                if trial_base.status == "prepared":
                    trial_base = trial_versions.check_integrity(trial_base.version_manifest_id)
                trial_base = trial_versions.activate(None, trial_base.version_manifest_id, reviewer, "isolated baseline")
                trial_candidate = trial_versions.prepare(candidate_refs_trial,
                                                         f"isolated candidate {candidate.candidate_id}")
                if trial_candidate.status == "prepared":
                    trial_candidate = trial_versions.check_integrity(trial_candidate.version_manifest_id)
                manifest_id = trial_base.version_manifest_id if name == "base" else trial_candidate.version_manifest_id
                if trial_versions.active().version_manifest_id != manifest_id:
                    trial_versions.activate(trial_base.version_manifest_id, manifest_id, reviewer,
                                            "isolated candidate switch")
                db = ScenarioDB.from_writer(writer, context.plant_id)
                runtime = default_registry(registry)
                runtime.register_hook("scenario.document_upload", _skip_trial_upload)
                runtime.register_hook("scenario.initial_manifest", lambda _runner, value=manifest_id: value)
                _use_trial_execution(runtime, context.plant_id)
                from plantkg.knowledge.rules.runtime import rules_for_manifest
                trial_artifacts = RuleArtifacts(writer, context.plant_id)
                runtime.register_hook('knowledge.b30.rules', lambda mid, at, as_of:
                                      rules_for_manifest(trial_versions, trial_artifacts, mid, at, as_of))
                matcher = _TrialRuleMatcher(trial_versions, trial_artifacts, manifest_id, db.runs)
                runtime.ports[RuleMatcher] = matcher
                if name == "candidate":
                    runtime.register_hook("sensors.config_overrides",
                                          lambda: _sensor_overrides(writer, context.plant_id, manifest_id))
                runner = ScenarioRunner(build_world(scenario), db, runtime, mode="trial", group="coordinated")
                policy = _policy_for_components(writer, context.plant_id,
                                                candidate_refs_trial if name == "candidate" else base_refs_trial)
                if policy is not None:
                    runner.policy, runner.coordinator.policy = policy, policy
                    runtime.register_hook(
                        "evolution.published_autonomy",
                        lambda category, value=policy: value.autonomy_levels.get(category)
                        or value.autonomy_levels.get(category.split("|", 1)[0]))
                def trial_policy_for_run(run_id, fallback):
                    run = db.runs.get_run(run_id)
                    if run.version_manifest_id != manifest_id:
                        return None
                    return policy or fallback
                runtime.register_hook('evolution.policy_for_run', trial_policy_for_run)
                def trial_autonomy_for_run(category, run_id, fallback):
                    bound = trial_policy_for_run(run_id, fallback)
                    if bound is None:
                        return 'L0'
                    return (bound.autonomy_levels.get(category)
                            or bound.autonomy_levels.get(category.split('|', 1)[0])
                            or (bound.autonomy_default_level if category == 'probe' else bound.autonomy_unseen_level))
                runtime.register_hook('evolution.autonomy_for_run', trial_autonomy_for_run)
                if name == 'candidate':
                    _, expected, _ = _backtest_target_types(
                        writer, context.plant_id, base_refs_trial, candidate_refs_trial, {},
                        base_policy=_default_policy_template(settings))
                    execution = runtime.hooks_for('router.execution')[-1].__self__
                    execution.ensure_store(writer, context.plant_id)
                    for category in expected:
                        execution.authorization_published(trial_base.version_manifest_id, manifest_id, category,
                                                          f'trial:{category}', scenario.start)
                runner.start()
                if runner.run_info.version_manifest_id != manifest_id:
                    raise PlantkgError(ErrorCode.INTERNAL, "trial runner did not bind requested manifest")
                return _trace_summary(runner.run_until(window_end), matcher, runtime)
            finally:
                writer.close()

        cached = trial_store.baseline(cache_key) if fast else None
        if warm_only:
            if cached is None:
                trial_store.put_baseline(cache_key, run_side("base", False))
            return {"warmed": True, "scenario_id": scenario.scenario_id, "cache_key": cache_key}
        base_summary = cached
        if base_summary is None:
            base_summary = run_side("base", False)
            if fast:
                trial_store.put_baseline(cache_key, base_summary)
        candidate_summary = run_side("candidate", True)
        if not base_summary.get("ledger_verified") or not candidate_summary.get("ledger_verified"):
            return {"failure": "scenario produced no balanced minute ledger", "executed": True,
                    "ledger_verified": False, "scope": "fast_window" if fast else "full"}
        target_events = sum(1 for event in base_summary["events"] if _target_matches(event, target_types))
        scenario_types = {event.type for event in scenario.expanded_events() if event.source in ("script", "sim_limit")}
        def _observable(target: str) -> bool:  # 类别目标（event_type|entity_type）看事件类型部分在不在场景里
            return _target_event_type(target) in scenario_types
        targets_unmeasurable = not target_types or not any(_observable(target) for target in target_types)
        # 事件类型的口径：router 的事件域、本场景出现过的事件类型、严重度配置里列出的类型
        severity = load_severity_config()
        known_event_types = (set(EVENT_DOMAINS) | {event.type for event in scenario.expanded_events()}
                             | {name for key, names in severity.items() if key.endswith("_event_types") or key == "rule_covered_events"
                                for name in (names or ())})
        combo_unmeasurable = False
        combo_required, combo_covered, eligible = set(), set(), None
        rule_ref = candidate_refs.get('rules')
        if ('rules' in {_component_key(key).value for key in candidate.changes} and rule_ref is not None
                and rule_ref.ref.startswith('b30-rules:')):
            artifacts = RuleArtifacts(versions.writer, context.plant_id)
            old_ref = base_refs.get('rules')
            old = {item['rule_id']: item for item in artifacts.load(old_ref)['rules']} if (
                old_ref is not None and old_ref.ref.startswith('b30-rules:')) else {}
            changed_combo = [Rule.model_validate(item) for item in artifacts.load(rule_ref)['rules']
                             if item.get('kind') == 'unknown_combo' and old.get(item['rule_id']) != item]
            if changed_combo:
                from plantkg.knowledge.rules.runtime import matches_combo_advice, matches_combo_rule
                combo_required = {rule.rule_id for rule in changed_combo}
                eligible = set()
                for event in candidate_summary['events']:
                    event_at = datetime.fromisoformat(event['at'])
                    for rule in changed_combo:
                        executable = matches_combo_rule(rule, event['event_type'], event['entity_id'], event['summary'])
                        advisory = matches_combo_advice(rule, event['event_type'], event['entity_id'], event['summary'])
                        if rule.is_visible(event_at) and rule.applies_to(event_at) and (executable or advisory):
                            eligible.add(event['event_id'])
                            if ((executable and event['matched_rule_id'] == rule.rule_id
                                 and rule.rule_id in event['action_rule_ids'])
                                    or (advisory and rule.rule_id in event['advice_rule_ids'])):
                                combo_covered.add(rule.rule_id)
                measurable = eligible
                if not measurable:
                    combo_unmeasurable = True
                    targets_unmeasurable = True
                    target_events = 0
                else:
                    target_events = len(eligible)
        base_metrics = _summary_metrics(base_summary, target_types, expected_levels, downgrades, eligible, combo_required)
        candidate_metrics = _summary_metrics(candidate_summary, target_types, expected_levels, downgrades, eligible, combo_required)
        policy_seen, policy_met, policy_failed, policy_undecided = set(), set(), set(), set()
        for category, expected in expected_levels.items():
            for event in candidate_summary['events']:
                same_category = (event['category'] == category if '|' in category else event['event_type'] == category)
                if (same_category and (event['event_type'] != 'unknown_combo'
                                      or eligible is None or event['event_id'] in eligible)):
                    policy_seen.add(category)
                    decisions = event.get('decision_levels') or ()
                    action_evidence = (event['event_type'] != 'unknown_combo' or not combo_required
                                       or bool(combo_required.intersection(event['action_rule_ids'])))
                    if expected in decisions and action_evidence:
                        policy_met.add(category)
                    if not decisions or not action_evidence:
                        policy_undecided.add(category)
                    if any(level != expected for level in decisions):
                        policy_failed.add(category)
        return {"scenario_id": scenario.scenario_id, "executed": True,
                "ledger_verified": True,
                "scope": "fast_window" if fast else "full",
                "window_end": window_end.isoformat(),
                "baseline_reused": bool(cached is not None),
                "path": _repo_relative(path), "sha256": scenario_sha256, "seed": seed,
                "window_minutes": (window_end - scenario.start).total_seconds() / 60,
                "base_elapsed_s": float(base_summary.get("elapsed_s", 0.0)),
                "candidate_elapsed_s": float(candidate_summary.get("elapsed_s", 0.0)),
                "policy_targets": bool(expected_levels),
                "policy_required": sorted(expected_levels), "policy_seen": sorted(policy_seen),
                "policy_met": sorted(policy_met),
                "policy_failed": sorted(policy_failed), "policy_undecided": sorted(policy_undecided),
                "policy_downgrades": sorted(downgrades),
                "targets_unmeasurable": targets_unmeasurable,
                "targets_event_like": any(_target_event_type(target) in known_event_types for target in target_types),
                "combo_unmeasurable": combo_unmeasurable,
                "combo_required": sorted(combo_required), "combo_covered": sorted(combo_covered),
                "target_events": target_events,
                "rule_coverage": {"base": float(sum(1 for event in base_summary["events"] if event["hit"])),
                                  "candidate": float(sum(1 for event in candidate_summary["events"] if event["hit"]))},
                "deterministic_fallbacks": base_summary["deterministic_fallbacks"] + candidate_summary["deterministic_fallbacks"],
                "base_deterministic_fallbacks": base_summary["deterministic_fallbacks"],
                "candidate_deterministic_fallbacks": candidate_summary["deterministic_fallbacks"],
                "metrics": {key: {"base": base_metrics[key], "candidate": candidate_metrics[key]} for key in base_metrics},
                "constraint_violations": {"base": int(base_metrics["capacity_violation_minutes"]),
                                          "candidate": int(candidate_metrics["capacity_violation_minutes"])},
                "ledger": {"base_cost_cny": base_summary["cost_cny"],
                           "candidate_cost_cny": candidate_summary["cost_cny"]}}


def _repo_relative(path) -> str:
    """场景路径在仓库根下时存相对路径（回测报告随仓库迁移不失效），否则存原路径；发布重校验按仓库根解析。"""
    try:
        return Path(path).resolve().relative_to(Path(__file__).resolve().parents[1]).as_posix()
    except ValueError:
        return str(path)


def _target_event_type(target: str) -> str:
    """目标类里的 B47 类别（event_type|entity_type）取事件类型部分；纯事件类型原样返回。"""
    return target.split("|", 1)[0] if "|" in target else target


def _default_policy_template(settings) -> Any:
    """无 policy 组件时的统一基线：evolution.default_policy 模板（与 B32 候选侧 _current_policy 同口径）。"""
    from plantkg.core.contracts import PolicyConfig, ResponseBudget
    from plantkg.evolution.service import evolution_config

    template = evolution_config(settings)["default_policy"]
    return PolicyConfig.model_validate({
        "policy_version": "b32-default-template",
        "arbitration_weights": {"cny": 1.0, "wait_min": 0.5, "comfort_min": 0.8},
        "priority_profiles": {"default": ("safety", "production", "comfort", "cost")},
        "response_budget": ResponseBudget(layer12_min=1, layer3_min=3), **template})


def _candidate_anchor(versions, plant_id, candidate_refs, context) -> Any:
    """候选生效时刻：新增/变更规则的最大 available_at；没有规则时用上下文 as_of。"""
    from plantkg.core.contracts import Rule
    from plantkg.knowledge.rules.publisher import RuleArtifacts

    ref = candidate_refs.get("rules")
    times = []
    if ref is not None and str(ref.ref).startswith("b30-rules:"):
        body = RuleArtifacts(versions.writer, plant_id).load(ref)
        times = [Rule.model_validate(rule).available_at for rule in body["rules"]]
    return max(times) if times else context.as_of


def _anchored_fast_scenario(scenario, anchor) -> Any:
    """把快场景整体平移到候选生效时刻之后（按整天平移，事件相对时刻不变），窗口才可能测到生效后的规则。"""
    events = scenario.expanded_events()
    if not events or anchor is None:
        return scenario
    first = min(scenario.instant(event.at) for event in events)
    days = max(0, (anchor.astimezone(scenario.tz).date()
                   - scenario.start.astimezone(scenario.tz).date()).days)
    if scenario.start + timedelta(days=days) + (first - scenario.start) < anchor:
        days += 1
    if days == 0:
        return scenario
    new_date = (scenario.start + timedelta(days=days)).astimezone(scenario.tz).date().isoformat()
    return scenario.model_copy(update={"date": new_date})


def _fast_scenarios(registry, candidate, settings=None):
    """演示发布用快回测场景：只覆盖常见事件类型；目标类不在快场景里时回退演示日（仍按窗口截断）。"""
    from plantkg.core.contracts import ComponentRef
    from plantkg.core.ports import VersionStore
    from plantkg.world.scenario.script import load_scenario

    versions = registry.resolve(VersionStore)
    base = versions.get(candidate.base_manifest_id)
    base_refs = {_component_key(key).value: ComponentRef.model_validate(value) for key, value in base.components.items()}
    candidate_refs = base_refs | {_component_key(key).value: ComponentRef.model_validate(value)
                                  for key, value in candidate.changes.items()}
    targets, _, _ = _backtest_target_types(versions.writer, versions.plant_id, base_refs, candidate_refs, {},
                                           base_policy=_default_policy_template(settings))
    if FAST_SCENARIO.is_file():
        scenario = load_scenario(FAST_SCENARIO)
        available = {event.type for event in scenario.expanded_events()}
        target_event_types = {_target_event_type(target) for target in targets}
        if not targets or target_event_types <= available:
            return ({"scenario_id": "b32-fast-demo", "seed": 32, "path": str(FAST_SCENARIO), "fast": True},)
    demo = Path(__file__).resolve().parents[1] / "configs" / "scenarios" / "factory_demo_day.yaml"
    return ({"scenario_id": "factory_demo_day", "seed": 32, "path": str(demo), "fast": True},)


def _warm_fast_baselines(registry, context, base_manifest_id=None, candidate=None, settings=None):
    """候选生成/规则上传时预生成基准摘要并持久化；发布时只跑候选一侧。

    窗口依赖候选的目标类事件，只有候选确定后才算得出来；B30 规则上传时传 candidate=None，
    只按快场景的固定窗口预热（快场景窗口与目标类无关）。
    """
    from types import SimpleNamespace

    shim = candidate if candidate is not None else SimpleNamespace(candidate_id="b32-prewarm",
                                                                  base_manifest_id=base_manifest_id, changes={})
    if candidate is not None:
        providers = registry.hooks_for("evolution.fast_scenarios")
        if not providers:
            return
        try:
            specs = providers[-1](candidate, context) or ()
        except Exception:  # noqa: BLE001 — 预热失败不挡候选生成，发布时仍会现跑基准
            return
    else:
        if not base_manifest_id or not FAST_SCENARIO.is_file():
            return
        # 目标类不在快场景里的候选（电价等）发布时回退演示日 + 固定短窗口；窗口只由 fast_window_min 决定，上传时一并预热才命中缓存
        demo = Path(__file__).resolve().parents[1] / "configs" / "scenarios" / "factory_demo_day.yaml"
        specs = ({"scenario_id": "b32-fast-demo", "seed": 32, "path": str(FAST_SCENARIO), "fast": True},
                 {"scenario_id": "factory_demo_day", "seed": 32, "path": str(demo), "fast": True})
    for spec in specs:
        try:
            _scenario_backtest(registry, shim, dict(spec) | {"fast": True, "warm_only": True}, context, settings)
        except Exception:  # noqa: BLE001 — 同上
            continue


def _fast_window_end(scenario, target_types, config) -> Any:
    """快回测窗口终点：快场景统一取最后一个剧本事件 + 收尾余量（便于基准随版本预生成）；其余场景取最后一个目标类事件 + 余量。"""
    tail = timedelta(minutes=int(config["fast_tail_min"]))
    if scenario.scenario_id == "b32_fast_demo":
        last = max((scenario.instant(event.at) for event in scenario.expanded_events()), default=scenario.start)
        end = last + tail
        return min(scenario.end, max(scenario.start + timedelta(minutes=1), end))
    target_event_types = {_target_event_type(target) for target in target_types}
    relevant = [scenario.instant(event.at) for event in scenario.expanded_events() if event.type in target_event_types]
    if relevant:
        end = max(relevant) + tail
    else:
        end = scenario.start + timedelta(minutes=int(config["fast_window_min"]))
    return min(scenario.end, max(scenario.start + timedelta(minutes=1), end))


def _trace_summary(trace, matcher, registry) -> dict[str, Any]:
    """把一次试算压成可缓存的基准摘要：事件数组 + 账本与回退计数，目标类指标按候选现算。"""
    summary = trace.ledger_summary()
    ticket_of: dict[str, str] = {}
    for ticket_id, record in trace.tickets.items():
        for event_id in record.get("event_ids", ()):
            ticket_of[event_id] = ticket_id
    admissions = {note.get("event_id"): note.get("channel") for note in trace.notes if note.get("kind") == "admission"}
    levels: dict[str, list[str]] = {}
    for note in trace.notes:
        if note.get('kind') == 'b47_execution' and note.get('ticket_id') and note.get('level'):
            levels.setdefault(note['ticket_id'], []).append(note['level'])
    advice_by_event: dict[str, set[str]] = {}
    actions_by_event: dict[str, set[str]] = {}
    for proposal in trace.proposals:
        binding = proposal.binding
        ticket = trace.tickets.get(proposal.ticket_id) or {}
        event_ids = ticket.get('event_ids') or ()
        if proposal.expert != 'equipment' or binding is None or len(event_ids) != 1:
            continue
        event = next((item for item in trace.events if item.event_id == event_ids[0]), None)
        if event is None or binding.ticket_id != proposal.ticket_id or binding.trigger_at != event.at:
            continue
        run = next((entry for entry in reversed(trace.runs)
                    if entry.forked_at is None or entry.forked_at <= event.at), None)
        if run is None or binding.run_id != run.run_id or binding.knowledge_version != run.knowledge_version:
            continue
        if binding.round != ticket.get('round'):
            continue
        for ref in proposal.evidence_refs:
            if ref.startswith('rule:'):
                rule_id = ref.removeprefix('rule:')
                if not proposal.actions:
                    advice_by_event.setdefault(event.event_id, set()).add(rule_id)
                else:
                    matched = matcher.matches.get(event.event_id)
                    allowed = matched.condition.get('actions', ()) if matched and matched.rule_id == rule_id else ()
                    if allowed and all(not action.params and any(
                            item['entity_id'] == action.entity_id and item['command'] == action.command
                            for item in allowed) for action in proposal.actions):
                        actions_by_event.setdefault(event.event_id, set()).add(rule_id)
    events = []
    for event in trace.events:
        if event.source not in ("script", "sim_limit"):
            continue
        ticket_id = ticket_of.get(event.event_id)
        ticket = trace.tickets.get(ticket_id) if ticket_id else None
        channel = admissions.get(event.event_id)
        hit = event.event_id in matcher.hits
        rule_action = matcher.action_for(event.event_id)
        if not hit and channel in ("slow", "urgent"):
            # 读数可疑等设计上走慢通道、困人等走紧急通道的事件不经规则命中路径：按已发布规则是否覆盖该目标类计入目标指标（#143/#144）
            hit = matcher.rules_cover(event)
            rule_action = matcher.action_for(event.event_id) if hit else None
        matched_rule = matcher.matches.get(event.event_id)
        decisions = (levels.get(ticket_id) or []) if ticket and len(ticket.get('event_ids') or ()) == 1 else []
        events.append({"event_id": event.event_id, "event_type": event.event_type, "at": event.at.isoformat(),
                       "matched_rule_id": matched_rule.rule_id if matched_rule is not None else None,
                       "advice_rule_ids": sorted(advice_by_event.get(event.event_id, set())),
                       "action_rule_ids": sorted(actions_by_event.get(event.event_id, set())),
                       "entity_id": event.entity_id, "summary": event.summary,
                       "category": _event_category(registry, event),
                       "hit": hit, "rule_action": rule_action,
                       "resolved": bool(ticket and ticket.get("status") == "resolved"),
                       "channel": channel, "level": (decisions or [None])[-1],
                       "decision_levels": decisions,
                       "ticket_id": ticket_id})
    return {"ledger_verified": all(row.balanced for row in trace.ledger),
            "deterministic_fallbacks": int(sum(int(bool(record.coverage_gap)) for record in trace.agent_runs)
                                           + sum(int(bool(note.get("coverage_gap"))) for note in trace.notes)),
            "cost_cny": float(summary["cost_cny"] or 0.0),
            "capacity_violation_minutes": float(summary["capacity_violation_minutes"]),
            "events": events}


def _event_category(registry, event) -> str:
    """事件类别 = `event_type|entity_type`（B47 的分类口径）；图谱认不出实体时退回纯事件类型。"""
    entity_type = None
    for hook in reversed(registry.hooks_for("router.entity_type")):
        try:
            entity_type = hook(event.entity_id)
        except Exception:  # noqa: BLE001
            continue
        if entity_type:
            break
    return f"{event.event_type}|{entity_type}" if entity_type else event.event_type


def _target_matches(event: Mapping[str, Any], target_types: set[str]) -> bool:
    """目标类既可能是事件类型（规则类别），也可能是 B47 的类别（event_type|entity_type）。"""
    return event["event_type"] in target_types or event.get("category") in target_types


# 各专家域可直接执行的动作命令（configured）；人类处置描述不按命令判域
_EVENT_DOMAIN_COMMANDS = {
    "energy": frozenset({"charge", "discharge", "idle", "set_mode", "set_setpoint", "shed", "stop", "log_maintenance"}),
    "hvac": frozenset({"set_mode", "set_setpoint", "precool", "stop", "ventilate", "log_maintenance"}),
    "equipment": frozenset({"isolate_leak", "repair", "stop", "set_mode", "ventilate", "log_maintenance"}),
    "elevator": frozenset({"set_mode", "ventilate", "broadcast", "stop", "log_maintenance"}),
}


def _rule_action_compatible(action: Any, event_type: str) -> bool:
    """规则声明的动作是否属于该事件可执行集合；人类处置描述（非命令词）不判域。"""
    from plantkg.agent.router.router import EVENT_DOMAINS

    if action in (None, ""):
        return True
    text = str(action).strip()
    known = {command for commands in _EVENT_DOMAIN_COMMANDS.values() for command in commands}
    if text not in known:
        return True
    domains = EVENT_DOMAINS.get(event_type) or ()
    commands = {command for domain in domains for command in _EVENT_DOMAIN_COMMANDS.get(domain, ())}
    return text in commands


def _summary_metrics(summary: Mapping[str, Any], target_types: set[str],
                     expected_levels: Mapping[str, str],
                     downgrade_targets: frozenset[str] | set[str] = frozenset(),
                     eligible_events: set[str] | None = None,
                     eligible_rules: set[str] | None = None) -> dict[str, float]:
    from plantkg.agent.router.severity import load_severity_config

    safety_types = set(load_severity_config().get("safety_event_types", ()))

    def _downgrade_event(event: Mapping[str, Any]) -> bool:
        # P1：降级 policy 的目标类别事件本来就会让“自动解决/快通道”变少，不计入退化比较
        return event["event_type"] in downgrade_targets or event.get("category") in downgrade_targets

    events = summary["events"]
    target = set()
    for event in events:
        if (not _target_matches(event, target_types)
                or (event['event_type'] == 'unknown_combo' and eligible_events is not None
                    and event['event_id'] not in eligible_events)):
            continue
        expected = expected_levels.get(event.get("category")) or expected_levels.get(event["event_type"])
        action_evidence = (event['event_type'] != 'unknown_combo' or not eligible_rules
                           or bool(eligible_rules.intersection(event.get('action_rule_ids') or ())))
        if expected is not None and event["level"] == expected and action_evidence:
            target.add(event["event_id"])
        elif (expected is None and event["hit"] and action_evidence
              and _rule_action_compatible(event.get("rule_action"), event["event_type"])
              and (event["event_type"] not in safety_types or event.get("ticket_id"))):
            # 安全类事件（困人等）按处置结果命中：候选规则覆盖该类且事件已转工单才计入
            target.add(event["event_id"])
        elif expected is None and eligible_rules and eligible_rules.intersection(event.get('advice_rule_ids') or ()):
            target.add(event['event_id'])
    return {"target": float(len(target)),
            "resolved": float(sum(1 for event in events if event["resolved"] and not _downgrade_event(event))),
            "safety": float(sum(1 for event in events
                                if event["event_type"] in safety_types and event["channel"] == "urgent")),
            "fast": float(sum(1 for event in events if event["channel"] == "fast" and not _downgrade_event(event))),
            "capacity_violation_minutes": summary["capacity_violation_minutes"],
            "cost_cny": summary["cost_cny"]}


def _component_key(key):
    from plantkg.core.contracts import ManifestComponent
    return key if isinstance(key, ManifestComponent) else ManifestComponent(key)


def _sensor_overrides(writer, plant_id, manifest_id):
    """D59：试算清单里的传感器案例，叠加到观测层故障特征矩阵。"""
    from plantkg.core.contracts import ManifestComponent
    from plantkg.core.versions import SqliteVersionStore
    from plantkg.evolution.artifacts import sensor_artifacts

    try:
        manifest = SqliteVersionStore(writer, plant_id).get(manifest_id)
    except Exception:  # noqa: BLE001
        return ()
    ref = manifest.components.get(ManifestComponent.sensor_cases)
    if ref is None:  # 兼容旧清单：D59 案例曾放在 parameters 槽
        legacy = manifest.components.get(ManifestComponent.parameters)
        if legacy is None or not str(legacy.ref).startswith("b32-sensor:"):
            return ()
        body = sensor_artifacts(writer, plant_id).load(legacy)
        return tuple(body["cases"]) if body.get("kind") == "sensor_cases" else (body,)
    return tuple(sensor_artifacts(writer, plant_id).load(ref).get("cases") or ())


def _skip_trial_upload(runner, at, payload):
    """隔离回测不执行资料入库：不写资产目录、不生成候选，只留一条说明。"""
    runner.note("knowledge_pending", at, {"reason": "隔离回测不执行资料入库", "document": payload.get("document", "")})


def _use_trial_execution(runtime, plant_id):
    """分级执行与值守钩子全部改成试算库：主装配的执行服务绑正式库，回测不能借它生成正式工单。"""
    from plantkg.agent.execution_policy import service as execution

    local = execution.ExecutionPolicyService(runtime, None, plant_id=plant_id)

    def is_execution_hook(hook):
        return isinstance(getattr(hook, "__self__", None), execution.ExecutionPolicyService)

    for name, method in ((execution.URGENT_HOOK, "on_urgent"), (execution.STEP_HOOK, "step"),
                         (execution.PROBE_HOOK, "on_probe"), (execution.INSTRUCTION_HOOK, "on_instruction"),
                         (execution.EXECUTION_HOOK, "execute"), (execution.FORK_HOOK, "on_fork"),
                         (execution.SCENARIO_CONFIRM_HOOK, "confirm_from_scenario"),
                         (execution.SCENARIO_BIND_HOOK, "bind")):
        kept = [hook for hook in runtime.hooks.get(name, ()) if not is_execution_hook(hook)]
        runtime.hooks[name] = kept + [getattr(local, method)]


class _TrialRuleMatcher:
    """RuleMatcher：候选清单里的已发布规则进快通道判定；未覆盖的类型沿用 B27 的静态白名单兜底。

    记录实际命中规则的事件，供回测计算“目标类是否真的走到正式决策链”。规则按事件时刻判可见与生效。"""

    def __init__(self, versions, artifacts, manifest_id, runs=None):
        from plantkg.agent.router.severity import EventTypeRuleMatcher, load_severity_config
        from plantkg.knowledge.rules.runtime import all_rules_for_manifest

        self.versions, self.artifacts, self.manifest_id = versions, artifacts, manifest_id
        self.runs = runs
        self._fallback = EventTypeRuleMatcher(load_severity_config())
        self._rules = tuple(rule for rule in all_rules_for_manifest(versions, artifacts, manifest_id) if rule.compilable)
        self.hits: dict[str, str] = {}
        self.matches: dict[str, Any] = {}

    def covers(self, event) -> bool:
        return self.rules_cover(event) or self._fallback.covers(event)

    def covers_for_run(self, event, run_id) -> bool:
        if self.runs is None or self.runs.get_run(run_id).version_manifest_id != self.manifest_id:
            return False
        return self.covers(event)

    def authorizes_for_run(self, event, run_id, actions=()) -> bool:
        if self.runs is None or self.runs.get_run(run_id).version_manifest_id != self.manifest_id:
            return False
        from plantkg.knowledge.rules.runtime import matches_combo_rule
        return any(rule.is_visible(event.at) and rule.applies_to(event.at)
                   and matches_combo_rule(rule, event.event_type, event.entity_id, event.summary)
                   and bool(actions) and all(not action.params and any(
                       item['entity_id'] == action.entity_id and item['command'] == action.command
                       for item in rule.condition['actions']) for action in actions) for rule in self._rules)

    def rules_cover(self, event) -> bool:
        """只按已发布规则判断覆盖（不含 B27 静态白名单）：慢通道事件是否落在已发布规则的消费范围。"""
        for rule in self._rules:
            if ((rule.kind == event.event_type or str(rule.condition.get("event_type") or "") == event.event_type)
                    and rule.is_visible(event.at) and rule.applies_to(event.at)):
                if event.event_type == 'unknown_combo':
                    from plantkg.knowledge.rules.runtime import matches_combo_rule
                    if not matches_combo_rule(rule, event.event_type, event.entity_id, event.summary):
                        continue
                self.hits[event.event_id] = event.event_type
                self.matches[event.event_id] = rule
                return True
        return False

    def action_for(self, event_id: str) -> Any:
        rule = self.matches.get(event_id)
        return rule.condition.get("action") if rule is not None else None


def _backtest_target_types(writer, plant_id, base_refs, candidate_refs, scenario_spec,
                           base_policy=None) -> tuple[set[str], dict[str, str]]:
    """目标类：候选相对基准新增/改动的规则类别、自主等级变更的类别，或场景显式 targets。

    返回（目标类别，policy 候选期望的自主等级）；policy 目标按“该等级真的出现在执行决策里”判定生效。"""
    from plantkg.core.contracts import PolicyConfig, Rule
    from plantkg.evolution.artifacts import policy_artifacts
    from plantkg.knowledge.rules.publisher import RuleArtifacts

    targets = {str(item) for item in (scenario_spec.get("targets") or ())}
    expected: dict[str, str] = {}
    downgrades: set[str] = set()
    artifacts = RuleArtifacts(writer, plant_id)
    rules_ref, base_rules_ref = candidate_refs.get("rules"), base_refs.get("rules")
    if rules_ref is not None and rules_ref != base_rules_ref and str(rules_ref.ref).startswith("b30-rules:"):
        base_rules = {}
        if base_rules_ref is not None and str(base_rules_ref.ref).startswith("b30-rules:"):
            base_rules = {(rule.kind, rule.subject_ids): rule
                          for rule in (Rule.model_validate(item) for item in artifacts.load(base_rules_ref)["rules"])}
        for rule in (Rule.model_validate(item) for item in artifacts.load(rules_ref)["rules"]):
            if base_rules.get((rule.kind, rule.subject_ids)) != rule:
                targets.add(rule.kind)
                event_type = str(rule.condition.get("event_type") or "")
                if event_type:
                    targets.add(event_type)
    policy_ref = candidate_refs.get("policy")
    if policy_ref is not None and str(policy_ref.ref).startswith("policy:"):
        store = policy_artifacts(writer, plant_id)
        candidate_policy = PolicyConfig.model_validate(store.load(policy_ref))
        base_ref = base_refs.get("policy")
        base_levels = {}
        if base_ref is not None and str(base_ref.ref).startswith("policy:"):
            base_levels = PolicyConfig.model_validate(store.load(base_ref)).autonomy_levels
        elif base_policy is not None:
            base_levels = base_policy.autonomy_levels
        for key, level in candidate_policy.autonomy_levels.items():
            if base_levels.get(key) != level:
                targets.add(key)
                expected[key] = level
                base_level = base_levels.get(key) or base_levels.get(key.split("|", 1)[0])
                if base_level is not None and _level_rank(level) < _level_rank(base_level):
                    downgrades.add(key)  # P1：等级下降的类别，resolved/fast 减少不算退化
    return targets, expected, downgrades


_AUTONOMY_ORDER = ("L0", "L1", "L2", "L3")


def _level_rank(level: str) -> int:
    return _AUTONOMY_ORDER.index(level) if level in _AUTONOMY_ORDER else 0




def _policy_for_components(writer, plant_id, components):
    from plantkg.core.contracts import PolicyConfig
    from plantkg.evolution.artifacts import policy_artifacts

    ref = components.get("policy")
    if ref is None or not str(ref.ref).startswith("policy:"):
        return None
    return PolicyConfig.model_validate(policy_artifacts(writer, plant_id).load(ref))


def check_tool_catalog(registry: Registry, *, require_all: bool = False) -> None:
    """启动自检（#160）：运行时 exposed_to_agent 工具必须在契约目录；契约里非可选工具必须已登记。

    扩展工具只能经契约 `TOOL_SPECS` 声明，运行时多出暴露工具或契约少了实现都会在装配阶段失败，
    不再等人工发现“契约目录缺工具”。``require_all=True`` 时才做反向检查（契约工具都要有实现），
    用于完整默认装配的测试；部分装配（评测、自定义 packs 子集、未启用存储）不按缺实现处理。
    """
    from plantkg.core.contracts import TOOL_SPECS

    contract = {spec.name: spec for spec in TOOL_SPECS}
    extra = sorted(name for name, binding in registry.tools.items()
                   if binding.spec.exposed_to_agent and name not in contract)
    if extra:
        raise RegistryError(f"运行时暴露工具未在契约 TOOL_SPECS 中声明：{extra}")
    if not require_all:
        return
    missing = sorted(name for name, spec in contract.items()
                     if spec.exposed_to_agent and not spec.optional and name not in registry.tools)
    if missing:
        raise RegistryError(f"契约工具未在本次装配中登记且未标 optional：{missing}")


def create_application(settings: Settings, packs_dir: Path | None = None, *, storage_resolver=None) -> Starlette:
    from plantkg.api import create_app
    from plantkg.mcp_server import create_mcp_server

    services = build_services(settings, packs_dir, storage_resolver=storage_resolver)
    try:
        return create_app(services, create_mcp_server(services))
    except BaseException:
        services.close()
        raise
