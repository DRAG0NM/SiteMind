"""规则表示与实体归一：triage、编译、调查和专家读取共用的一处实现。

冲突比较不再直接比原始 draft（#96）；B30 只在这里维护"消费者实体 → B25 图谱编号"
的映射，聚合实体不登记进图谱（#143、#175 裁决）。
"""
from __future__ import annotations

from plantkg.core.contracts import Bounds

# 候选 draft 的 kind → 编译后 Rule.kind（#96：三处共用这一份映射）
COMPILED_KINDS = {"tariff_tou_partial": "tariff_tou", "workorder_rule_partial": "maintenance_advisory"}
# 消费者使用的聚合实体 → B25 图谱编号；聚合实体不入图（#175：B30 映射表）
AGGREGATE_ENTITIES = {
    "air_system/AIR": ("AIR-HDR",),
    "grid_connection/MAIN": ("GRID",),
    "elevator_group/admin_low": ("PE-1", "PE-2", "PE-3"),
    "elevator_group/admin_high": ("PE-4", "PE-5", "PE-6"),
    "elevator_group/freight": ("FE-1", "FE-2", "FE-3", "FE-4"),
}
# 电梯规则消费端能执行的 action；空表示按候梯时间阈值处理（#144）
ELEVATOR_ACTIONS = frozenset({"增派轿厢并保持分区"})
# 浮点比较容差取固定值（#175 待定项：不复用求解器的 mip_rel_gap）
TOLERANCE = 1e-9


def normalised_kind(source) -> str | None:
    """候选 draft 或 Rule → 编译后的规则种类。"""
    kind = source.get("kind") if isinstance(source, dict) else getattr(source, "kind", None)
    return COMPILED_KINDS.get(kind, kind)


def _consumer_path(entity) -> str:
    """去掉 entity://{厂区}/ 前缀，留下消费者使用的 类型/编号。"""
    ref = str(entity or "").strip()
    if not ref.startswith("entity://"):
        return ref
    parts = ref[len("entity://"):].split("/")
    return "/".join(parts[1:]) if len(parts) >= 3 else ""


def entity_keys(entity) -> tuple[str, ...]:
    """任何实体写法 → B25 图谱侧对等编号；消费者解析不了时返回空元组。

    接受图谱编号（AIR-HDR）、entity:// 标识、聚合实体（air_system/AIR、
    elevator_group/admin_low、grid_connection/MAIN）和区域（zone/admin、zone:admin）。
    """
    ref = _consumer_path(entity)
    if not ref:
        return ()
    if ref in AGGREGATE_ENTITIES:
        return AGGREGATE_ENTITIES[ref]
    if ref.startswith("zone:"):
        return (ref,)
    kind, separator, code = ref.partition("/")
    if separator and code:
        return ("zone:" + code,) if kind == "zone" else (code,)
    return (ref,)


def is_elevator_group(entity) -> bool:
    ref = _consumer_path(entity)
    return ref.startswith("elevator_group/") or ref.startswith("elevator_group:")


def unresolved_entity(entity_type, entity) -> bool:
    """实体是否不能被消费者解析；区域看映射，设备看图谱节点。"""
    keys = entity_keys(entity)
    if not keys or entity_type is None:
        return True
    return any(not key.startswith("zone:") and not entity_type(key) for key in keys)


def subject_keys(rule) -> frozenset[str]:
    """规则主体的消费端归一编号集合。"""
    return frozenset(key for subject in rule.subject_ids for key in entity_keys(subject))


def same_access(left, right) -> bool:
    """两条规则是否作用于同一组消费端主体：种类相同且归一主体集合相同。"""
    return left.kind == right.kind and subject_keys(left) == subject_keys(right)


def zone_hook_for(rule, registry):
    """按规则类型取消费端区域钩子：温度类优先 hvac.consumer_zone，缺失回退 router.entity_zone；其他类型用 router.entity_zone（#225）。"""
    names = ("hvac.consumer_zone", "router.entity_zone") if rule.kind == "zone_temperature_limit" else ("router.entity_zone",)
    for name in names:
        hooks = registry.hooks_for(name)
        if hooks:
            return hooks[-1]
    return None


def _field(source, name):
    return source.get(name) if isinstance(source, dict) else getattr(source, name, None)


def _bounds(source) -> dict | None:
    raw = _field(source, "bounds")
    if raw is None:
        return None
    try:
        return Bounds.model_validate(raw).model_dump()
    except ValueError:
        return None


def _ratios(source) -> dict[str, float]:
    if isinstance(source, dict):
        periods = source.get("suggested_periods") or []
    else:
        periods = (getattr(source, "condition", None) or {}).get("periods") or []
    return {str(period["name"]): float(period["ratio"]) for period in periods
            if isinstance(period, dict) and isinstance(period.get("ratio"), (int, float))}


