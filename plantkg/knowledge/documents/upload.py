"""受控的本地资料入库；原文和版本仍由 B06 保存。"""
from __future__ import annotations

import json
import logging
import os
import re
import secrets
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from plantkg.core.contracts import (AssetFragment, Context, DocumentIngestReceipt, DocumentIngestRequest,
                                    DocumentSearchHit, ErrorCode, EvidenceRef, Locator, Page, PlantkgError)
from plantkg.core.ports import AssetRepository, PendingActionStore, VersionStore
from plantkg.core.versions import atomic, digest, read
from plantkg.knowledge.documents.retrieval import tokens
from plantkg.knowledge.documents.spec import DocumentSpec, LEVEL_RANK, SourceLevel

log = logging.getLogger(__name__)


def mentions(text, name):
    return bool(name and re.search(r'(?<![A-Za-z0-9_-])' + re.escape(name) + r'(?![A-Za-z0-9_-])', text))


def at_time(context, query):
    if not query.get('as_of'):
        return context
    try:
        when = datetime.fromisoformat(query['as_of'].replace('Z', '+00:00'))
        if when.tzinfo is None or when > datetime.now(timezone.utc):
            raise ValueError
        return context.model_copy(update={'as_of': when.astimezone(timezone.utc)})
    except ValueError:
        raise PlantkgError(ErrorCode.INVALID_REQUEST, 'as_of 须为不晚于现在的带时区时间') from None


