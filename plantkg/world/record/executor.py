"""Internal replay record executor. Unsupported action types fail closed."""
import hashlib
import math
from datetime import datetime, timedelta

from plantkg.core.contracts import (ErrorCode, ExecutedAction, ExecutionResult, Interval, PlantkgError,
                                    Rule, StorageParams)
from plantkg.core.ledger import LedgerMeta, SlotInput, compute_ledger, validate_balance
from plantkg.core.versions import atomic, digest
from plantkg.world.replay.series import online_record_hash
from .pv import apply_pv_cap
from .operation import shift_slot
from .repository import ExecutionRepository
from .scenario import ScenarioInputs


class RecordExecutor:
    """Execute one replay slot, recording physical results without controlling devices.

    No public tool or endpoint should expose this object before source and
    projection isolation have been reviewed. Production configuration remains
    fail-closed without a server-provided approved execution snapshot.
    """

    def __init__(self, writer, clock, gate, series, runs):
        self.writer, self.clock, self.gate, self.series, self.runs = writer, clock, gate, series, runs
        self.repository = ExecutionRepository(writer, clock, gate)
        self.scenario = ScenarioInputs(clock, series.series, runs)
        clock.projection_reader = self.repository.visible_projection

    def execute(self, confirmed_plan, context, expected_seq, idempotency_key):
        if not isinstance(idempotency_key, str) or not idempotency_key.strip() or len(idempotency_key) > 200:
            raise PlantkgError(ErrorCode.INVALID_REQUEST, "需要幂等键")
        if any(not math.isfinite(action.value) for action in confirmed_plan.actions):
            raise PlantkgError(ErrorCode.DOMAIN_INVALID, "计划动作必须为有限数")
        request_hash = digest({"context": context.model_dump(mode="json"), "expected_seq": expected_seq,
                               "plan": confirmed_plan.model_dump(mode="json")})
        prior = self.repository.find_retry(context, idempotency_key, request_hash)
        if prior is not None:
            return prior
        try:
            return self._execute_new(confirmed_plan, context, expected_seq, idempotency_key, request_hash)
        except PlantkgError:
            # Another identical request may have committed while this caller
            # was preparing data. Re-read the durable result before failing.
            prior = self.repository.find_retry(context, idempotency_key, request_hash)
            if prior is not None:
                return prior
            raise

    def _execute_new(self, confirmed_plan, context, expected_seq, idempotency_key, request_hash):
        approved = self.gate.executable(confirmed_plan.plan_id, confirmed_plan.revision)
        if approved != confirmed_plan:
            raise PlantkgError(ErrorCode.STATE_CONFLICT, "计划内容与批准版本不一致")
        run, seq, current = self.clock.current(context)
        if run.kind not in ("replay_day", "replay_week"):
            raise PlantkgError(ErrorCode.PRECONDITION, "仅支持历史回放运行")
        if type(expected_seq) is not int or seq != expected_seq:
            raise PlantkgError(ErrorCode.STATE_CONFLICT, "回放版本冲突")
        end = current + timedelta(minutes=15)
        slot = Interval(start=current, end=end)
        def get_basis(con):
            return self.gate.require_execution_snapshot_in_transaction(con, approved.plan_id, approved.revision,
                                                                        run.run_id, run.version_manifest_id, current)
        basis, basis_hash = atomic(self.writer, get_basis)
        fixed = self.runs.run_inputs(run.run_id)["inputs"]
        mode = fixed.get("execution_input_mode")
        quantities = fixed.get("execution_quantities")
        if mode not in ("online_observed", "offline_scenario") or not isinstance(quantities, dict):
            raise PlantkgError(ErrorCode.PRECONDITION, "运行缺少固定执行输入模式和量纲")
        if mode == "offline_scenario" and context.channel != "offline":
            raise PlantkgError(ErrorCode.FORBIDDEN, "事后场景仅可由可信离线入口执行")
        if set(quantities) != {"load", "pv"} or not all(isinstance(v, str) and v for v in quantities.values()):
            raise PlantkgError(ErrorCode.PRECONDITION, "运行缺少固定的负荷和光伏来源")
        def fetch(quantity):
            if mode == "online_observed":
                return self.series.candidate_observed(context, quantity, slot, expected_seq)
            return self.scenario.candidate(context, quantity, slot, expected_seq)
        load_bundle, pv_bundle = fetch(quantities["load"]), fetch(quantities["pv"])
        load_record, pv_record = load_bundle.records[0], pv_bundle.records[0]
        if load_record.unit != "kW" or pv_record.unit != "kW":
            raise PlantkgError(ErrorCode.DOMAIN_INVALID, "执行输入必须为 kW")
        actual_load, operation_states = shift_slot(approved, current, load_record.value, basis["config"]["operations"])
        actions = [a for a in approved.actions if a.interval.start <= current and a.interval.end >= end]
        if any(a.quantity not in ("pv_use", "pv_limit", "pv_curtail", "charge_power", "discharge_power", "grid_export", "operation_start", "operation_power") for a in actions):
            raise PlantkgError(ErrorCode.DEPENDENCY_UNAVAILABLE, "存在尚未支持的执行动作")
        values = {}
        for action in actions:
            if action.quantity in ("operation_start", "operation_power"):
                continue
            if action.unit != "kW" or action.quantity in values or action.value < 0:
                raise PlantkgError(ErrorCode.DOMAIN_INVALID, "动作单位、数量或数值无效")
            values[action.quantity] = action
        def amount(name):
            return values[name].value if name in values else 0.0
        config = basis["config"]
        pv_config = config.get("pv")
        if not pv_config or pv_record.value > pv_config["capacity_kw"]:
            raise PlantkgError(ErrorCode.PRECONDITION, "缺少有效光伏装机或实际值超过装机")
        if any(values[name].object != pv_config["entity_id"] for name in ("pv_use", "pv_limit", "pv_curtail") if name in values):
            raise PlantkgError(ErrorCode.PRECONDITION, "光伏动作与批准设备不匹配")
        rules = tuple(Rule.model_validate(r) for r in basis["rules"])
        limited = apply_pv_cap(pv_record.value, pv_config["entity_id"], current, rules, complete=True)
        planned_charge, planned_discharge, export = amount("charge_power"), amount("discharge_power"), amount("grid_export")
        charge, discharge = planned_charge, planned_discharge
        if charge and discharge:
            raise PlantkgError(ErrorCode.DOMAIN_INVALID, "不允许同时充放")
        if export > 0 and config["export"]["limit_kw"] <= 0:
            raise PlantkgError(ErrorCode.DOMAIN_INVALID, "本期不外送：外送上限为 0，拒绝外送动作")
        if export > config["export"]["limit_kw"]:
            raise PlantkgError(ErrorCode.DOMAIN_INVALID, "外送超过批准上限")
        if "grid_export" in values and values["grid_export"].object != config["export"].get("entity_id"):
            raise PlantkgError(ErrorCode.PRECONDITION, "外送动作与批准并网点不匹配")
        battery = config.get("storage")
        previous = self.repository.last_projection(context, seq)
        if previous is None and current != approved.valid_interval.start:
            raise PlantkgError(ErrorCode.STATE_CONFLICT, "不能跳过计划内的先前时段")
        if battery:
            if any(values[name].object != battery.get("entity_id") for name in ("charge_power", "discharge_power") if name in values):
                raise PlantkgError(ErrorCode.PRECONDITION, "储能动作与批准设备不匹配")
            storage = StorageParams(**{k: battery[k] for k in ("capacity_kwh", "eta_c", "eta_d", "soc_min", "soc_max")})
            if charge > battery["charge_kw"] or discharge > battery["discharge_kw"]:
                raise PlantkgError(ErrorCode.DOMAIN_INVALID, "储能动作超过功率上限")
            if previous is None:
                snap = basis.get("planning_snapshot") or {}
                energy = snap.get("pack_states", {}).get("storage", {}).get("energy_kwh")
                planned_energy = energy
            else:
                energy = previous.get("storage", {}).get("energy_kwh")
                planned_energy = previous.get("storage", {}).get("planned_energy_kwh")
            if (not isinstance(energy, (int, float)) or isinstance(energy, bool)
                    or not math.isfinite(energy) or not isinstance(planned_energy, (int, float))
                    or isinstance(planned_energy, bool) or not math.isfinite(planned_energy)):
                raise PlantkgError(ErrorCode.PRECONDITION, "缺少固定的储能初始状态")
            lower = storage.capacity_kwh * storage.soc_min
            upper = storage.capacity_kwh * storage.soc_max
            if not lower <= energy <= upper:
                raise PlantkgError(ErrorCode.DOMAIN_INVALID, "实际储能起点超出批准范围")
            charge = min(charge, max(upper - energy, 0.0) / (storage.eta_c * .25))
            reserve = lower + battery["reserve_ratio"] * (upper - lower)
            discharge = min(discharge, max(energy - reserve, 0.0) * storage.eta_d / .25)
        else:
            if charge or discharge:
                raise PlantkgError(ErrorCode.PRECONDITION, "储能动作缺少固定配置")
            storage, energy = None, 0.0
        # Export only what on-site supply actually leaves over; never buy from the grid to export.
        export = min(export, max(limited.usable_kw + discharge - actual_load - charge, 0.0))
        excess = max(limited.usable_kw + discharge - actual_load - charge - export, 0.0)
        curtailed = min(excess, limited.usable_kw)
        actual_discharge = discharge - max(excess - curtailed, 0.0)
        tariff = [s for s in config["tariff"]["slots"] if datetime.fromisoformat(s["start"]) <= current
                  and datetime.fromisoformat(s["end"]) >= end]
        if len(tariff) != 1:
            raise PlantkgError(ErrorCode.PRECONDITION, "当前槽缺少唯一已批准电价")
        meta = LedgerMeta("exec-" + hashlib.sha256(f"{run.run_id}|{seq}".encode()).hexdigest()[:32],
                          "execution", run.run_id, approved.plan_id, approved.revision, run.version_manifest_id,
                          (f"{load_bundle.bundle_id}:{online_record_hash(load_record)}",
                           f"{pv_bundle.bundle_id}:{online_record_hash(pv_record)}"))
        outcome = compute_ledger([SlotInput(slot, limited.usable_kw, actual_load, charge, actual_discharge,
                                           export, curtailed, tariff[0]["price_cny_per_kwh"])], meta, storage, energy)
        if not outcome.ledger.balanced or validate_balance(outcome.ledger):
            raise PlantkgError(ErrorCode.DOMAIN_INVALID, "执行账本未平衡或储能越界")
        if storage and outcome.ledger.entries[0].energy_end_kwh < reserve - 1e-8:
            raise PlantkgError(ErrorCode.DOMAIN_INVALID, "储能低于已批准备用下限")
        if storage:
            # B08 is also the authority for the planned storage trajectory.
            # The neutral demand merely balances this private SOC calculation;
            # it is never persisted as an observed load or an execution ledger.
            planned = compute_ledger([SlotInput(slot, 0.0, planned_discharge, planned_charge, planned_discharge, 0.0, 0.0, 1.0)],
                                     meta, storage, planned_energy).ledger
            if not planned.balanced:
                raise PlantkgError(ErrorCode.DOMAIN_INVALID, "计划储能轨迹无法复核")
            planned_end = planned.entries[0].energy_end_kwh
            deviation = (outcome.ledger.entries[0].energy_end_kwh - planned_end) / storage.capacity_kwh
        else:
            planned_end, deviation = 0.0, 0.0
        executed = []
        actuals = {"pv_use": outcome.ledger.entries[0].pv_used_kwh * 4, "pv_limit": limited.limit_kw,
                   "pv_curtail": curtailed, "charge_power": charge, "discharge_power": actual_discharge, "grid_export": export}
        for name, action in values.items():
            actual = actuals[name]
            executed.append(ExecutedAction(action_id=action.action_id, planned_value=action.value,
                executed_value=actual, unit=action.unit, interval=slot, curtailed=max(action.value - actual, 0),
                reason="已批准光伏限发上限" if name == "pv_limit" else
                       "实际供需或储能边界导致削减" if actual < action.value else None))
        if limited.limit_kw and "pv_limit" not in values:
            executed.append(ExecutedAction(action_id=f"actual-pv-limit-{seq}", planned_value=0,
                executed_value=limited.limit_kw, unit="kW", interval=slot, reason="已批准光伏限发上限"))
        if curtailed and "pv_curtail" not in values:
            executed.append(ExecutedAction(action_id=f"actual-pv-curtail-{seq}", planned_value=0,
                executed_value=curtailed, unit="kW", interval=slot, reason="实际供需导致经济弃光"))
        for action in actions:
            if action.quantity in ("operation_start", "operation_power"):
                executed.append(ExecutedAction(action_id=action.action_id, planned_value=action.value,
                                               executed_value=action.value, unit=action.unit, interval=slot))
        errors = {}
        forecasts = basis.get("forecasts") or {}
        for forecast in forecasts.get("forecasts", ()):
            quantity = forecast.get("quantity")
            if quantity not in ("base_load", "pv_available"):
                continue
            target = forecast["target"]
            offset = (current - datetime.fromisoformat(target["start"])) / timedelta(minutes=forecast["step_minutes"])
            if offset == int(offset) and 0 <= offset < len(forecast["values"]):
                predicted = forecast["values"][int(offset)]
                if predicted is not None:
                    operation_power = sum(a.value for a in actions if a.quantity == "operation_power")
                    actual = actual_load - operation_power if quantity == "base_load" else pv_record.value
                    errors[quantity] = actual - predicted
        result = ExecutionResult(run_id=run.run_id, plan_id=approved.plan_id, plan_revision=approved.revision,
                                 event_seq=seq + 1, executed=tuple(executed), ledger=outcome.ledger,
                                 soc_deviation=deviation, forecast_error=errors, idempotency_key=idempotency_key)
        projection = {"storage": {"energy_kwh": outcome.ledger.entries[0].energy_end_kwh,
                                   "capacity_kwh": storage.capacity_kwh,
                                   "planned_energy_kwh": planned_end} if storage else {},
                      "production": operation_states,
                      "mode": mode,
                      "sources": [{"source_id": r.source_id, "source_version": r.source_version,
                                   "fingerprint": online_record_hash(r), "nature": r.nature,
                                   "quality": r.quality.flag, "available_at": r.available_at.isoformat(),
                                   "record": r.model_dump(mode="json")}
                                  for r in (load_record, pv_record)]}
        visible_after = max(slot.end, load_record.available_at, pv_record.available_at)
        return self.repository.commit(context, approved, expected_seq, request_hash, basis_hash, result, projection, visible_after)

    def read_execution(self, context, seq):
        return self.repository.read(context, seq)[0]

    def read_ledger(self, context, seq):
        return self.read_execution(context, seq).ledger

    def read_execution_offline(self, context, seq):
        return self.repository.read_offline(context, seq)[0]
