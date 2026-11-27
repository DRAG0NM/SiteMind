"""B30 工具登记：读取 B06 原文快照，候选持久化，复核经 B04 确认。"""
from __future__ import annotations

import base64
import hashlib
import json
from datetime import datetime

from plantkg.core.contracts import Actor, AssetFragment, ErrorCode, Locator, OntologyCandidate, PlantkgError, Role, Rule, RuleCandidate, ToolResult
from plantkg.core.ports import AssetContentReader, AssetRepository, GraphStore, Judge, KnowledgePublisher, OntologyBuilder, PendingActionStore, RuleExtractor, RunStore, VersionStore
from plantkg.knowledge.rules.compile import compile_reviewed_rule
from plantkg.knowledge.rules.normalise import consumer_mismatch, same_access, zone_hook_for
from plantkg.knowledge.rules.publisher import candidate_for
from plantkg.knowledge.ontology.builder import DEFINITIONS, OPTIONS, vector_recall
from plantkg.knowledge.rules.triage import assess
from plantkg.knowledge.rules.runtime import rules_for_manifest


class ReviewHandler:
    kind = "review_decision"

    def __init__(self, candidates, templates, builder):
        self.candidates, self.templates, self.builder = candidates, templates, builder

    def apply(self, action, actor):
        if action.status != "confirmed" or Role.reviewer not in actor.roles:
            raise PlantkgError(ErrorCode.FORBIDDEN, "复核决定须由复核员确认")
        decision = {"action_id": action.action_id, "decision": action.payload["decision"], "reviewer": actor.actor_id,
                    "reason": action.reason, "revision": action.edits or action.payload.get("revision")}
        review_id = action.payload["review_id"]
        if self.builder is not None and review_id in self.builder.candidates:
            if decision["decision"] == "reject":
                self.builder.reject(review_id, actor)
            else:
                self.builder.review(review_id, (decision["revision"] or {}).get("choice"), actor, action_id=action.action_id)
            return ToolResult(ok=True, business_status="done", data={"review_id": review_id, "decision": decision["decision"]})
        self.candidates.decide(action.payload["review_id"], decision)
        if decision["decision"] in ("accept", "revise"):
            candidate = self.candidates.get(action.payload["review_id"])
            revision = decision["revision"] or {}
            label = {"compressor_pressure_drop": OPTIONS["constraint"][1],
                     "zone_temperature_limit": OPTIONS["constraint"][0],
                     "elevator_congestion_rule": OPTIONS["constraint"][3]}.get(candidate.draft.get("kind"))
            self.templates.record(candidate.extractor_version, candidate, label or "unlabeled", actor,
                                  alias=revision.get("alias"), field_map=revision,
                                  judge_result=(self.candidates.route(candidate.candidate_id) or {}).get("judge") if label else None)
        return ToolResult(ok=True, business_status="done", data={"review_id": action.payload["review_id"], "decision": decision["decision"]})


_REVIEW_PRIORITY = {"uncalibrated": 3, "violation": 2, "scored": 1, "incomparable": 0}


def _review_key(candidate_id: str, score: dict) -> tuple[int, float, str]:
    """复核队列稳定排序键：优先级、得分降序，候选编号升序（#145）。"""
    return -_REVIEW_PRIORITY[score["status"]], -float(score.get("value") or 0), candidate_id


_CURSOR_STALE = "游标已失效，请从第一页重新取"