class UploadService:
    def __init__(self, registry, writer, auth, settings):
        self.registry, self.writer, self.auth, self.settings = registry, writer, auth, settings
        self.catalog = registry.resolve(AssetRepository)
        self.root = Path(writer.path).parent / "document_sources" / digest(settings.plant_id)
        self.lock = threading.RLock()
        for get_library in registry.hooks_for('knowledge.documents.library'):
            library = get_library()
            library.sources.append(self.documents)
            library.content_readers.append(self.document_chunks)
        registry.register_hook('evolution.authorization_published', self.published)

    def published(self, base_manifest, manifest_id, category, action_id, at):
        """B32 已激活清单的通知；只推进本模块上传资料，不影响授权类别。"""
        action = self.registry.resolve(PendingActionStore).get(action_id)
        rows = read(self.writer, lambda con: con.execute(
            'SELECT receipt FROM b26_uploads WHERE plant_id=?', (self.settings.plant_id,)).fetchall())
        context = Context(plant_id=self.settings.plant_id, industry_pack=self.settings.industry_pack,
            actor={'actor_id': 'document-publication', 'roles': ['reviewer']}, channel='rest',
            trace_id=action_id, as_of=at)
        for row in rows:
            receipt = DocumentIngestReceipt.model_validate_json(row[0])
            if action.target_ref in (*receipt.candidate_ids, *receipt.ontology_candidate_ids):
                self.catalog.record_activation(context, receipt.asset_version_id, 'in_graph',
                                               ref=manifest_id, available_at=at)

    def ingest(self, context, path, query, *, body, headers):
        actor = self.auth.authenticate(headers)
        request = DocumentIngestRequest.model_validate(body)
        try:
            content = request.text.encode('utf-8')
        except UnicodeEncodeError:
            raise PlantkgError(ErrorCode.INVALID_REQUEST, '资料必须是有效的 UTF-8 文本') from None
        if (not re.search(r"\.(txt|md|markdown|json)$", request.filename, re.I)
                or '/' in request.filename or '\\' in request.filename or not request.text.strip()
                or len(content) > 1048576):
            raise PlantkgError(ErrorCode.INVALID_REQUEST, "只接受不超过 1 MiB 的非空 UTF-8 txt、md 或 json")
        if request.filename.lower().endswith('.json'):
            try:
                json.loads(request.text)
            except ValueError:
                raise PlantkgError(ErrorCode.INVALID_REQUEST, "JSON 格式无效") from None
        request_hash = digest(request.model_dump(mode='json'))
        trusted = Context(plant_id=context.plant_id, industry_pack=context.industry_pack, actor=actor,
                          channel='rest', trace_id=context.trace_id, as_of=datetime.now(timezone.utc),
                          idempotency_key=request.idempotency_key)
        with self.lock:
            row = read(self.writer, lambda con: con.execute(
                "SELECT request_hash, receipt FROM b26_uploads WHERE plant_id=? AND idempotency_key=?",
                (context.plant_id, request.idempotency_key)).fetchone())
            if row:
                if row[0] != request_hash:
                    raise PlantkgError(ErrorCode.IDEMPOTENCY_MISMATCH, "幂等键已用于其他资料")
                receipt = DocumentIngestReceipt.model_validate_json(row[1])
                if receipt.status in ('stored', 'processing_failed'):
                    receipt = self.extract(trusted, request.idempotency_key, receipt)
                return self.project(trusted.model_copy(update={'as_of': datetime.now(timezone.utc)}), receipt)
            document_id = request.document_id or 'doc-' + digest([context.plant_id, request.idempotency_key])[:24]
            previous = read(self.writer, lambda con: con.execute(
                'SELECT expert FROM b26_uploads WHERE plant_id=? AND document_id=? LIMIT 1',
                (context.plant_id, document_id)).fetchone())
            if previous and previous[0] != request.expert:
                raise PlantkgError(ErrorCode.STATE_CONFLICT, '资料已归属其他专家')
            for entity_id in request.entity_ids:
                entity = self.catalog.resolve_entity(trusted, entity_id)
                names = [entity.label, *entity.aliases]
                if not mentions(request.text, entity_id) and not any(
                    mentions(request.text, name) and len(self.catalog.find_entities(trusted, name)) == 1 for name in names
                ):
                    raise PlantkgError(ErrorCode.PRECONDITION, f'原文中没有可唯一定位的实体 {entity_id}')
            source = self.root / digest(document_id) / 'source.txt'
            source.parent.mkdir(parents=True, exist_ok=True)
            now = datetime.now(timezone.utc)
            trusted = trusted.model_copy(update={'as_of': now})
            temporary = source.with_name('pending-' + secrets.token_hex(12))
            try:
                temporary.write_bytes(content)
                os.replace(temporary, source)
            finally:
                temporary.unlink(missing_ok=True)
            asset, existed = self.catalog.ingest_detail(trusted, str(source), 'process_doc', now,
                                                         nature=request.nature, title=request.filename,
                                                         entity_ids=request.entity_ids or None)
            receipt = DocumentIngestReceipt(document_id=document_id, asset_version_id=asset.asset_version_id,
                        content_hash=asset.content_hash, parent_version_id=asset.parent_version_id,
                        expert=request.expert, nature=asset.nature, available_at=asset.available_at,
                        status='stored', deduplicated=existed, entity_ids=tuple(request.entity_ids),
                        received_at=now, source_name=request.filename, source_level=request.source_level,
                        note='原文已保存；尚未发布')
            atomic(self.writer, lambda con: con.execute(
                'INSERT INTO b26_uploads VALUES (?,?,?,?,?,?,?,?,?)',
                (context.plant_id, request.idempotency_key, request_hash, document_id, asset.asset_version_id,
                 request.expert, request.source_level, now.isoformat(), receipt.model_dump_json())))
            self.catalog.record_activation(trusted, asset.asset_version_id, 'identified', ref=f'kb://{request.expert}')
            receipt = self.extract(trusted, request.idempotency_key, receipt)
            return self.project(trusted.model_copy(update={'as_of': datetime.now(timezone.utc)}), receipt)

    def extract(self, context, key, receipt):
        try:
            if ('extract_rules' not in self.registry.tools or 'extract_rules' in self.registry.fallbacks
                    or 'extract_rules' in self.registry.placeholders):
                raise PlantkgError(ErrorCode.DEPENDENCY_UNAVAILABLE, '规则抽取尚未接入')
            result = self.registry.tools['extract_rules'].handler(context, asset_version_id=receipt.asset_version_id)
            if not result.ok:
                raise PlantkgError(ErrorCode.DEPENDENCY_UNAVAILABLE, '规则抽取未完成')
            ontology_ids = []
            hooks = self.registry.hooks_for('knowledge.ontology.propose_document')
            if receipt.entity_ids and not hooks:
                raise PlantkgError(ErrorCode.DEPENDENCY_UNAVAILABLE, '本体候选尚未接入')
            text = self.catalog.read_content(context, receipt.asset_version_id).decode('utf-8')
            for entity_id in receipt.entity_ids:
                entity = self.catalog.resolve_entity(context, entity_id)
                names = [entity_id, entity.label, *entity.aliases]
                line_number, line = next((i, line) for i, line in enumerate(text.splitlines(), 1)
                                         if any(mentions(line, name) for name in names))
                evidence = EvidenceRef(asset_version_id=receipt.asset_version_id,
                                       locator=Locator(line_start=line_number), excerpt=line[:500])
                ontology_ids.append(hooks[-1](context, entity_id, evidence).candidate_id)
            receipt = receipt.model_copy(update={'candidate_ids': tuple(result.data.get('candidate_ids', ())),
                'ontology_candidate_ids': tuple(ontology_ids),
                'status': 'needs_review', 'note': '本地模板抽取；候选待复核，入库时尚未发布'})
        except Exception:
            log.exception('资料已存储，但候选抽取失败: %s', receipt.asset_version_id)
            receipt = receipt.model_copy(update={'status': 'processing_failed',
                'note': '原文已保存，本地候选处理失败；用原幂等键重试。资料可检索，尚未发布'})
        atomic(self.writer, lambda con: con.execute(
            'UPDATE b26_uploads SET receipt=? WHERE plant_id=? AND idempotency_key=?',
            (receipt.model_dump_json(), context.plant_id, key)))
        return receipt

    def receipt(self, context, path, query):
        context = at_time(context, query)
        asset_id = path['asset_version_id']
        document_id = query.get('document_id')
        if document_id is not None and not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,63}', document_id):
            raise PlantkgError(ErrorCode.INVALID_REQUEST, '资料编号格式无效')
        sql = ('SELECT receipt, document_id FROM b26_uploads '
               'WHERE plant_id=? AND asset_version_id=? AND available_at<=?')
        params = (context.plant_id, asset_id, context.as_of.isoformat())
        if document_id is not None:
            sql += ' AND document_id=?'
            params += (document_id,)
        rows = read(self.writer, lambda con: con.execute(sql + ' ORDER BY rowid DESC', params).fetchall())
        if not rows:
            raise PlantkgError(ErrorCode.NOT_FOUND, '没有入库回执')
        if document_id is None and any(row[1] != rows[0][1] for row in rows[1:]):
            raise PlantkgError(ErrorCode.STATE_CONFLICT, '该资产对应多个资料，请提供 document_id')
        receipt = DocumentIngestReceipt.model_validate_json(rows[0][0])
        if receipt.available_at > context.as_of:
            raise PlantkgError(ErrorCode.NOT_VISIBLE, '资料在该时点不可见')
        return self.project(context, receipt)

    def project(self, context, receipt):
        """读取统一动作及其执行结果；不把上传或审核冒充发布。"""
        actions = self.registry.resolve(PendingActionStore)
        refs = {*receipt.candidate_ids, *receipt.ontology_candidate_ids}
        reviews, backtests, action_ids, manifests = {}, {}, [], []
        cursor = None
        while True:
            page, cursor = actions.list(cursor=cursor, page_size=100)
            for action in page:
                if action.target_ref not in refs or action.proposed_at > context.as_of:
                    continue
                action_ids.append(action.action_id)
                decided = action.decided_at is not None and action.decided_at <= context.as_of
                if action.kind == 'review_decision' and decided:
                    reviews[action.target_ref] = action.payload['decision'] if action.status == 'confirmed' else 'unconfirmed'
                if action.kind == 'publish':
                    report = action.payload.get('backtest_report')
                    if report:
                        backtests[action.action_id] = report['verdict']
                    if decided and action.status == 'confirmed':
                        result = actions.result(action.action_id)
                        if result and result.ok and result.version_manifest_id:
                            manifests.append(result.version_manifest_id)
            if not cursor:
                break
        status = receipt.status
        if manifests:
            status = 'published'
        elif reviews and all(reviews.get(ref) == 'reject' for ref in refs):
            status = 'rejected'
        elif reviews:
            status = 'reviewed'
        note = receipt.note
        if manifests:
            note = '已通过统一确认发布；图谱须按清单读取，旧运行保持原版本'
        if backtests and self.settings.profile in ('dev', 'demo', 'mock'):
            note += '；本地回测使用明确标注的 synthetic 计价基础，缺月补 0.62 元/kWh'
        return receipt.model_copy(update={'status': status, 'published': bool(manifests), 'note': note,
            'reviews': reviews, 'backtests': backtests, 'action_ids': tuple(action_ids),
            'manifest_ids': tuple(dict.fromkeys(manifests))})

    def documents(self, expert, as_of):
        when = as_of or datetime.now(timezone.utc)
        rows = read(self.writer, lambda con: con.execute(
            'SELECT receipt,available_at FROM b26_uploads WHERE plant_id=? AND available_at<=? ORDER BY rowid',
            (self.settings.plant_id, when.isoformat())).fetchall())
        docs = {}
        for raw, received_at in rows:
            receipt = DocumentIngestReceipt.model_validate_json(raw)
            if expert is not None and receipt.expert != expert:
                continue
            key = 'upload-' + digest([receipt.document_id, receipt.asset_version_id])[:32]
            docs[key] = DocumentSpec(document_id=key, expert=receipt.expert, title=receipt.source_name,
                path=receipt.asset_version_id, kind='text', asset_type='process_doc', source_level=receipt.source_level,
                available_at=received_at, nature=receipt.nature, kb_upload=False,
                asset_version_id=receipt.asset_version_id, tags=('local_deterministic',))
        return tuple(docs.values())

    def document_chunks(self, document):
        if not document.asset_version_id:
            return None
        context = Context(plant_id=self.settings.plant_id, industry_pack=self.settings.industry_pack,
            actor={'actor_id': 'document-reader', 'roles': ['viewer']}, channel='rest',
            trace_id='document-library', as_of=document.available_at)
        text = self.catalog.read_content(context, document.asset_version_id).decode('utf-8')
        return [(f'line:{i}', line) for i, line in enumerate(text.splitlines(), 1) if line.strip()]

    def fragments(self, context, path, query):
        context = at_time(context, query)
        asset = self.catalog.get(context, path['asset_version_id'])
        try:
            text = self.catalog.read_content(context, asset.asset_version_id).decode('utf-8')
        except UnicodeDecodeError:
            raise PlantkgError(ErrorCode.PRECONDITION, '该资料不是 UTF-8 文本') from None
        items = tuple(AssetFragment(fragment_id=f"{asset.content_hash[7:23]}-{i}", asset_version_id=asset.asset_version_id,
                      locator=Locator(line_start=i), kind='text', text=line, available_at=asset.available_at)
                      for i, line in enumerate(text.splitlines(), 1) if line.strip())
        try:
            offset, size = int(query.get('cursor', '0')), int(query.get('page_size', '50'))
            if offset < 0 or not 1 <= size <= 200:
                raise ValueError
        except ValueError:
            raise PlantkgError(ErrorCode.INVALID_REQUEST, '分页参数无效') from None
        return Page[AssetFragment](items=items[offset:offset + size],
                    next_cursor=str(offset + size) if offset + size < len(items) else None, as_of=context.as_of)

    def search(self, context, path, query):
        expert, wanted = query.get('expert'), tokens(query.get('q', ''))
        if expert not in ('elevator', 'hvac', 'energy', 'equipment') or not wanted:
            raise PlantkgError(ErrorCode.INVALID_REQUEST, '需要专家和检索词')
        context = at_time(context, query)
        rows = read(self.writer, lambda con: con.execute(
            'SELECT document_id,asset_version_id,source_level FROM b26_uploads WHERE plant_id=? AND expert=? AND available_at<=? ORDER BY rowid DESC',
            (context.plant_id, expert, context.as_of.isoformat())).fetchall())
        hits, seen, levels = [], set(), {}
        for document_id, asset_id, level in rows:
            if (document_id, asset_id) in seen:
                continue
            seen.add((document_id, asset_id))
            levels[document_id, asset_id] = LEVEL_RANK[SourceLevel(level)]
            cursor = None
            while True:
                page = self.fragments(context, {'asset_version_id': asset_id}, {'page_size': '200', **({'cursor': cursor} if cursor else {})})
                for fragment in page.items:
                    score = len(wanted & tokens(fragment.text or '')) / len(wanted)
                    if score:
                        hits.append(DocumentSearchHit(document_id=document_id, asset_version_id=asset_id,
                            locator=fragment.locator, text=fragment.text or '', score=score, expert=expert))
                cursor = page.next_cursor
                if not cursor:
                    break
        hits.sort(key=lambda item: (-round(item.score, 4), -levels[item.document_id, item.asset_version_id], item.asset_version_id, item.locator.line_start or 0))
        try:
            offset, size = int(query.get('cursor', '0')), int(query.get('page_size', '50'))
            if offset < 0 or not 1 <= size <= 200:
                raise ValueError
        except ValueError:
            raise PlantkgError(ErrorCode.INVALID_REQUEST, '分页参数无效') from None
        return Page[DocumentSearchHit](items=tuple(hits[offset:offset + size]),
            next_cursor=str(offset + size) if offset + size < len(hits) else None, as_of=context.as_of)

    def graph(self, context, path, query):
        manifest_id = query.get('manifest_id')
        if not manifest_id:
            raise PlantkgError(ErrorCode.INVALID_REQUEST, '需要固定的 manifest_id')
        if 'graph_query' not in self.registry.tools or 'graph_query' in self.registry.fallbacks or 'graph_query' in self.registry.placeholders:
            raise PlantkgError(ErrorCode.DEPENDENCY_UNAVAILABLE, '图谱查询尚未接入')
        try:
            size = int(query.get('page_size', '50'))
            if not 1 <= size <= 200:
                raise ValueError
        except ValueError:
            raise PlantkgError(ErrorCode.INVALID_REQUEST, 'page_size 须在 1～200') from None
        context = at_time(context, query).model_copy(update={'version_manifest_id': manifest_id})
        versions = self.registry.resolve(VersionStore)
        history = versions.history(manifest_id)
        if not any(event['to_status'] == 'active' and
                   datetime.fromisoformat(event['occurred_at'].replace('Z', '+00:00')) <= context.as_of
                   for event in history):
            raise PlantkgError(ErrorCode.NOT_VISIBLE, '清单在该时点尚未发布')
        return self.registry.tools['graph_query'].handler(context, query_id='triples',
                    cursor=query.get('cursor'), page_size=size)

    def install_local_backtest(self):
        """本地资料候选缺当月价格时，使用明确标注的合成快回测配置。"""
        hooks = self.registry.hooks_for('evolution.fast_scenarios')
        if not hooks or self.settings.profile not in ('dev', 'demo', 'mock'):
            return
        previous = hooks[-1]
        repository = Path(__file__).resolve().parents[3]

        def scenarios(candidate, context):
            rows = read(self.writer, lambda con: con.execute(
                'SELECT receipt FROM b26_uploads WHERE plant_id=?', (context.plant_id,)).fetchall())
            receipts = [DocumentIngestReceipt.model_validate_json(row[0]) for row in rows]
            if not any(candidate.candidate_id in (*r.candidate_ids, *r.ontology_candidate_ids) for r in receipts):
                return previous(candidate, context)
            tariff = yaml.safe_load((repository / 'fixtures/packs/tariff/tariff.yaml').read_text(encoding='utf-8'))
            for year in range(context.as_of.year - 1, context.as_of.year + 3):
                for month in range(1, 13):
                    tariff['tariff']['base_prices'].setdefault(f'{year:04}-{month:02}', 0.62)
            tariff['tariff']['demo_base_price'] = True
            tariff['tariff']['base_price_note'] = 'synthetic 本地资料回测：0.62 元/kWh，不代表公告值'
            tariff_text = yaml.safe_dump(tariff, allow_unicode=True)
            target = self.root / 'synthetic_backtest'
            target.mkdir(parents=True, exist_ok=True)
            tariff_path = target / (digest(tariff_text) + '.yaml')
            if not tariff_path.exists():
                tariff_path.write_text(tariff_text, encoding='utf-8')
            scenario = yaml.safe_load((repository / 'configs/scenarios/fast/b32_demo.yaml').read_text(encoding='utf-8'))
            scenario['sources'] = {'tariff_config': str(tariff_path)}
            scenario['date'] = (context.as_of.astimezone(ZoneInfo('Asia/Shanghai')) + timedelta(days=1)).date().isoformat()
            scenario['description'] += '；计价基础为 synthetic 测试值，不代表真实公告'
            scenario['scenario_id'] = 'issue_232_' + digest([tariff_text, scenario])[:16]
            scenario_text = yaml.safe_dump(scenario, allow_unicode=True)
            scenario_path = target / (digest(scenario_text) + '.yaml')
            if not scenario_path.exists():
                scenario_path.write_text(scenario_text, encoding='utf-8')
            return ({'scenario_id': scenario['scenario_id'], 'seed': 32, 'path': str(scenario_path), 'fast': True},)

        self.registry.register_hook('evolution.fast_scenarios', scenarios)


def install(registry, writer, auth, settings):
    service = UploadService(registry, writer, auth, settings)
    for method, path, handler in (
        ('POST', '/assets:ingest', service.ingest),
        ('GET', '/assets/{asset_version_id}/ingestion', service.receipt),
        ('GET', '/assets/{asset_version_id}/fragments', service.fragments),
        ('GET', '/knowledge/documents:search', service.search),
        ('GET', '/knowledge/graph', service.graph),
    ):
        registry.register_rest(method, path, handler)
    return service
