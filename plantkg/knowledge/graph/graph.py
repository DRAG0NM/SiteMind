"""全局图谱（B25）：设备拓扑、介质网络、安全标记、优先级配置与应急预案，存进 Oxigraph，按 SPARQL 查询。

`GlobalGraph.load(graph_yaml)` 读 B18 的 site.yaml 和本目录配置，写入命名图 BASE_GRAPH。查询都走 Oxigraph：
trace_impact 沿介质向下找受影响设备与区域，find_sources 按症状找来源候选，safety_tags 确定性判断动作是否碰安全标记，
priority_rules 与 playbooks 读优先级窗口和预案。B30 等后续写入方用 `add_subgraph` 写自己的子图，查询取全部子图的并集。
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
from collections import deque
from datetime import datetime, time
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import yaml
from pyoxigraph import Literal, NamedNode, Quad, RdfFormat, Store

from plantkg.core.contracts import ErrorCode, PlantkgError
from plantkg.knowledge.rules.normalise import AGGREGATE_ENTITIES

PK = "https://plantkg.dev/ns/graph#"
BRICK = "https://brickschema.org/schema/Brick#"
RDFS_LABEL = NamedNode("http://www.w3.org/2000/01/rdf-schema#label")
RDF_TYPE = NamedNode("http://www.w3.org/1999/02/22-rdf-syntax-ns#type")
BASE_GRAPH = "urn:plantkg:graph:global:base"
SUBGRAPH_PREFIX = "urn:plantkg:graph:global:"
MEDIA = ("power", "cooling_water", "compressed_air", "chilled_water")
# 实际命令 → 概念动作：契约 ProposedAction.command 用实际命令名，图谱的安全标记用概念动作名。
# 一个命令有多种语义时全部列出（如 set_mode 既可改运行模式也可能推迟预冷），避免词表不一致漏判。
# 键与值都必须在 graph.yaml 的 state_changing_actions/safe_actions 里（load 时校验）。
COMMAND_ACTIONS: dict[str, tuple[str, ...]] = {
    "set_mode": ("zone_mode", "precool_delay"),
    "set_active": ("zone_mode",),
    "set_setpoint": ("setpoint_change",),
    "switch_circuit": ("power_off",),
    "switch_standby": ("power_off",),
    "isolate_leak": ("stop",),
    "repair": ("maintenance",),
    "rescue": ("maintenance",),
    "ventilate": ("start",),
}
# 非切断动作作用于区域时的目标领域：暖通类动作只直接碰暖通设备，区域自身的温度硬约束仍按区域标记判定。
# 没有列出的非切断动作只在区域自身的约束上判定，不把区内每个节点都当成直接受害。
ACTION_DOMAINS: dict[str, frozenset[str]] = {
    "zone_mode": frozenset({"hvac"}),
    "precool_delay": frozenset({"hvac"}),
    "setpoint_change": frozenset({"hvac"}),
}
# 聚合对象（电梯群、空压系统）：不在图谱节点里，但不算“查不清”，按具体设备判定。
AGGREGATE_KINDS = ("elevator_group", "air_system")

# 会切断目标对下游供给的动作：沿全部介质（电、冷却水、压缩空气、冷冻水）查下游是否带安全标记
CUT_SUPPLY = frozenset({"power_off", "shed", "curtail", "stop", "maintenance"})
# 其中彻底停供的动作互相等价（限负荷 curtail 不算：只降功率不断供）
FULL_CUT = CUT_SUPPLY - {"curtail"}
# 聚合对象哪些动作展开成成员设备逐台判定，由 graph.yaml 的 aggregate_policies 配置（#240）；
# 其余动作保持群级判定（auto_ok），如空压隔离/检修、电梯群模式调整。
# 聚合类型没配 aggregate_policies 时沿用 #201 的展开集合：只展开供电切断类，不能整类 auto_ok。
DEFAULT_AGGREGATE_EXPAND = frozenset({"power_off", "shed", "curtail"})
# 症状只沿相关介质往上找；未列出的症状查全部介质
SYMPTOM_MEDIA = {"temperature_deviation": ("power", "chilled_water", "cooling_water"), "power_loss": ("power",)}
LOAD_SYMPTOMS = ("load_surge", "overload")
SYMPTOMS = (*LOAD_SYMPTOMS, "pressure_drop", *SYMPTOM_MEDIA)
# 区域查询时只从这些领域的设备起步（温度偏离只看暖通设备）；未列出的症状不限领域
SYMPTOM_DOMAINS = {"temperature_deviation": {"hvac"}}
DEFAULT_CONFIG = Path(__file__).resolve().parents[3] / "configs/sites/factory/graph/graph.yaml"


def _p(name: str) -> NamedNode:
    return NamedNode(PK + name)


def normalize_action(action: str) -> tuple[str, ...]:
    """实际命令归一成概念动作；未登记命令原样返回，由 safety_tags 按明确规则处理。"""
    return COMMAND_ACTIONS.get(action, (action,))


def _check_action_vocabulary(cfg: dict) -> None:
    """词表同源：归一表的键与目标、后果档位引用的动作都必须是 graph.yaml 登记过的动作。"""
    known = set(cfg["state_changing_actions"]) | set(cfg["safe_actions"])
    bad = sorted((set(COMMAND_ACTIONS) - known) |
                 ({action for actions in COMMAND_ACTIONS.values() for action in actions} - known) |
                 ({action for actions in (cfg.get("action_consequences") or {}).values() for action in actions} - known))
    if bad:
        raise ValueError(f"动作词表不一致：{bad} 不在 state_changing_actions/safe_actions 里")


def _check_aggregate_policies(cfg: dict) -> None:
    """聚合对象的展开策略与动作词表同源：键是聚合类型，expand_on 里是登记过的动作（#240）。"""
    policies = cfg.get("aggregate_policies") or {}
    unknown = sorted(set(policies) - set(AGGREGATE_KINDS))
    if unknown:
        raise ValueError(f"聚合策略里未知的聚合类型：{unknown}")
    known = set(cfg["state_changing_actions"]) | set(cfg["safe_actions"])
    for kind, policy in policies.items():
        actions = policy.get("expand_on") if isinstance(policy, dict) else None
        if not isinstance(actions, list) or not all(isinstance(action, str) for action in actions):
            raise ValueError(f"聚合策略 {kind} 的 expand_on 必须是动作列表")
        bad = sorted(set(actions) - known)
        if bad:
            raise ValueError(f"聚合策略 {kind} 的动作 {bad} 不在 state_changing_actions/safe_actions 里")