def _filter_digest(filters: dict) -> str:
    """筛选参数摘要：游标只对同一组筛选条件有效（#205-4）。"""
    body = json.dumps(filters, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _encode_cursor(key, manifest_id, filter_digest) -> str:
    """游标编码清单与筛选摘要，key 是下一页的起点（#205-4）。"""
    raw = json.dumps({"v": 2, "manifest_id": manifest_id, "filter_digest": filter_digest, "key": key},
                     ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str, manifest_id, filter_digest):
    """解析并核对游标绑定的清单与筛选；旧格式或已失效一律报“从第一页重新取”。"""
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
    except (ValueError, TypeError, UnicodeDecodeError):
        raise PlantkgError(ErrorCode.INVALID_REQUEST, _CURSOR_STALE) from None
    if (not isinstance(payload, dict) or payload.get("v") != 2
            or payload.get("manifest_id") != manifest_id or payload.get("filter_digest") != filter_digest):
        raise PlantkgError(ErrorCode.INVALID_REQUEST, _CURSOR_STALE)
    return payload.get("key")


def install_tools(registry, candidates, templates, artifacts, tariff_category=None):
    assets = registry.resolve(AssetRepository)
    reader = registry.resolve(AssetContentReader)
    extractor = registry.resolve(RuleExtractor)
    actions = registry.resolve(PendingActionStore)
    builder = registry.ports.get(OntologyBuilder)

    def ontology_visible(context, candidate):
        for evidence in candidate.evidence:
            if evidence.asset_version_id:
                assets.get(context, evidence.asset_version_id)

    def extract_rules(context, asset_version_id, rule_kind=None):
        asset = assets.get(context, asset_version_id)
        raw = reader.read_content(context, asset_version_id)
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise PlantkgError(ErrorCode.INVALID_REQUEST, "当前模板只处理 UTF-8 文本资料") from None
        fragment = AssetFragment(fragment_id="asset-" + asset.content_hash[7:23], asset_version_id=asset.asset_version_id,
                                 locator=Locator(line_start=1), kind="text", text=text, available_at=asset.available_at)
        extracted = extractor.extract(context, [fragment])
        if rule_kind:
            extracted = [c for c in extracted if c.draft.get("kind") == rule_kind]
        if not extracted and not rule_kind:
            extracted = [RuleCandidate(candidate_id="unhandled-" + asset.content_hash[7:23],
                         draft={"kind": "unhandled_document", "source_text": text[:500],
                                "available_at": asset.available_at.isoformat()},
                         extractor_version="unhandled-v1", source_asset_version=asset.asset_version_id,
                         locator=Locator(line_start=1), confidence=None)]
        for item in extracted:
            candidates.save(item)
            candidates.record_route(item.candidate_id, assess(registry, artifacts, templates.load(), context, item))
        prewarm = registry.hooks_for("evolution.prewarm_baseline")
        if prewarm and extracted:  # B32 发布只跑候选一侧：规则上传时按当前版本预生成快回测基准摘要
            try:
                versions = registry.resolve(VersionStore)
                prewarm[-1](None, context,
                            base_manifest_id=context.version_manifest_id or versions.active().version_manifest_id)
            except Exception:  # noqa: BLE001 — 预热失败不挡资料抽取，发布时仍会现跑基准
                pass
        return ToolResult(ok=True, business_status="done", data={"candidate_ids": [c.candidate_id for c in extracted],
                    "routing": [{"candidate_id": c.candidate_id, "stage": candidates.route(c.candidate_id)["stage"],
                                 "reasons": candidates.route(c.candidate_id)["reasons"]} for c in extracted],
                    "count": len(extracted), "published": False},
                          notice="模板只覆盖部分条文；未识别内容需由资料入库 Agent 检查")

    def next_review_batch(context, cursor=None, page_size=None):
        try:
            size = int(page_size or 10)
        except (TypeError, ValueError):
            raise PlantkgError(ErrorCode.INVALID_REQUEST, "复核分页参数无效") from None
        if not 1 <= size <= 10:
            raise PlantkgError(ErrorCode.INVALID_REQUEST, "复核分页参数无效")
        manifest_id = context.version_manifest_id
        digest = _filter_digest({})
        start = None
        if cursor not in (None, ""):
            key = _decode_cursor(str(cursor), manifest_id, digest)
            if (not isinstance(key, list) or len(key) != 3 or isinstance(key[0], bool) or isinstance(key[1], bool)
                    or not isinstance(key[0], int) or not isinstance(key[1], (int, float)) or not isinstance(key[2], str)):
                raise PlantkgError(ErrorCode.INVALID_REQUEST, _CURSOR_STALE)
            start = (key[0], float(key[1]), key[2])
        visible = []
        for item, score in candidates.ranked_pending(context.version_manifest_id):
            try:
                assets.get(context, item.source_asset_version)
            except PlantkgError as exc:
                if exc.code == ErrorCode.NOT_VISIBLE:
                    continue
                raise
            visible.append((item, score))
        if builder is not None:
            if context.plant_id != builder.plant_id:
                raise PlantkgError(ErrorCode.CROSS_PLANT, "候选不属于当前厂区")
            for cid, item in builder.candidates.items():
                if cid in builder.approved or cid in builder.rejected:
                    continue
                try:
                    ontology_visible(context, item)
                except PlantkgError as exc:
                    if exc.code == ErrorCode.NOT_VISIBLE:
                        continue
                    raise
                visible.append((item, {"status": "uncalibrated", "value": None}))
        visible.sort(key=lambda pair: _review_key(pair[0].candidate_id, pair[1]))
        if start is not None:
            visible = [pair for pair in visible if _review_key(pair[0].candidate_id, pair[1]) > start]
        items = visible[:size]
        def summary(c, score):
            if isinstance(c, OntologyCandidate):
                return {"candidate_id": c.candidate_id, "layer": c.layer, "subject": c.subject,
                        "options": list(c.options), "score": score}
            return {"candidate_id": c.candidate_id,
                    "source_asset_version": c.source_asset_version, "locator": c.locator.model_dump(mode="json", exclude_none=True),
                    "kind": c.draft.get("kind"), "excerpt": str(c.draft.get("source_text", ""))[:200], "score": score,
                    "routing": {k: v for k, v in (candidates.route(c.candidate_id) or {}).items() if k != "judge"}}
        next_cursor = (_encode_cursor(list(_review_key(items[-1][0].candidate_id, items[-1][1])), manifest_id, digest)
                       if len(visible) > size else None)
        return ToolResult(ok=True, business_status="done", data={"items": [summary(c, score) for c, score in items]},
                          page={"page_size": size, "next_cursor": next_cursor})

    def propose_review_decision(context, review_id, decision, revision=None, rationale=None):
        if decision not in ("accept", "revise", "reject") or not rationale:
            raise PlantkgError(ErrorCode.INVALID_REQUEST, "复核建议必须给结论和理由")
        if builder is not None and review_id in builder.candidates:
            candidate = builder.candidates[review_id]
            ontology_visible(context, candidate)
            if context.plant_id != builder.plant_id:
                raise PlantkgError(ErrorCode.CROSS_PLANT, "候选不属于当前厂区")
            if decision != "reject" and (revision or {}).get("choice") not in candidate.options:
                raise PlantkgError(ErrorCode.INVALID_REQUEST, "本体复核需要选择候选中的类型或关系")
        else:
            candidate = candidates.get(review_id)
            assets.get(context, candidate.source_asset_version)
        action = actions.propose("review_decision", {"review_id": review_id, "target_ref": review_id,
                                  "decision": decision, "revision": revision, "rationale": rationale}, context)
        return ToolResult(ok=True, business_status="done", data={"action_id": action.action_id, "status": action.status})

    def propose_publish(context, candidate_id, rationale):
        if not rationale:
            raise PlantkgError(ErrorCode.INVALID_REQUEST, "发布提议需要理由")
        evolvers = registry.hooks_for("evolution.propose_publish")
        if evolvers:
            action = evolvers[-1](context, candidate_id, rationale)
            if action is not None:
                return ToolResult(ok=True, business_status="done", data={"action_id": action.action_id,
                                                                             "status": action.status,
                                                                             "candidate_id": candidate_id})
        if builder is not None and candidate_id in builder.candidates:
            action_id = builder.review_actions.get(candidate_id)
            if not action_id or actions.get(action_id).status != "confirmed":
                raise PlantkgError(ErrorCode.PRECONDITION, "本体候选尚未完成人工复核")
            for reviewed_id in builder.approved:
                ontology_visible(context, builder.candidates[reviewed_id])
                reviewed_action = builder.review_actions.get(reviewed_id)
                if not reviewed_action or actions.get(reviewed_action).status != "confirmed":
                    raise PlantkgError(ErrorCode.PRECONDITION, "命名图包含未确认的本体候选")
            review = actions.get(action_id)
            reviewer = Actor(actor_id=review.decided_by, roles=(Role.reviewer,))
            versions = registry.resolve(VersionStore)
            base = versions.active()
            if context.version_manifest_id and context.version_manifest_id != base.version_manifest_id:
                raise PlantkgError(ErrorCode.STATE_CONFLICT, "当前运行绑定的知识版本不是最新活动版本")
            component = base.components.get("rules")
            if component and component.ref.startswith("b30-rules:"):
                existing = [Rule.model_validate(r) for r in artifacts.load(component)["rules"]]
            elif component and component.ref != "scenario-knowledge:empty":
                raise PlantkgError(ErrorCode.PRECONDITION, "旧规则格式不能合并，须先迁移或人工复核完整规则集")
            else:
                existing = []
            graph = builder.prepare_graph(context, reviewer)
            ref = artifacts.prepare(existing, reviewer, ontology=graph)
            proposal = candidate_for(ref, base.version_manifest_id, candidate_id, ontology=graph)
            evolvers = registry.hooks_for("evolution.propose_publish_candidate")
            if evolvers:
                action = evolvers[-1](context, proposal, rationale)
                return ToolResult(ok=True, business_status="done", data={"action_id": action.action_id,
                                                                              "status": action.status,
                                                                              "candidate_id": candidate_id})
            publisher = registry.resolve(KnowledgePublisher)
            if hasattr(publisher, "with_context"):
                publisher = publisher.with_context(context)
            action = publisher.propose_publish(proposal)
            return ToolResult(ok=True, business_status="done", data={"action_id": action.action_id, "status": action.status,
                                                                      "candidate_id": candidate_id},
                              notice="B30 兜底不回测；发布仍需复核员确认")
        decision = candidates.decision(candidate_id)
        if not decision or decision["decision"] not in ("accept", "revise"):
            raise PlantkgError(ErrorCode.PRECONDITION, "候选尚未通过人工复核")
        review = actions.get(decision["action_id"])
        details = decision.get("revision") or {}
        if review.status != "confirmed" or not details.get("subject_id") or not details.get("effective_from"):
            raise PlantkgError(ErrorCode.PRECONDITION, "复核确认须给适用实体和生效时间")
        candidate = candidates.get(candidate_id)
        asset = assets.get(context, candidate.source_asset_version)
        reviewer = Actor(actor_id=review.decided_by, roles=(Role.reviewer,))
        try:
            effective_from = datetime.fromisoformat(details["effective_from"].replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            raise PlantkgError(ErrorCode.INVALID_REQUEST, "生效时间须为 UTC 时间") from None
        rule = compile_reviewed_rule(candidates, actions, candidate_id, reviewer, subject_id=details["subject_id"],
                                     effective_from=effective_from, nature=asset.nature)
        standard_ref, standard_locator = details.get("standard_ref"), details.get("standard_locator")
        if standard_ref or standard_locator:
            if not isinstance(standard_ref, str) or not isinstance(standard_locator, str) or not standard_locator.strip():
                raise PlantkgError(ErrorCode.PRECONDITION, "标准对齐须同时提供资料版本和原文条款定位")
            standard = assets.get(context, standard_ref)
            rule = rule.model_copy(update={"condition": rule.condition | {"standard_alignment": {
                "asset_version_id": standard_ref, "locator": standard_locator}},
                "available_at": max(rule.available_at, standard.available_at)})
        if rule.kind == "tariff_tou":
            refs = rule.condition.get("calendar_refs")
            if not isinstance(refs, list) or not refs or not all(isinstance(ref, str) for ref in refs):
                raise PlantkgError(ErrorCode.PRECONDITION, "节日日历须引用可见的资料版本")
            calendar_assets = [assets.get(context, ref) for ref in refs]
            rule = rule.model_copy(update={"available_at": max([rule.available_at] + [source.available_at for source in calendar_assets])})
            validators = registry.hooks_for("knowledge.validate.tariff")
            if not validators:
                raise PlantkgError(ErrorCode.DEPENDENCY_UNAVAILABLE, "缺少电价时段表校验器")
            issue = validators[-1](rule)
            if isinstance(issue, tuple):
                raise PlantkgError(ErrorCode.PRECONDITION, "电价时段表未通过校验：" + str(issue[1]))
        versions = registry.resolve(VersionStore)
        base = versions.active()
        if context.version_manifest_id and context.version_manifest_id != base.version_manifest_id:
            raise PlantkgError(ErrorCode.STATE_CONFLICT, "当前运行绑定的知识版本不是最新活动版本")
        component = base.components.get("rules")
        old = []
        if component and component.ref.startswith("b30-rules:"):
            old = [Rule.model_validate(r) for r in artifacts.load(component)["rules"]]
        elif component and component.ref != "scenario-knowledge:empty":
            raise PlantkgError(ErrorCode.PRECONDITION, "旧规则格式不能合并，须先迁移或人工复核完整规则集")
        # 发布前按 kind 校验消费端契约（#143/#144）：失败记 consumer_mismatch 并拒绝
        entity_type = (registry.hooks_for("router.entity_type") or (None,))[-1]
        zone_of = zone_hook_for(rule, registry)
        mismatch = consumer_mismatch(rule, entity_type, zone_of, tariff_category)
        if mismatch is not None:
            route = candidates.route(candidate_id) or {}
            reasons = list(route.get("reasons") or ())
            if "consumer_mismatch" not in reasons:
                candidates.record_route(candidate_id, {**route, "reasons": reasons + ["consumer_mismatch"],
                                                       "consumer_mismatch": mismatch})
            raise PlantkgError(ErrorCode.PRECONDITION, "consumer_mismatch：" + mismatch)
        retained = [old_rule for old_rule in old if not same_access(old_rule, rule)]
        ref = artifacts.prepare(retained + [rule], reviewer)
        proposal = candidate_for(ref, base.version_manifest_id, candidate_id)
        evolvers = registry.hooks_for("evolution.propose_publish_candidate")
        if evolvers:
            action = evolvers[-1](context, proposal, rationale)
            return ToolResult(ok=True, business_status="done", data={"action_id": action.action_id,
                                                                          "status": action.status,
                                                                          "candidate_id": candidate_id})
        publisher = registry.resolve(KnowledgePublisher)
        if hasattr(publisher, "with_context"):
            publisher = publisher.with_context(context)
        action = publisher.propose_publish(proposal)
        return ToolResult(ok=True, business_status="done", data={"action_id": action.action_id, "status": action.status,
                                                                  "candidate_id": candidate_id},
                          notice="B30 兜底不回测；发布仍需复核员确认")

    for name, handler in (("extract_rules", extract_rules), ("next_review_batch", next_review_batch),
                          ("propose_review_decision", propose_review_decision)):
        registry.register_tool(name, handler)
    registry.register_tool("propose_publish", propose_publish, default=True)
    registry.register_action_handler("review_decision", ReviewHandler(candidates, templates, builder), default=True)

    def lookup_rules(context, subject_id=None, kind=None, cursor=None, page_size=None):
        if not context.version_manifest_id:
            raise PlantkgError(ErrorCode.PRECONDITION, "查询规则须绑定知识版本")
        manifest_id = context.version_manifest_id
        as_of = context.as_of
        view = {"as_of": as_of.isoformat()}
        if context.run_id:
            run = registry.resolve(RunStore).get_run(context.run_id)
            if run.version_manifest_id != context.version_manifest_id:
                raise PlantkgError(ErrorCode.PRECONDITION, "运行与知识版本不一致")
            if run.forked_at:
                as_of = max(as_of, run.forked_at)
            # 运行绑定的视图由 run_id + forked_at 决定（as_of 随调用推进，不能进游标）
            view = {"run_id": context.run_id, "forked_at": run.forked_at.isoformat() if run.forked_at else None}
        # 游标同时绑定清单、筛选与 as_of 视图：as_of 变了，旧游标失效，从第一页重取
        digest = _filter_digest({"subject_id": subject_id, "kind": kind, **view})
        offset = 0
        if cursor not in (None, ""):
            key = _decode_cursor(str(cursor), manifest_id, digest)
            if isinstance(key, bool) or not isinstance(key, int) or key < 0:
                raise PlantkgError(ErrorCode.INVALID_REQUEST, _CURSOR_STALE)
            offset = key
        size = min(int(page_size or 10), 10)
        if size < 1:
            raise PlantkgError(ErrorCode.INVALID_REQUEST, "分页参数无效")
        rules = rules_for_manifest(registry.resolve(VersionStore), artifacts, context.version_manifest_id, as_of, as_of)
        rules = [r for r in rules if (not subject_id or subject_id in r.subject_ids) and (not kind or kind == r.kind)]
        result = rules[offset:offset + size]
        next_cursor = _encode_cursor(offset + size, manifest_id, digest) if offset + size < len(rules) else None
        return ToolResult(ok=True, business_status="done", version_manifest_id=context.version_manifest_id,
                          data={"items": [r.model_dump(mode="json") for r in result]},
                          page={"page_size": size, "next_cursor": next_cursor})

    registry.register_tool("lookup_rules", lookup_rules)

    if OntologyBuilder in registry.ports:
        builder = registry.resolve(OntologyBuilder)

        def propose_candidates(context, subject_ids, layer):
            result = builder.propose_candidates(context, subject_ids, layer)
            return ToolResult(ok=True, business_status="done", data={"items": [c.model_dump(mode="json") for c in result]})

        def link_entities(context, subject_id, target_id, relation):
            result = builder.link_entities(context, subject_id, target_id, relation)
            return ToolResult(ok=True, business_status="done", data={"item": result.model_dump(mode="json")})

        def judge_candidates(context, candidate_ids):
            judge = registry.resolve(Judge)
            results = []
            for candidate_id in candidate_ids:
                candidate = builder.candidates.get(candidate_id)
                if candidate is None:
                    raise PlantkgError(ErrorCode.NOT_FOUND, "本体候选不存在")
                top, scores = vector_recall(candidate.subject, candidate.options)
                state = {"content": candidate.subject, "definitions": {o: DEFINITIONS[o] for o in candidate.options}, "coverage": 0.0}
                if candidate.recall_source == "vector" and top:
                    state.update(vector_top=top, vector_scores=scores)
                results.append(judge.decide(state, candidate, candidate.options).model_dump(mode="json"))
            return ToolResult(ok=True, business_status="done", data={"items": results})

        def graph_name(context):
            if not context.version_manifest_id:
                raise PlantkgError(ErrorCode.PRECONDITION, "查询本体须绑定知识版本")
            manifest = registry.resolve(VersionStore).get(context.version_manifest_id)
            component = manifest.components.get("ontology")
            if not manifest.ever_active or not component or not component.ref.startswith("graph:v1:"):
                raise PlantkgError(ErrorCode.NOT_VISIBLE, "该版本没有已发布的 B30 本体图")
            return "b30-" + component.content_hash.removeprefix("sha256:")[:24]

        def graph_query(context, query_id, params=None, cursor=None, page_size=None):
            size = min(int(page_size or 50), 200)
            rows, next_cursor = registry.resolve(GraphStore).read_named_graph(context, graph_name(context), query_id,
                                                                                (params or {}) | {"page_size": size}, cursor)
            return ToolResult(ok=True, business_status="done", version_manifest_id=context.version_manifest_id,
                              data={"items": list(rows)}, page={"page_size": size, "next_cursor": next_cursor})

        def validate(context, graph=None):
            name = graph_name(context)
            if graph and graph != name:
                raise PlantkgError(ErrorCode.NOT_VISIBLE, "只能验证当前版本绑定的命名图")
            issues = builder.validate(context, name)
            return ToolResult(ok=True, business_status="done", data={"issues": [i.model_dump(mode="json") for i in issues],
                                                                       "profile": "SHACL Core: targetClass/path/minCount"})

        def snapshot(context):
            return ToolResult(ok=True, business_status="done", version_manifest_id=context.version_manifest_id,
                              data={"manifest_hash": builder.snapshot(context)})

        def diff(context, from_version, to_version):
            versions = registry.resolve(VersionStore)
            old, new = versions.get(from_version), versions.get(to_version)
            if not old.ever_active or not new.ever_active:
                raise PlantkgError(ErrorCode.NOT_VISIBLE, "只能比较已发布过的版本")
            names = set(old.components) | set(new.components)
            changes = {name: {"before": old.components[name].ref if name in old.components else None,
                              "after": new.components[name].ref if name in new.components else None}
                       for name in names if old.components.get(name) != new.components.get(name)}
            return ToolResult(ok=True, business_status="done", data={"from_version": from_version, "to_version": to_version,
                                                                       "changes": changes})

        for name, handler in (("propose_candidates", propose_candidates), ("link_entities", link_entities),
                              ("judge_candidates", judge_candidates), ("graph_query", graph_query), ("validate", validate),
                              ("snapshot", snapshot), ("diff", diff)):
            registry.register_tool(name, handler)
