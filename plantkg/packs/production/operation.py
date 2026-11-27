"""单道工序的配置、运行形状与负荷曲线（B13）。

口径（03 §2.1，07 Q09/Q25）：
- 工序功率 = 设备额定功率（configured）× HIPE 设备曲线形状；HIPE 只给启停与持续时长，不冒称同厂同年实测；
- 规划时 Forecaster 预测不可平移负荷（总进线扣除被选作形状的设备表），工序负荷只由本包经 x[s] 贡献；
- 执行/记账时用 B08 的 replace_shifted_load 把当天设备实际曲线从原时段挪到新时段，并先做总分表覆盖校验。
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from plantkg.core.contracts import LEDGER_TOLERANCE, ErrorCode, Interval, PlantkgError, Ref, SourceBundle
from plantkg.core.ledger import ShiftedOperation, ShiftReport, replace_shifted_load
from plantkg.packs._validation import is_number, number, read_yaml

PROVENANCE_NOTE = "工序与设备的对应、额定功率为配置；曲线形状取自 HIPE（德国 2017），不是本厂同年实测"
_OP_ENTITY = re.compile(r"^entity://[A-Za-z0-9_-]+/operation/([A-Za-z0-9_.-]+)$")
_OP_KEYS = {"operation_id", "entity_id", "rated_kw", "shape", "original_start"}


@dataclass(frozen=True)
class OperationShape:
    """设备一次完整运行的形状：每个 15 分钟时段的相对功率（峰值 = 1）。"""

    fractions: tuple[float, ...]
    meter: str
    source_interval: Interval
    source_bundle_id: str
    content_hash: str
    line_range: tuple[int, int]

    def __post_init__(self) -> None:
        if not self.fractions or any(not is_number(f) or not 0 <= f <= 1 for f in self.fractions):
            raise PlantkgError(ErrorCode.DOMAIN_INVALID, f"工序形状 fractions={self.fractions!r} 须是非空、0～1 之间的有限数列表")
        if max(self.fractions) <= LEDGER_TOLERANCE["power_zero_kw"]:
            raise PlantkgError(ErrorCode.DOMAIN_INVALID, "工序形状 fractions 的峰值必须大于 0")


@dataclass(frozen=True)
class OperationSpec:
    """一道不可抢占的工序。operation_id 与 StateSnapshot.pack_states["production"]["pending_jobs"] 对应。"""

    operation_id: str
    entity_id: str  # entity://{厂区}/operation/{编号}
    rated_kw: float
    shape: OperationShape
    original_start: datetime  # 未优化时的开始时刻（UTC），B0 基线与执行记账用
    refs: tuple[Ref, ...]  # 额定功率、形状来源、数据说明等配置依据

    def __post_init__(self) -> None:
        if not is_number(self.rated_kw) or self.rated_kw <= 0:
            raise PlantkgError(ErrorCode.DOMAIN_INVALID, f"工序 {self.operation_id} 的 rated_kw={self.rated_kw!r} 必须是正有限数（bool 不算数值）")
        if not self.refs:
            raise PlantkgError(ErrorCode.DOMAIN_INVALID, f"工序 {self.operation_id} 缺少配置依据")

    @property
    def profile_kw(self) -> tuple[float, ...]:
        return tuple(self.rated_kw * f for f in self.shape.fractions)

    @property
    def slots(self) -> int:
        return len(self.shape.fractions)


def load_operations(path: Path, config_id: str) -> list[OperationSpec]:
    """读工序配置 yaml（形状 json 与它同目录）。config_id 为该文件的 config:// 路径部分，用于给约束和动作挂依据。

    缺文件、缺段、未知键、bool/非正额定功率、空/越界形状都在装载期报 DOMAIN_INVALID；
    entity 尾名须与 operation_id 一致：窗口键、待排清单都按 operation_id，两者不同会让规划对不上。
    """
    data = read_yaml(path, config_id)
    operations = data.get("operations")
    if not isinstance(operations, list) or not operations:
        raise PlantkgError(ErrorCode.DOMAIN_INVALID, f"配置 {config_id} 没有 operations 列表")
    specs = []
    for k, op in enumerate(operations):
        if not isinstance(op, dict):
            raise PlantkgError(ErrorCode.DOMAIN_INVALID, f"配置 {config_id} 的 operations[{k}] 须是映射，实际 {op!r}")
        unknown = sorted(set(op) - _OP_KEYS)
        if unknown:
            raise PlantkgError(ErrorCode.DOMAIN_INVALID, f"配置 {config_id} 的 operations[{k}] 有未知键 {unknown}")
        operation_id = op.get("operation_id")
        if not isinstance(operation_id, str) or not operation_id:
            raise PlantkgError(ErrorCode.DOMAIN_INVALID, f"配置 {config_id} 的 operations[{k}].operation_id 不能为空")
        m = _OP_ENTITY.match(str(op.get("entity_id", "")))
        if not m:
            raise PlantkgError(ErrorCode.DOMAIN_INVALID, f"配置 {config_id} 的 operations[{k}].entity_id={op.get('entity_id')!r} 须是 entity://{{厂区}}/operation/{{编号}}")
        if m.group(1) != operation_id:
            raise PlantkgError(ErrorCode.DOMAIN_INVALID,
                               f"配置 {config_id} 的 operations[{k}] entity_id 尾名 {m.group(1)} 与 operation_id {operation_id!r} 不一致")
        key = f"config://{config_id}#operations.{operation_id}"
        try:
            raw = json.loads((Path(path).parent / str(op.get("shape"))).read_text(encoding="utf-8"))
            shape = OperationShape(fractions=tuple(raw["fractions"]), meter=raw["meter"], source_interval=Interval(**raw["interval"]),
                                   source_bundle_id=raw["source_bundle_id"], content_hash=raw["content_hash"], line_range=tuple(raw["line_range"]))
            original_start = datetime.fromisoformat(op["original_start"])
        except (OSError, KeyError, TypeError, ValueError) as exc:  # 缺文件、缺键、类型不对都不抛原生异常
            raise PlantkgError(ErrorCode.DOMAIN_INVALID, f"配置 {key} 的形状或开始时刻读取失败：{exc}") from exc
        if original_start.utcoffset() is None:
            raise PlantkgError(ErrorCode.DOMAIN_INVALID, f"配置 {key}.original_start 必须带时区")
        specs.append(OperationSpec(
            operation_id=operation_id, entity_id=op["entity_id"], rated_kw=number(op.get("rated_kw"), f"{key}.rated_kw"), shape=shape,
            original_start=original_start,
            refs=(Ref(kind="config", id=f"{key}.rated_kw"), Ref(kind="config", id=f"{key}.shape"),
                  Ref(kind="config", id=f"config://{config_id}#provenance_note"))))
    return specs


def operation_shape(bundle: SourceBundle, run: Interval, meter: str) -> OperationShape:
    """从 B05 规范化的设备表取一段运行，归一成形状。只用 configured 视图；运行期内缺测直接拒绝，不补零。"""
    rows = [r for r in bundle.records if r.nature == "configured" and run.start <= r.interval.start < run.end]
    if not rows or any(r.locator.meter != meter for r in rows):
        raise PlantkgError(ErrorCode.DOMAIN_INVALID, f"来源 {bundle.bundle_id} 在 {run.start}～{run.end} 没有设备 {meter} 的记录")
    rows.sort(key=lambda r: r.interval.start)
    if rows[0].interval.start != run.start or rows[-1].interval.end != run.end or any(
            a.interval.end != b.interval.start for a, b in zip(rows, rows[1:])):
        raise PlantkgError(ErrorCode.DOMAIN_INVALID, f"设备 {meter} 在运行区间内时段不连续")
    missing = [r.interval.start.isoformat() for r in rows if r.value is None]
    if missing:
        raise PlantkgError(ErrorCode.DOMAIN_INVALID, f"设备 {meter} 运行区间内缺测 {missing}，不能当 0 做形状")
    peak = max(r.value for r in rows)
    if peak <= LEDGER_TOLERANCE["power_zero_kw"]:
        raise PlantkgError(ErrorCode.DOMAIN_INVALID, f"设备 {meter} 在运行区间内没有运行")
    lines = [n for r in rows for n in (r.locator.line_start, r.locator.line_end) if n is not None]
    return OperationShape(fractions=tuple(r.value / peak for r in rows), meter=meter, source_interval=run,
                          source_bundle_id=bundle.bundle_id, content_hash=rows[0].content_hash,
                          line_range=(min(lines), max(lines)) if lines else (0, 0))


def load_profile_for_start(spec: OperationSpec, start: int, n_slots: int) -> tuple[float, ...]:
    """工序从第 start 个时段开始时，在 n_slots 个时段上的负荷（kW）；放不完整就拒绝（不可抢占）。"""
    if start < 0 or start + spec.slots > n_slots:
        raise PlantkgError(ErrorCode.DOMAIN_INVALID, f"工序 {spec.operation_id} 从时段 {start} 开始放不进 {n_slots} 个时段")
    out = [0.0] * n_slots
    for k, p in enumerate(spec.profile_kw):
        out[start + k] = p
    return tuple(out)


def slot_index(time_set: Sequence[Interval], instant: datetime) -> int | None:
    for i, slot in enumerate(time_set):
        if slot.start == instant:
            return i
    return None


def shifted_total(total: SourceBundle, device: SourceBundle, spec: OperationSpec, window: Interval, new_start: datetime,
                  slot: timedelta = timedelta(minutes=15)) -> tuple[ShiftReport, dict]:
    """执行记账：总进线扣除设备当天原运行段，再加到 new_start。先做总分表覆盖校验，再调 B08 账本端口。

    两份来源须是同一倍数放大的 configured 视图、时段一一对齐、区间内无缺测；设备任一时段大于总表即判重复计量。
    返回（平移报告，覆盖校验摘要）。
    """
    def view(bundle: SourceBundle) -> list:
        rows = sorted((r for r in bundle.records if r.nature == "configured" and window.start <= r.interval.start < window.end),
                      key=lambda r: r.interval.start)
        if len(rows) != round((window.end - window.start) / slot):
            raise PlantkgError(ErrorCode.DOMAIN_INVALID, f"来源 {bundle.bundle_id} 没有完整覆盖 {window.start}～{window.end}")
        return rows

    main, dev = view(total), view(device)
    if [r.interval for r in main] != [r.interval for r in dev]:
        raise PlantkgError(ErrorCode.DOMAIN_INVALID, "总表与设备表时段不对齐")
    factors = {r.transform_steps[-1].params.get("factor") for r in main + dev}
    if len(factors) != 1:
        raise PlantkgError(ErrorCode.DOMAIN_INVALID, f"总表与设备表放大倍数不一致：{sorted(map(str, factors))}")
    missing = [r.interval.start.isoformat() for r in main + dev if r.value is None]
    if missing:
        raise PlantkgError(ErrorCode.DOMAIN_INVALID, f"总表或设备表缺测 {missing[:5]}，不能做平移替换")
    tol = LEDGER_TOLERANCE["power_zero_kw"]
    over = [(m.interval.start.isoformat(), d.value - m.value) for m, d in zip(main, dev) if d.value > m.value + tol]
    if over:
        raise PlantkgError(ErrorCode.DOMAIN_INVALID, f"设备 {spec.shape.meter} 超过总表 {over[:3]}：分表不属于总表或重复计量")
    times = [r.interval.start for r in main]
    for label, instant in (("原开始", spec.original_start), ("新开始", new_start)):
        if instant not in times:
            raise PlantkgError(ErrorCode.DOMAIN_INVALID, f"工序 {spec.operation_id} {label} {instant} 不在记账区间的时段网格上")
    orig, new = times.index(spec.original_start), times.index(new_start)
    profile = tuple(r.value for r in dev[orig:orig + spec.slots])
    if len(profile) != spec.slots:
        raise PlantkgError(ErrorCode.DOMAIN_INVALID, f"工序 {spec.operation_id} 原运行段超出记账区间")
    report = replace_shifted_load([r.value for r in main], [ShiftedOperation(spec.operation_id, profile, orig, new)],
                                  slot / timedelta(hours=1))
    coverage = {"slots": len(main), "factor": factors.pop(), "max_device_share": max(
        (d.value / m.value for m, d in zip(main, dev) if m.value > tol), default=0.0),
        "total_bundle": total.bundle_id, "device_bundle": device.bundle_id, "note": PROVENANCE_NOTE}
    return report, coverage