ACTION_TEMPLATE_KINDS = frozenset({"protection", "disposal"})
ACTION_TEMPLATE_FIELDS = frozenset({"role", "command", "params", "minutes", "readiness"})
ACTION_TEMPLATE_READINESS = frozenset({"ready", "needs_human", "needs_diagnosis"})


def validate_playbook_spec(spec: dict, known_actions: set[str], where: str) -> None:
    """预案结构化动作模板的 schema：graph.yaml 与 B30 子图共用一处校验。

    模板字段：role（实体角色）、command（动作词表内的命令）、params、minutes（正整数）、readiness（可选）。
    类别只有 protection（开单即执行）与 disposal（工单确认后执行）；没有 actions 的旧格式仍允许，B47 回退代码缺省映射。
    """
    if not isinstance(spec, dict) or not isinstance(spec.get("id"), str) or not spec["id"]:
        raise ValueError(f"{where} 预案动作模板缺少预案 id")
    actions = spec.get("actions")
    if actions is None:
        return
    if not isinstance(actions, dict) or set(actions) - ACTION_TEMPLATE_KINDS:
        raise ValueError(f"{where} 预案动作模板类别只能是 protection/disposal")
    for kind, items in actions.items():
        if not isinstance(items, list):
            raise ValueError(f"{where} 预案动作模板 {kind} 必须是列表")
        for item in items:
            if not isinstance(item, dict) or set(item) - ACTION_TEMPLATE_FIELDS:
                raise ValueError(f"{where} 预案动作模板字段不合法：{item!r}")
            command = item.get("command")
            minutes = item.get("minutes")
            if not isinstance(item.get("role"), str) or not item["role"]:
                raise ValueError(f"{where} 预案动作模板缺少 role")
            if command not in known_actions:
                raise ValueError(f"{where} 预案动作模板的命令 {command!r} 不在动作词表里")
            if not isinstance(minutes, int) or isinstance(minutes, bool) or minutes <= 0:
                raise ValueError(f"{where} 预案动作模板的 minutes 必须是正整数")
            if not isinstance(item.get("params", {}), dict):
                raise ValueError(f"{where} 预案动作模板的 params 必须是映射")
            if item.get("readiness", "ready") not in ACTION_TEMPLATE_READINESS:
                raise ValueError(f"{where} 预案动作模板的 readiness {item.get('readiness')!r} 不可用")


def _floors(spec: str) -> list[tuple[str, int]]:
    zone, _, rng = spec.partition(":")
    lo, _, hi = rng.partition("-")
    return [(zone, f) for f in range(int(lo), int(hi or lo) + 1)]