def _text(source) -> str | None:
    if isinstance(source, dict):
        text = source.get("source_text")
    else:
        text = (getattr(source, "condition", None) or {}).get("source_text")
    return " ".join(str(text).split()) if text else None


def normalised_view(source) -> dict:
    """候选草稿或编译后规则 → 可比较视图；部分候选比较它能表达的阈值（#96）。"""
    kind = normalised_kind(source)
    if kind == "tariff_tou":
        return {"ratios": _ratios(source)}
    if kind == "maintenance_advisory":
        return {"text": _text(source)}
    return {"value": _field(source, "value"), "bounds": _bounds(source),
            "quantity": _field(source, "quantity"), "unit": _field(source, "unit")}


def _same_number(left, right) -> bool:
    if left is None or right is None:
        return left is None and right is None
    if isinstance(left, bool) or isinstance(right, bool):
        return left == right
    try:
        return abs(float(left) - float(right)) <= TOLERANCE
    except (TypeError, ValueError):
        return left == right


def _same_bounds(left, right) -> bool:
    if left is None or right is None:
        return left is None and right is None
    return all(_same_number(left.get(key), right.get(key)) for key in ("lower", "upper")) and set(left) == set(right)


def _same_ratios(left: dict, right: dict) -> bool:
    return set(left) == set(right) and all(_same_number(left[key], right[key]) for key in left)


def views_conflict(left: dict, right: dict) -> bool:
    """两个归一视图是否有实质差异；缺键按 None 比较。"""
    for key in set(left) | set(right):
        if key == "ratios":
            if not _same_ratios(left.get(key) or {}, right.get(key) or {}):
                return True
        elif key == "bounds":
            if not _same_bounds(left.get(key), right.get(key)):
                return True
        elif key == "value":
            if not _same_number(left.get(key), right.get(key)):
                return True
        elif left.get(key) != right.get(key):
            return True
    return False


def subject_zone(entity, zone_of) -> str | None:
    """实体 → 区域编号；zone:xx 与设备/编号都交给消费端钩子解析，钩子说没有的区域返回 None（#175 方案 3c、#205）。

    没有登记钩子时 zone:xx 直接取（旧装配仍可用）。
    """
    for key in entity_keys(entity):
        if key.startswith("zone:"):
            return zone_of(key) if zone_of is not None else key.split(":", 1)[1]
        found = zone_of(key) if zone_of is not None else None
        if found:
            return found
    return None


def consumer_mismatch(rule, entity_type=None, zone_of=None, tariff_category=None) -> str | None:
    """编译后的规则能否被消费端接受；返回原因，能接受返回 None（#143/#144）。"""
    keys = tuple(key for subject in rule.subject_ids for key in entity_keys(subject))
    # 与 unresolved_entity 同一口径：没有图谱钩子时主体一律按不可解析处理（#205）
    if not keys or entity_type is None:
        return f"主体 {rule.subject_ids} 不能被消费端解析"
    unknown = [key for key in keys if not key.startswith("zone:") and not entity_type(key)]
    if unknown:
        return f"主体 {unknown} 不在图谱"
    if rule.kind == "elevator_congestion_rule":
        if not any(is_elevator_group(subject) for subject in rule.subject_ids):
            return "电梯规则须作用于电梯群，单梯主体不被消费端接受"
        if rule.bounds is None or rule.bounds.upper is None:
            return "电梯规则缺少候梯时间上界"
        if not rule.condition.get("time_range") or not rule.condition.get("zones"):
            return "电梯规则缺少生效时段或楼层分区"
        action = rule.condition.get("action")
        if action not in (None, "") and action not in ELEVATOR_ACTIONS:
            return f"电梯规则 action={action!r} 不在消费端可执行范围"
    elif rule.kind == "zone_temperature_limit":
        if rule.bounds is None or rule.bounds.lower is None or rule.bounds.upper is None:
            return "暖通规则须同时给出温度上下界或声明单边语义"
        if not any(subject_zone(subject, zone_of) for subject in rule.subject_ids):
            return "暖通规则主体不能映射到区域"
    elif rule.kind == "tariff_tou":
        if not any(entity_type(key) == "grid" for key in keys):
            return "电价规则主体不是图谱中的电网接入点"
        if not isinstance(rule.condition.get("category"), str) or not rule.condition["category"].strip():
            return "电价规则缺少用电类别"
        if tariff_category is None:
            return "缺少厂区电价类别配置，无法校验电价规则"
        if rule.condition.get("category") != tariff_category:
            return f"电价类别 {rule.condition.get('category')} 与厂区配置 {tariff_category} 不一致"
    return None