class GlobalGraph:
    def __init__(self, store: Store, plant_id: str, plant_tz: str, knowledge_version: str):
        self.store, self.plant_id, self.plant_tz, self.knowledge_version = store, plant_id, plant_tz, knowledge_version
        self._stamp()

    def _stamp(self) -> None:
        """版本 = 配置版本 @ base 与全部子图内容的摘要；子图变了版本跟着变。"""
        h = hashlib.sha256()
        for g in sorted((g for g in self.store.named_graphs() if g.value.startswith(SUBGRAPH_PREFIX)), key=lambda g: g.value):
            h.update(g.value.encode("utf-8"))
            h.update(self.store.dump(format=RdfFormat.N_QUADS, from_graph=g))
        self.version = f"{self.knowledge_version}@{h.hexdigest()[:12]}"

    # ------------------------------------------------------------------ 构建
    @classmethod
    def load(cls, graph_yaml: str | Path = DEFAULT_CONFIG, store: Store | None = None) -> GlobalGraph:
        path = Path(graph_yaml)
        cfg = yaml.safe_load(path.read_text(encoding="utf-8"))
        _check_action_vocabulary(cfg)
        _check_aggregate_policies(cfg)
        regs = yaml.safe_load((path.parent / "regulations.yaml").read_text(encoding="utf-8"))
        site = _read_site(path.parent / cfg["site"])
        quads = _Builder(site, cfg, regs).quads()
        store = Store() if store is None else store
        g = NamedNode(BASE_GRAPH)
        store.remove_graph(g)
        store.extend(Quad(s, p, o, g) for s, p, o in quads)
        return cls(store, site.plant_id, site.plant_tz, cfg["knowledge_version"])

    def add_subgraph(self, name: str, turtle: str) -> str:
        """后续写入方（B30 本体、B32 进化）按约定写自己的子图 urn:plantkg:graph:global:<name>，不改 base。"""
        if not name or name == "base" or not name.replace("-", "").replace("_", "").isalnum():
            raise PlantkgError(ErrorCode.INVALID_REQUEST, f"子图名 {name!r} 不可用")
        g = NamedNode(SUBGRAPH_PREFIX + name)
        staging = Store()
        staging.load(input=turtle, format=RdfFormat.TURTLE)  # 先完整解析，失败不碰正式图
        self._validate_subgraph(staging)  # 预案动作模板与 base 同一 schema，坏模板不进正式图
        self.store.remove_graph(g)
        self.store.load(input=turtle, format=RdfFormat.TURTLE, to_graph=g)
        self._stamp()
        return g.value

    def _validate_subgraph(self, staging: Store) -> None:
        """校验子图里写的预案动作模板；B25 的 schema 是 B30 写入的门禁。"""
        rows = staging.query(f"PREFIX pk: <{PK}> SELECT ?spec WHERE {{ ?p a pk:Playbook ; pk:spec ?spec }}",
                             use_default_graph_as_union=True)
        known = self._action_vocabulary()
        for row in rows:
            spec = json.loads(row["spec"].value)
            try:
                validate_playbook_spec(spec, known, f"子图预案 {spec.get('id', '?')}：")
            except ValueError as exc:
                raise PlantkgError(ErrorCode.INVALID_REQUEST, str(exc)) from None

    def _action_vocabulary(self) -> set[str]:
        meta = self._select("SELECT ?changing ?safe WHERE { ?m pk:stateChanging ?changing ; pk:safeActions ?safe }")[0]
        return set(json.loads(meta["changing"])) | set(json.loads(meta["safe"]))

    def turtle(self) -> str:
        return self.store.dump(format=RdfFormat.TURTLE, from_graph=NamedNode(BASE_GRAPH)).decode("utf-8")

    # ------------------------------------------------------------------ 底层查询
    def _select(self, sparql: str) -> list[dict[str, str]]:
        rows = self.store.query(f"PREFIX pk: <{PK}> PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#> {sparql}",
                                use_default_graph_as_union=True)
        names = [v.value for v in rows.variables]
        return [{n: (s[n].value if s[n] is not None else None) for n in names} for s in rows]

    def _nodes(self) -> dict[str, dict]:
        rows = self._select("SELECT ?n ?code ?label ?type ?domain ?zone ?kw ?src WHERE { ?n pk:code ?code ; rdfs:label ?label ; "
                            "pk:type ?type ; pk:domain ?domain . OPTIONAL { ?n pk:inZone/pk:code ?zone } "
                            "OPTIONAL { ?n pk:ratedKw ?kw } OPTIONAL { ?n pk:supplyOnly ?src } }")
        return {r["code"]: {"code": r["code"], "label": r["label"], "type": r["type"], "domain": r["domain"],
                            "zone": r["zone"], "rated_kw": float(r["kw"]) if r["kw"] else None, "iri": r["n"],
                            "supply_only": r["src"] == "true"} for r in rows}

    def _edges(self, media=MEDIA) -> list[tuple[str, str, str]]:
        preds = " ".join(f"pk:feeds_{m}" for m in media)
        return [(r["a"], r["b"], r["p"][len(PK) + 6:]) for r in self._select(
            f"SELECT ?a ?b ?p WHERE {{ VALUES ?p {{ {preds} }} ?x ?p ?y . ?x pk:code ?a . ?y pk:code ?b }}")]

    def _resolve(self, ref: str, nodes: dict) -> str:
        code = ref.rsplit("/", 1)[-1] if ref.startswith("entity://") else ref
        if code not in nodes:
            raise PlantkgError(ErrorCode.NOT_FOUND, f"图谱里没有 {ref}")
        return code

    @staticmethod
    def _walk(start: str, edges, reverse=False) -> dict[str, list[str]]:
        """广度优先，返回每个可达节点的最短路径（从 start 起，含两端）。"""
        nxt: dict[str, list[str]] = {}
        for a, b, _ in edges:
            nxt.setdefault(b if reverse else a, []).append(a if reverse else b)
        paths, queue = {start: [start]}, deque([start])
        while queue:
            cur = queue.popleft()
            for n in sorted(nxt.get(cur, ())):
                if n not in paths:
                    paths[n] = paths[cur] + [n]
                    queue.append(n)
        return paths

    @staticmethod
    def _upchain(code: str, edges) -> list[str]:
        """沿上游一路找到源头，返回源头 → code 的路径（多个上游取编号最小的一个）。"""
        up: dict[str, list[str]] = {}
        for a, b, _ in edges:
            up.setdefault(b, []).append(a)
        path = [code]
        while up.get(path[0]) and min(up[path[0]]) not in path:
            path.insert(0, min(up[path[0]]))
        return path

    def _served(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for r in self._select("SELECT ?code ?t WHERE { ?n pk:code ?code ; pk:serves ?x . ?x pk:code ?t }"):
            out.setdefault(r["code"], []).append(r["t"])
        return out

    # ------------------------------------------------------------------ 公开查询
    def trace_impact(self, entity: str, media: tuple[str, ...] | None = None) -> dict:
        """某节点停供或异常时，沿介质向下受影响的设备、楼层与区域，各带一条图谱路径。"""
        nodes = self._nodes()
        start = self._resolve(entity, nodes)
        edges = self._edges(media or MEDIA)
        paths = self._walk(start, edges)
        served, zones = self._served(), self._zone_labels()
        has_down = {a for a, _, _ in self._edges()}  # 末端按全部介质判断，按介质过滤时起点不被当成末端
        affected, areas = [], {}
        for code, path in sorted(paths.items(), key=lambda kv: (len(kv[1]), kv[0])):
            n = nodes[code]
            if code != start:  # 起点自身不算受影响设备，但它服务的楼层、区域算
                affected.append({"code": code, "label": n["label"], "type": n["type"], "domain": n["domain"], "path": path})
            # 区域只从用户端算：有服务关系、计负荷或末端的节点；管网、母线等中间节点不单独算
            if code in served or n["rated_kw"] or code not in has_down:
                for target in served.get(code) or ([n["zone"]] if n["zone"] else []):
                    areas.setdefault(target, path + [target])
                    if ":" in target:  # 服务某层也算影响该层所在区域
                        areas.setdefault(target.split(":")[0], path + [target.split(":")[0]])
        return {"entity": start, "label": nodes[start]["label"], "knowledge_version": self.version,
                "affected": affected,
                "zones": [{"zone": z, "label": zones[z], "path": p} for z, p in areas.items() if z in zones],
                "floors": sorted((z for z in areas if z not in zones), key=lambda f: (f.split(":")[0], int(f.split(":")[1]))),
                "safety": _group(self._tags_on([start, *paths]), {c: p for c, p in paths.items()})}

    def node(self, ref: str) -> dict | None:
        """按编号或 entity:// 标识取节点；图谱里没有返回 None。"""
        return self._nodes().get(ref.rsplit("/", 1)[-1] if ref.startswith("entity://") else ref)

    def neighbors(self, ref: str) -> list[str]:
        """一跳邻居：沿介质边的直接上游与下游，按 entity:// 标识返回（B27 recall 用）。"""
        nodes = self._nodes()
        code = self._resolve(ref, nodes)
        found = {b if a == code else a for a, b, _ in self._edges() if code in (a, b)}
        return [f"entity://{self.plant_id}/{nodes[c]['type']}/{c}" for c in sorted(found)]

    def is_zone(self, ref: str) -> bool:
        return (ref.rsplit("/", 1)[-1] if ref.startswith("entity://") else ref) in self._zone_labels()

    def direct_tags(self, code: str) -> list[dict]:
        """节点自身带的安全标记（不沿供给链传播）。"""
        return self._tags_on([code])

    def find_sources(self, location: str, symptom: str) -> dict:
        """按症状在图谱上找来源候选，并按领域汇总（给 Jev 打领域权重）。
        load_surge/overload：位置下游的用电设备（load），区域查询时路径从供电源头写到设备；
        pressure_drop：上游供给方（supply，供给不足）与下游管网、用气点（leak，泄漏点）；
        其他症状（温度偏离、失电等）：上游各介质的供给方（supply）。
        位置为区域时，服务该区域的设备只作搜索入口：失电时它们是受影响的负荷，不算候选；其他症状下标 serving。"""
        nodes = self._nodes()
        zones = self._zone_labels()
        notes = [] if symptom in SYMPTOMS else [f"症状 {symptom} 未登记，按全部介质查上游供给方"]
        candidates: dict[str, dict] = {}

        def add(code, role, path):
            n = nodes[code]
            if code not in candidates or len(path) < len(candidates[code]["path"]):
                candidates[code] = {"code": code, "label": n["label"], "type": n["type"], "domain": n["domain"],
                                    "role": role, "rated_kw": n["rated_kw"], "path": path}

        power = self._edges(("power",))
        if location in zones:
            served = self._served()
            if symptom in LOAD_SYMPTOMS:  # 区域内全部计负荷设备
                for c in sorted(c for c, n in nodes.items() if n["zone"] == location and n["rated_kw"] and not n["supply_only"]):
                    add(c, "load", self._upchain(c, power))
            elif symptom == "pressure_drop":
                self._zone_pressure_drop(location, nodes, served, add, notes)
            else:  # 入口：服务本区或本区某层的设备，及未声明服务的区内负荷
                wanted = SYMPTOM_DOMAINS.get(symptom)
                starts = sorted(c for c, n in nodes.items()
                                if (any(t == location or t.startswith(location + ":") for t in served.get(c, ())) or
                                    (n["zone"] == location and c not in served and n["rated_kw"]))
                                and (wanted is None or n["domain"] in wanted))
                edges = self._edges(SYMPTOM_MEDIA.get(symptom, MEDIA))
                for s in starts:
                    for code, path in self._walk(s, edges, reverse=True).items():
                        if code != s:
                            add(code, "supply", list(reversed(path)) + [location])
                if symptom != "power_loss":
                    for s in starts:
                        if s not in candidates:
                            add(s, "serving", [s, location])
        else:
            s = self._resolve(location, nodes)
            if symptom in LOAD_SYMPTOMS:
                for code, path in self._walk(s, power).items():
                    if nodes[code]["rated_kw"] and not nodes[code]["supply_only"]:
                        add(code, "load", self._upchain(s, power) if code == s else path)  # 位置本身是负荷时写出供电链
            else:
                if symptom == "pressure_drop":
                    for code, path in self._walk(s, self._edges(("compressed_air",))).items():
                        if code != s:
                            add(code, "leak", path)
                for code, path in self._walk(s, self._edges(SYMPTOM_MEDIA.get(symptom, MEDIA)), reverse=True).items():
                    if code != s and code not in candidates:
                        add(code, "supply", list(reversed(path)))
        if not candidates and not notes:
            notes.append(f"{location} 下没有与症状 {symptom} 相关的设备或供给方")
        domains: dict[str, dict] = {}
        for c in candidates.values():
            d = domains.setdefault(c["domain"], {"count": 0, "rated_kw": 0.0})
            d["count"] += 1
            d["rated_kw"] += c["rated_kw"] or 0.0
        if symptom in LOAD_SYMPTOMS:
            key = lambda c: (-(c["rated_kw"] or 0), len(c["path"]), c["code"])  # noqa: E731
        else:  # 泄漏点、供给方、入口设备依次；同组无额定功率的管网节点排前，从源头（长路径）往下
            order = {"leak": 0, "supply": 1, "serving": 2}
            key = lambda c: (order[c["role"]], c["rated_kw"] is not None,  # noqa: E731
                             -len(c["path"]) if c["rated_kw"] is None else -c["rated_kw"], len(c["path"]), c["code"])
        ranked = sorted(candidates.values(), key=key)
        return {"location": location, "symptom": symptom, "knowledge_version": self.version, "candidates": ranked,
                "domains": dict(sorted(domains.items(), key=lambda kv: -kv[1]["rated_kw"])), **({"notes": notes} if notes else {})}

    def _zone_pressure_drop(self, zone, nodes, served, add, notes) -> None:
        """区域压降：本区气网入口（本区或服务本区、上游不在本区的气网节点）往下的支管与用气点是泄漏点候选，
        往上（含电、冷却水）是供给方。"""
        air = self._edges(("compressed_air",))
        mine = {c for a, b, _ in air for c in (a, b) if nodes[c]["zone"] == zone or zone in self._served_by(c, served)}
        if not mine:
            notes.append(f"区域 {zone} 没有压缩空气管网")
            return
        fed = {b for _, b, _ in air}
        entries = sorted(c for c in mine if not any(b == c and a in mine for a, b, _ in air))
        for e in entries:
            for code, path in self._walk(e, air).items():
                if code in fed:  # 有上游供气的支管、用气点才可能泄漏；空压机本身是供给方
                    add(code, "leak", path + [zone])
        everything = self._edges()
        for e in entries:
            if e not in fed:
                add(e, "supply", [e, zone])
            for code, path in self._walk(e, everything, reverse=True).items():
                if code != e:
                    add(code, "supply", list(reversed(path)) + [zone])

    @staticmethod
    def _served_by(code: str, served: dict) -> set[str]:
        return {t.split(":")[0] for t in served.get(code, ())}

    def action_consequence(self, action: str) -> str:
        """动作后果档位（D53，B47 分级执行用）：reversible_small / hard_to_reverse；未标注的按难逆处理。

        只追加查询，不改已有工具与判定；档位配置在 graph.yaml 的 action_consequences。
        """
        rows = self._select("SELECT ?rev ?hard WHERE { ?a pk:reversibleActions ?rev ; pk:hardToReverseActions ?hard }")
        if not rows:
            return "hard_to_reverse"
        if action in set(json.loads(rows[0]["rev"])):
            return "reversible_small"
        return "hard_to_reverse"

    @staticmethod
    def is_aggregate(ref: str) -> bool:
        """聚合对象（电梯群、空压系统）不在图谱节点里，但不算查不清。"""
        code = ref.rsplit("/", 1)[-1] if ref.startswith("entity://") else ref
        return any(f"/{kind}/" in ref or code.startswith(f"{kind}:") for kind in AGGREGATE_KINDS)

    @staticmethod
    def aggregate_members(target: str) -> tuple[str, ...]:
        """聚合对象 → 成员设备编号，复用 B30 的实体归一映射（唯一来源）；返回空表示展不开。"""
        ref = target[len("entity://"):].split("/", 1)[-1] if target.startswith("entity://") else target
        return AGGREGATE_ENTITIES.get(ref.replace(":", "/", 1), ())

    @staticmethod
    def aggregate_kind(ref: str) -> str:
        """聚合对象 → aggregate_policies 的键（elevator_group / air_system）；不是聚合对象返回空串。"""
        code = ref[len("entity://"):].split("/", 1)[-1] if ref.startswith("entity://") else ref
        kind = code.replace(":", "/", 1).split("/", 1)[0]
        return kind if kind in AGGREGATE_KINDS else ""

    def safety_tags(self, target: str, action: str, occupied: bool | None = None) -> dict:
        """确定性安全判断：动作是否碰安全标记（直接命中，或切断供给经各介质断到带标记设备）。

        实际命令先按 COMMAND_ACTIONS 归一成概念动作。区域目标只有切断类动作才把区内全部节点当直接受害；
        非切断动作按动作的目标领域限定（暖通动作只碰暖通设备与区域温度约束），没有目标领域的只在区域自身
        约束上判定。未登记动作按会改变状态处理（保守按切断类传播），只命中目标自身的安全标记，不再伪造
        unknown_action。只有目标在图谱里查不清（且不是区域或聚合对象）才整体按涉及转工单。
        聚合对象（电梯群、空压系统）命中 aggregate_policies 的 expand_on 动作时展开成员设备逐台判定
        （#240：电梯群 stop/检修查轿厢有人，空压隔离/检修保留群级），展不开成员时按涉及转工单。
        没有配置 aggregate_policies 的聚合类型沿用 #201 的切断类展开集合，不会整类 auto_ok。
        """
        nodes = self._nodes()
        meta = self._select("SELECT ?changing ?safe ?aggregates WHERE { ?m pk:stateChanging ?changing ; "
                            "pk:safeActions ?safe ; pk:aggregatePolicies ?aggregates }")[0]
        changing, safe = set(json.loads(meta["changing"])), set(json.loads(meta["safe"]))
        policies = json.loads(meta["aggregates"])
        ref = target.rsplit("/", 1)[-1] if target.startswith("entity://") else target
        result = {"target": ref, "action": action, "knowledge_version": self.version}
        zones = self._zone_labels()
        if ref.startswith("zone:"):
            ref = ref[5:]  # 也接受 zone:admin 写法
            result["target"] = ref
        if ref not in nodes and ref not in zones and not self.is_aggregate(target):
            return result | {"tags": [{"id": "unknown_target", "label": "图谱里查不到目标", "via": "unknown"}],
                             "requires_work_order": True, "verdict": "work_order"}
        canonical = normalize_action(action)
        if any(item in safe for item in canonical):
            return result | {"tags": [], "requires_work_order": False, "verdict": "auto_ok"}
        aggregate = self.is_aggregate(target)
        policy = (policies.get(self.aggregate_kind(target)) or {}) if aggregate else {}
        expand_on = policy.get("expand_on", DEFAULT_AGGREGATE_EXPAND)
        members = self.aggregate_members(target) if aggregate else ()
        if aggregate and not any(item in expand_on for item in canonical):
            return result | {"tags": [], "requires_work_order": False, "verdict": "auto_ok",
                             "notes": [f"{ref} 是聚合对象，安全标记按具体设备判定"]}
        if aggregate and not members:
            return result | {"tags": [], "requires_work_order": True, "verdict": "work_order",
                             "notes": [f"{ref} 是聚合对象，展开不到具体设备，按涉及处理"]}
        missing = [code for code in members if code not in nodes]
        if aggregate and missing:
            return result | {"tags": [], "requires_work_order": True, "verdict": "work_order",
                             "notes": [f"{ref} 是聚合对象，成员 {missing} 不在图谱，按涉及处理"]}
        unregistered = not any(item in changing for item in canonical)
        notes, hits = [], []
        if unregistered:
            notes.append(f"动作 {action} 未登记，按会改变设备状态处理；只按目标自身的安全标记判定")
        cuts = any(item in CUT_SUPPLY for item in canonical)
        spreads = cuts or unregistered  # 切断或查不清：沿全部介质往下游传播
        edges = self._edges() if spreads else []
        if aggregate:  # 切断类：展开成员设备，各自由此往下游查安全标记
            direct = set(members)
            reach = {}
            for member in sorted(direct):
                for code, path in self._walk(member, edges).items():
                    reach.setdefault(code, path)
        elif ref in zones:
            if spreads:  # 区域目标：切断类动作把区内全部节点当直接作用，再从每个节点往下游找（可跨区）
                direct = {c for c, n in nodes.items() if n["zone"] == ref}
                reach = {c: [ref, c] for c in sorted(direct)}
                # 先从区域入口（上游不在本区的节点）往下走，路径沿真实供给链；入口走不到的区内节点再补
                entries = sorted(c for c in direct if not any(b == c and a in direct for a, b, _ in edges))
                for c in [*entries, *sorted(direct - set(entries))]:
                    for code, path in self._walk(c, edges).items():
                        if code not in direct and code not in reach:
                            reach[code] = [ref, *path]
            else:  # 非切断动作：按目标领域限定区内直接作用的对象；没有领域限定的只看区域自身约束
                domains = {domain for item in canonical for domain in ACTION_DOMAINS.get(item, ())}
                direct = ({c for c, n in nodes.items() if n["zone"] == ref and n["domain"] in domains}
                          if domains else {ref})
                reach = {c: [ref, c] for c in sorted(direct)}
        else:
            reach = self._walk(ref, edges) if spreads else {ref: [ref]}
            direct = {ref}
        for tag in self._tags_on(list(reach)):
            via = "direct" if tag["node"] in direct else "supply"
            # 彻底停供的动作互相等价：标记禁止任一种，就禁止全部（检修同样断电断气）
            same_cut = any(item in FULL_CUT for item in canonical) and bool(FULL_CUT & set(tag["actions"]))
            if not ("*" in tag["actions"] or any(item in tag["actions"] for item in canonical) or same_cut or unregistered):
                continue
            if via == "direct" and tag["occupancy"] and occupied is False:
                continue
            if via == "supply" and not tag["power"]:
                continue
            hits.append(tag | {"via": via})
        return result | {"tags": _group(hits, reach), "requires_work_order": bool(hits), "verdict": "work_order" if hits else "auto_ok",
                         **({"notes": notes} if notes else {})}

    def priority_rules(self, zone: str | None = None, at: str | datetime | None = None) -> dict:
        """不传参数返回整份优先级配置；传区域与时刻返回当时生效的顺序（安全永远第一）。"""
        head = self._select("SELECT ?first ?clause ?text ?default WHERE { ?c a pk:PriorityConfig ; pk:fixedFirst ?first ; "
                            "pk:default ?default ; pk:clause ?k . ?k pk:code ?clause ; pk:text ?text }")[0]
        windows = sorted((json.loads(r["spec"]) for r in self._select(
            "SELECT ?spec WHERE { ?w a pk:PriorityWindow ; pk:spec ?spec }")), key=lambda w: w["rank"])
        default = json.loads(head["default"])
        base = {"fixed_first": head["first"], "clause": head["clause"], "clause_text": head["text"],
                "knowledge_version": self.version}
        if zone is None and at is None:
            return base | {"windows": windows, "default": default}
        if at is None:
            raise PlantkgError(ErrorCode.INVALID_REQUEST, "查当时生效的优先级要给时刻 at")
        if zone is not None and zone not in self._zone_labels():
            raise PlantkgError(ErrorCode.NOT_FOUND, f"图谱里没有区域 {zone}")
        try:
            when = datetime.fromisoformat(at) if isinstance(at, str) else at
        except ValueError:
            raise PlantkgError(ErrorCode.INVALID_REQUEST, f"时刻 {at!r} 不是 ISO 格式") from None
        tz = ZoneInfo(self.plant_tz)
        when = when.astimezone(tz) if when.tzinfo else when.replace(tzinfo=tz)
        hit = next((w for w in windows if _window_hits(w, zone, when)), default)
        return base | {"zone": zone, "at": when.isoformat(), "window": hit["id"],
                       "window_label": hit["label"], "order": [head["first"], *hit["order"]]}

    def playbooks(self, event: str | None = None, target: str | None = None) -> list[dict]:
        """应急预案。可按触发事件与目标筛选：目标是设备时按类型或所在区域匹配触发条件，是区域时按区域匹配。"""
        nodes = self._nodes() if target else {}
        zones = self._zone_labels() if target else {}
        ref = target.rsplit("/", 1)[-1] if target else None
        if target and ref not in nodes and ref not in zones:
            raise PlantkgError(ErrorCode.NOT_FOUND, f"图谱里没有 {target}")
        out = []
        for r in self._select("SELECT ?spec ?text WHERE { ?p a pk:Playbook ; pk:spec ?spec ; pk:clause/pk:text ?text }"):
            pb = json.loads(r["spec"]) | {"clause_text": r["text"], "knowledge_version": self.version}
            trig = pb["trigger"]
            if event and trig["event"] != event:
                continue
            if ref in zones and ref not in trig.get("zones", ()):
                continue
            if ref in nodes and not (nodes[ref]["type"] in trig.get("types", ()) or nodes[ref]["zone"] in trig.get("zones", ())):
                continue
            out.append(pb)
        return sorted(out, key=lambda pb: pb["id"])

    # ------------------------------------------------------------------ 辅助
    def _zone_labels(self) -> dict[str, str]:
        return {r["code"]: r["label"] for r in self._select("SELECT ?code ?label WHERE { ?z a pk:Zone ; pk:code ?code ; rdfs:label ?label }")}

    def _tags_on(self, codes: list[str]) -> list[dict]:
        """节点自身或所在区域带的安全标记。"""
        if not codes:
            return []
        values = " ".join(json.dumps(c, ensure_ascii=False) for c in codes)
        rows = self._select(
            f"SELECT ?code ?id ?label ?actions ?power ?occ ?clause ?text WHERE {{ VALUES ?code {{ {values} }} "
            "?n pk:code ?code . { ?n pk:safetyTag ?t } UNION { ?n pk:inZone ?z . ?z pk:safetyTag ?t } "
            "?t pk:code ?id ; rdfs:label ?label ; pk:actions ?actions ; pk:viaPower ?power ; pk:occupancy ?occ ; "
            "pk:clause ?k . ?k pk:code ?clause ; pk:text ?text }")
        seen, out = set(), []
        for r in sorted(rows, key=lambda r: (codes.index(r["code"]), r["id"])):
            if (r["code"], r["id"]) in seen:
                continue
            seen.add((r["code"], r["id"]))
            out.append({"node": r["code"], "id": r["id"], "label": r["label"], "actions": json.loads(r["actions"]),
                        "power": r["power"] == "true", "occupancy": r["occ"] == "true",
                        "clause": r["clause"], "clause_text": r["text"]})
        return out


def _group(tags: list[dict], paths: dict[str, list[str]]) -> list[dict]:
    """同一标记命中多个节点时合成一条：列出节点，路径给最近的一条。"""
    out: dict[tuple, dict] = {}
    for t in tags:
        if t["id"] not in out:
            out[t["id"]] = {k: v for k, v in t.items() if k != "node"} | {"nodes": [], "path": paths.get(t["node"], [t["node"]])}
        elif t.get("via") == "direct":  # 同一标记既直接又间接命中时按直接报
            out[t["id"]] |= {"via": "direct", "path": paths.get(t["node"], [t["node"]])}
        out[t["id"]]["nodes"].append(t["node"])
    return list(out.values())


def _window_hits(w: dict, zone: str | None, when: datetime) -> bool:
    """不给区域时只匹配全厂窗口（zones: "*"）。"""
    if w["zones"] != "*" and zone not in w["zones"]:
        return False
    if w["days"] == "weekday" and when.weekday() >= 5:
        return False
    now = when.time()
    for span in w["times"]:
        lo, hi = (time.fromisoformat(t) for t in span.split("-"))
        if (lo <= now < hi) if lo < hi else (now >= lo or now < hi):
            return True
    return False


def _read_site(site_yaml: Path) -> SimpleNamespace:
    """读 B18 的 site.yaml（只读配置，不导入 world 平面）；逐层设备的展开规则同 SiteModel.load。
    拓扑连通等校验由 B18 负责，这里只在引用不存在的节点时报错。"""
    data = yaml.safe_load(site_yaml.read_text(encoding="utf-8"))
    zones = data["zones"]
    nodes = {n["id"]: SimpleNamespace(id=n["id"], type=n["type"], label=n["label"], parent=n.get("parent"), zone=n.get("zone"))
             for n in data["distribution"]["nodes"]}
    equipment = []
    for item in data["equipment"]:
        per_floor = item.get("per_floor")
        for floor in range(1, zones[item["zone"]]["floors"] + 1) if per_floor else [item.get("floor")]:
            equipment.append(SimpleNamespace(
                code=f"{item['code']}{floor:02d}" if per_floor else item["code"], type=item["type"],
                label=f"{item['label']}（{floor} 层）" if per_floor else item["label"], zone=item["zone"], panel=item["panel"],
                rated_kw=float(item["rated_kw"]), load=item["load"], floor=floor, ontology_class=item.get("ontology_class")))
    for e in equipment:
        if e.panel not in nodes:
            raise ValueError(f"{e.code} 接入的配电柜 {e.panel} 不存在")
    return SimpleNamespace(plant_id=data["plant_id"], plant_tz=data["plant_tz"], zones=zones, nodes=nodes, equipment=equipment)


class _Builder:
    """把 site.yaml 与图谱配置展开成三元组。只在 load 时用一次。"""

    def __init__(self, site: SimpleNamespace, cfg: dict, regs: dict):
        self.site, self.cfg, self.regs = site, cfg, regs
        self.out: list[tuple] = []
        self.domain_of = {t: d for d, types in cfg["domains"].items() for t in types}
        self.iri: dict[str, NamedNode] = {}
        self.zone_of: dict[str, str] = {}
        self.type_of: dict[str, str] = {}
        self.floor_of: dict[str, int | None] = {}

    def add(self, s, p, o):
        self.out.append((s, _p(p) if isinstance(p, str) else p, o if isinstance(o, (NamedNode, Literal)) else Literal(o)))

    def node(self, code, type_, label, zone=None, rated_kw=None, floor=None, nature="configured", brick=None):
        n = NamedNode(f"entity://{self.site.plant_id}/{type_}/{code}")
        self.iri[code], self.type_of[code], self.zone_of[code], self.floor_of[code] = n, type_, zone, floor
        for p, o in (("code", code), (RDFS_LABEL, label), ("type", type_), ("domain", self.domain_of.get(type_, "other")),
                     ("nature", nature)):
            self.add(n, p, o)
        self.add(n, RDF_TYPE, _p("Node"))
        if brick:
            self.add(n, RDF_TYPE, NamedNode(BRICK + brick.split(":", 1)[1]))
        if zone:
            self.add(n, "inZone", self.zone(zone))
        if rated_kw is not None:
            self.add(n, "ratedKw", Literal(str(rated_kw), datatype=NamedNode("http://www.w3.org/2001/XMLSchema#double")))
        if floor is not None:
            self.add(n, "onFloor", self.floor(zone, floor))
        return n

    def zone(self, z):
        return NamedNode(f"entity://{self.site.plant_id}/zone/{z}")

    def floor(self, zone, f):
        return NamedNode(f"entity://{self.site.plant_id}/floor/{zone}-F{f:02d}")

    def expand(self, ref) -> list[str]:
        refs = ref if isinstance(ref, list) else [ref]
        out = [c for r in refs for c in (sorted(fnmatch.filter(self.iri, r)) if "*" in r else [r])]
        missing = [c for c in out if c not in self.iri]
        if missing or not out:
            raise ValueError(f"图谱配置引用了不存在的节点：{missing or refs}")
        return out

    def clause(self, cid):
        if cid not in self.regs["clauses"]:
            raise ValueError(f"条款 {cid} 不在 regulations.yaml")
        return NamedNode(f"{PK}clause/{cid}")

    def quads(self) -> list[tuple]:
        site, cfg = self.site, self.cfg
        for cid, text in self.regs["clauses"].items():
            c = self.clause(cid)
            self.add(c, RDF_TYPE, _p("Clause"))
            for p, o in (("code", cid), ("text", text), ("document", self.regs["document"]), ("nature", self.regs["nature"])):
                self.add(c, p, o)
        for z, info in site.zones.items():
            self.add(self.zone(z), RDF_TYPE, _p("Zone"))
            self.add(self.zone(z), "code", z)
            self.add(self.zone(z), RDFS_LABEL, info["label"])
            for f in range(1, info["floors"] + 1) if info["floors"] > 1 else ():
                fl = self.floor(z, f)
                self.add(fl, "code", f"{z}:{f}")
                self.add(fl, RDFS_LABEL, f"{info['label']} {f} 层")
                self.add(fl, "partOf", self.zone(z))
        for n in site.nodes.values():
            self.node(n.id, n.type, n.label, zone=n.zone)
        for e in site.equipment:
            n = self.node(e.code, e.type, e.label, e.zone, e.rated_kw, e.floor, brick=e.ontology_class)
            if e.load == "none":
                self.add(n, "supplyOnly", True)  # 光伏、储能：电源侧，不算突增来源
        extra_panels = {}
        for n in cfg["nodes"]:
            self.node(n["code"], n["type"], n["label"], n.get("zone"), nature="synthetic")
            if n.get("panel"):
                extra_panels[n["code"]] = n["panel"]
        # 介质供给：电力来自 B18 配电树
        for n in site.nodes.values():
            if n.parent:
                self.add(self.iri[n.parent], "feeds_power", self.iri[n.id])
        for e in site.equipment:
            self.add(self.iri[e.panel], "feeds_power", self.iri[e.code])
        for code, panel in extra_panels.items():
            self.add(self.iri[self.expand(panel)[0]], "feeds_power", self.iri[code])
        for medium, links in cfg["links"].items():
            if medium not in MEDIA:
                raise ValueError(f"未知介质 {medium}")
            for up, down in links:
                for a in self.expand(up):
                    for b in self.expand(down):
                        self.add(self.iri[a], f"feeds_{medium}", self.iri[b])
        for ref, targets in cfg["serves"].items():
            for code in self.expand(ref):
                for t in targets:
                    if t == "floor":
                        self.add(self.iri[code], "serves", self.floor(self.zone_of[code], self.floor_of[code]))
                    elif ":" in t:
                        for z, f in _floors(t):
                            self.add(self.iri[code], "serves", self.floor(z, f))
                    else:
                        self.add(self.iri[code], "serves", self.zone(t))
        # 安全标记：挂在命中的区域或节点上
        for tag in cfg["safety_tags"]:
            t = NamedNode(f"{PK}tag/{tag['id']}")
            for p, o in (("code", tag["id"]), (RDFS_LABEL, tag["label"]), ("actions", json.dumps(tag["actions"])),
                         ("viaPower", bool(tag.get("power"))), ("occupancy", bool(tag.get("occupancy"))),
                         ("clause", self.clause(tag["clause"]))):
                self.add(t, p, o)
            self.add(t, RDF_TYPE, _p("SafetyTag"))
            for z in tag["match"].get("zones", ()):
                self.add(self.zone(z), "safetyTag", t)
            for code, type_ in self.type_of.items():
                if type_ in tag["match"].get("types", ()) or code in tag["match"].get("codes", ()):
                    self.add(self.iri[code], "safetyTag", t)
        meta = _p("ActionPolicy")
        self.add(meta, "stateChanging", json.dumps(cfg["state_changing_actions"]))
        self.add(meta, "safeActions", json.dumps(cfg["safe_actions"]))
        self.add(meta, "aggregatePolicies", json.dumps(cfg.get("aggregate_policies") or {}, ensure_ascii=False))
        # D53 动作后果档位（B47）：可选配置，缺省时 action_consequence 一律按难逆处理
        consequences = cfg.get("action_consequences") or {}
        self.add(meta, "reversibleActions", json.dumps(consequences.get("reversible_small", [])))
        self.add(meta, "hardToReverseActions", json.dumps(consequences.get("hard_to_reverse", [])))
        pr = cfg["priority"]
        pc = _p("priority")
        self.add(pc, RDF_TYPE, _p("PriorityConfig"))
        for p, o in (("fixedFirst", pr["fixed_first"]), ("clause", self.clause(pr["clause"])),
                     ("default", json.dumps(pr["default"], ensure_ascii=False)), ("knowledgeVersion", cfg["knowledge_version"])):
            self.add(pc, p, o)
        for rank, w in enumerate(pr["windows"]):
            wn = NamedNode(f"{PK}priority/{w['id']}")
            self.add(wn, RDF_TYPE, _p("PriorityWindow"))
            self.add(wn, "spec", json.dumps(w | {"rank": rank}, ensure_ascii=False))
            self.add(pc, "window", wn)
        for pb in cfg["playbooks"]:
            validate_playbook_spec(pb, set(cfg["state_changing_actions"]) | set(cfg["safe_actions"]), f"图谱 {pb.get('id')}：")
            pn = NamedNode(f"{PK}playbook/{pb['id']}")
            self.add(pn, RDF_TYPE, _p("Playbook"))
            self.add(pn, RDFS_LABEL, pb["label"])
            self.add(pn, "spec", json.dumps(pb, ensure_ascii=False))
            self.add(pn, "clause", self.clause(pb["clause"]))
        return self.out
