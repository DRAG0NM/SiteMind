from datetime import datetime, timezone
from dataclasses import replace
from urllib.parse import quote

import pytest
from starlette.testclient import TestClient

from plantkg.api import create_app
from plantkg.bootstrap import build_services
from plantkg.core.config import load_settings
from plantkg.core.contracts import Actor, Context, Entity, Role, PlantkgError
from plantkg.core.ports import AssetRepository, PendingActionStore, VersionStore
from plantkg.knowledge.rules.publisher import RuleArtifacts
from plantkg.knowledge.rules.compile import compile_reviewed_rule
from plantkg.knowledge.rules.store import CandidateStore


def test_shared_asset_receipts_are_scoped_by_document_and_survive_restart(tmp_path):
    settings = load_settings(profile='dev', overrides={
        'storage.enabled': True, 'storage.sqlite': str(tmp_path / 'catalog.db'),
        'storage.graph': str(tmp_path / 'graph'), 'storage.series': str(tmp_path / 'series'),
        'actions.reviewers': ['reviewer'],
    })
    services = build_services(settings)
    headers = {'x-plantkg-operator-token': services.version_run.operator.token, 'x-plantkg-actor': 'reviewer'}
    body = {'filename': 'hvac.md', 'text': '合成资料：行政楼温度24±1 ℃。', 'nature': 'synthetic',
            'expert': 'hvac', 'document_id': 'manual-hvac', 'idempotency_key': 'hvac-1'}
    with TestClient(create_app(services)) as client:
        first = client.post('/api/v1/assets:ingest', json=body, headers=headers)
        assert first.status_code == 200, first.text
        first = first.json()
        duplicate = client.post('/api/v1/assets:ingest', headers=headers,
            json=body | {'filename': 'hvac-latest.md', 'idempotency_key': 'hvac-2'})
        assert duplicate.status_code == 200, duplicate.text
        assert duplicate.json()['asset_version_id'] == first['asset_version_id']
        path = f"/api/v1/assets/{quote(first['asset_version_id'], safe='')}/ingestion"
        cutoff = datetime.now(timezone.utc).isoformat()
        assert client.get(path, params={'as_of': cutoff}).json()['source_name'] == 'hvac-latest.md'
        second = client.post('/api/v1/assets:ingest', headers=headers,
            json=body | {'filename': 'energy.md', 'expert': 'energy', 'document_id': 'manual-energy', 'idempotency_key': 'energy-1'})
        assert second.status_code == 200, second.text
        assert second.json()['asset_version_id'] == first['asset_version_id']

        def check_receipts(client):
            now = datetime.now(timezone.utc).isoformat()
            for document_id, expert, source in (('manual-hvac', 'hvac', 'hvac-latest.md'),
                                                ('manual-energy', 'energy', 'energy.md')):
                result = client.get(path, params={'document_id': document_id, 'as_of': now})
                assert result.status_code == 200, result.text
                assert (result.json()['document_id'], result.json()['expert'], result.json()['source_name']) == (document_id, expert, source)
            ambiguous = client.get(path, params={'as_of': now})
            assert ambiguous.status_code == 409 and 'document_id' in ambiguous.json()['error']['message']
            assert client.get(path, params={'as_of': now, 'document_id': 'missing'}).status_code == 404
            assert client.get(path, params={'as_of': now, 'document_id': ''}).status_code == 400
            assert client.get(path, params={'as_of': cutoff}).json()['document_id'] == 'manual-hvac'
            assert client.get(path, params={'as_of': cutoff, 'document_id': 'manual-energy'}).status_code == 404
        check_receipts(client)
    services = build_services(settings)
    with TestClient(create_app(services)) as client:
        check_receipts(client)


def test_http_upload_versions_search_and_restart(tmp_path):
    settings = load_settings(profile='dev', overrides={
        'storage.enabled': True, 'storage.sqlite': str(tmp_path / 'catalog.db'),
        'storage.graph': str(tmp_path / 'graph'), 'storage.series': str(tmp_path / 'series'),
        'actions.reviewers': ['reviewer'],
    })
    services = build_services(settings)
    headers = {'x-plantkg-operator-token': services.version_run.operator.token, 'x-plantkg-actor': 'reviewer'}
    body = {'filename': '规程.md', 'text': '行政楼温度24±1 ℃。', 'expert': 'hvac',
            'document_id': 'manual', 'idempotency_key': 'one', 'nature': 'synthetic'}
    with TestClient(create_app(services)) as client:
        assert client.post('/api/v1/assets:ingest', json=body).status_code == 403
        first_response = client.post('/api/v1/assets:ingest', json=body, headers=headers)
        assert first_response.status_code == 200, first_response.text
        first = first_response.json()
        assert first['candidate_ids'] and not first['published']
        assert client.post('/api/v1/assets:ingest', json=body, headers=headers).json() == first
        cutoff = datetime.now(timezone.utc).isoformat()
        assert client.post('/api/v1/assets:ingest', json=body | {'text': 'different'}, headers=headers).status_code == 409
        body.update(text='行政楼温度25±1 ℃。', idempotency_key='two')
        second_response = client.post('/api/v1/assets:ingest', json=body, headers=headers)
        assert second_response.status_code == 200, second_response.text
        second = second_response.json()
        assert second['parent_version_id'] == first['asset_version_id']
        assert client.get('/api/v1/knowledge/documents:search', params={'expert': 'hvac', 'q': '25', 'as_of': cutoff}).json()['items'] == []
        assert client.get(f"/api/v1/assets/{quote(second['asset_version_id'], safe='')}/ingestion", params={'as_of': cutoff}).status_code == 404
        as_of = datetime.now(timezone.utc).isoformat()
        asset_path = quote(first['asset_version_id'], safe='')
        fragments = client.get(f'/api/v1/assets/{asset_path}/fragments', params={'as_of': as_of})
        assert fragments.status_code == 200, fragments.text
        assert fragments.json()['items'][0]['text'] == '行政楼温度24±1 ℃。'
        search = client.get('/api/v1/knowledge/documents:search', params={'expert': 'hvac', 'q': '行政楼温度', 'as_of': as_of})
        assert search.status_code == 200, search.text
        assert first['asset_version_id'] in {hit['asset_version_id'] for hit in search.json()['items']}
        assert not client.get('/api/v1/knowledge/documents:search', params={'expert': 'energy', 'q': '行政楼温度', 'as_of': as_of}).json()['items']
        library = services.registry.hooks_for('knowledge.documents.library')[-1]()
        from plantkg.knowledge.documents.retrieval import search as library_search
        assert any(hit.document.asset_version_id == first['asset_version_id']
                   for hit in library_search(library, 'hvac', '行政楼温度', as_of=datetime.now(timezone.utc)))
        assert library.documents('hvac', datetime.fromisoformat(cutoff))
        assert all(doc.asset_version_id != second['asset_version_id'] for doc in library.documents('hvac', datetime.fromisoformat(cutoff)))
        assert all(not doc.kb_upload for doc in library.documents('hvac') if doc.asset_version_id)
    services = build_services(settings)
    with TestClient(create_app(services)) as client:
        response = client.get(f'/api/v1/assets/{asset_path}/ingestion', params={'as_of': datetime.now(timezone.utc).isoformat()})
        assert response.status_code == 200, response.text
        assert response.json()['asset_version_id'] == first['asset_version_id']


def test_uploaded_entity_requires_review_and_publishes_versioned_graph(tmp_path):
    settings = load_settings(profile='dev', overrides={
        'storage.enabled': True, 'storage.sqlite': str(tmp_path / 'catalog.db'),
        'storage.graph': str(tmp_path / 'graph'), 'storage.series': str(tmp_path / 'series'),
        'actions.reviewers': ['reviewer'],
    })
    services = build_services(settings)
    registry = services.registry
    versions = registry.resolve(VersionStore)
    reviewer = Actor(actor_id='reviewer', roles=(Role.reviewer,))
    now = datetime.now(timezone.utc)
    context = Context(plant_id=settings.plant_id, industry_pack=settings.industry_pack, actor=reviewer,
                      channel='rest', trace_id='upload-graph-test', as_of=now)
    entity_id = f'entity://{settings.plant_id}/air_compressor/AC-2'
    registry.resolve(AssetRepository).register_entity(context, Entity(
        entity_id=entity_id, entity_type='air_compressor', label='AC-2', plant_id=settings.plant_id))
    headers = {'x-plantkg-operator-token': services.version_run.operator.token, 'x-plantkg-actor': 'reviewer'}
    with TestClient(create_app(services)) as client:
        wrong_entity = client.post('/api/v1/assets:ingest', headers=headers, json={
            'filename': 'manual.txt', 'text': 'AC-20 空压机，主管压力5分钟内下降超过0.2 bar。',
            'expert': 'equipment', 'nature': 'synthetic', 'entity_ids': [entity_id], 'idempotency_key': 'wrong-entity'})
        assert wrong_entity.status_code == 422
        body = {
            'filename': 'manual.txt', 'text': 'AC-2 空压机，主管压力5分钟内下降超过0.2 bar。',
            'expert': 'equipment', 'nature': 'synthetic', 'entity_ids': [entity_id], 'idempotency_key': 'upload-graph'}
        response = client.post('/api/v1/assets:ingest', headers=headers, json=body)
        assert response.status_code == 200, response.text
        receipt = response.json()
        context = context.model_copy(update={'as_of': datetime.now(timezone.utc)})
        rule_candidate = receipt['candidate_ids'][0]
        rule_review = registry.tools['propose_review_decision'].handler(
            context, review_id=rule_candidate, decision='accept', rationale='核对资料')
        actions = registry.resolve(PendingActionStore)
        actions.confirm(rule_review.data['action_id'], reviewer, '核对规则', 0, 'initial-rule-review')
        rule = compile_reviewed_rule(CandidateStore(versions.writer, settings.plant_id), actions, rule_candidate,
                                     reviewer, subject_id=entity_id, effective_from=context.as_of, nature='synthetic')
        rules = RuleArtifacts(versions.writer, settings.plant_id).prepare([rule], reviewer)
        base = versions.prepare({'rules': rules}, 'initial')
        versions.check_integrity(base.version_manifest_id)
        base = versions.activate(None, base.version_manifest_id, reviewer, 'initial')
        candidate = receipt['ontology_candidate_ids'][0]
        old_context = context.model_copy(update={'as_of': now})
        assert candidate not in str(registry.tools['next_review_batch'].handler(old_context).data)
        with pytest.raises(PlantkgError):
            registry.tools['propose_review_decision'].handler(old_context, review_id=candidate,
                decision='accept', rationale='future', revision={'choice': 'https://brickschema.org/schema/Brick#Compressor'})
        context = context.model_copy(update={'as_of': datetime.now(timezone.utc), 'version_manifest_id': base.version_manifest_id})
        proposal = registry.tools['propose_review_decision'].handler(
            context, review_id=candidate, decision='accept', rationale='核对资料',
            revision={'choice': 'https://brickschema.org/schema/Brick#Compressor'})
        action = actions.get(proposal.data['action_id'])
        confirmed = client.post(f'/api/v1/actions/{action.action_id}/confirm', headers=headers,
                                json={'reason': '复核原文', 'expected_revision': action.revision, 'idempotency_key': 'review-graph'})
        assert confirmed.status_code == 200, confirmed.text
        publish = registry.tools['propose_publish'].handler(context, candidate_id=candidate, rationale='发布本体')
        assert publish.ok, publish.to_agent_text()
        action = actions.get(publish.data['action_id'])
        assert action.payload['backtest_report']['verdict'] == 'pass'
        result = client.post(f'/api/v1/actions/{action.action_id}/confirm', headers=headers,
                             json={'reason': '确认发布', 'expected_revision': action.revision, 'idempotency_key': 'publish-graph'})
        assert result.status_code == 200, result.text
        active = versions.active()
        assert active.version_manifest_id != base.version_manifest_id
        graph = client.get('/api/v1/knowledge/graph', params={'manifest_id': active.version_manifest_id, 'as_of': datetime.now(timezone.utc).isoformat()})
        assert graph.status_code == 200, graph.text
        assert receipt['asset_version_id'] in str(graph.json()['data']['items'])
        old = client.get('/api/v1/knowledge/graph', params={'manifest_id': base.version_manifest_id})
        assert old.status_code == 404
        assert client.get('/api/v1/knowledge/graph', params={'manifest_id': active.version_manifest_id, 'as_of': now.isoformat()}).status_code == 404
        asset_path = quote(receipt['asset_version_id'], safe='')
        state = client.get(f'/api/v1/assets/{asset_path}/ingestion', params={'as_of': datetime.now(timezone.utc).isoformat()}).json()
        assert state['published'] and state['manifest_ids'] == [active.version_manifest_id]
        assert state['reviews'][candidate] == 'accept' and state['backtests'][action.action_id] == 'pass'
        from plantkg.core.versions import read
        tables = ('asset_versions', 'b26_uploads', 'b30_candidates', 'b30_ontology_candidates')
        counts = lambda: read(versions.writer, lambda con: [con.execute(f'SELECT count(*) FROM {table}').fetchone()[0] for table in tables])
        before = counts()
        replay = client.post('/api/v1/assets:ingest', headers=headers, json=body)
        assert replay.status_code == 200, replay.text
        for field in ('status', 'published', 'manifest_ids', 'candidate_ids', 'ontology_candidate_ids',
                      'document_id', 'asset_version_id', 'reviews', 'backtests', 'action_ids'):
            assert replay.json()[field] == state[field]
        assert counts() == before
        assert registry.resolve(AssetRepository).activation(context.model_copy(update={'as_of': datetime.now(timezone.utc)}), receipt['asset_version_id']) == 'in_graph'
    services = build_services(settings)
    with TestClient(create_app(services)) as client:
        state = client.get(f'/api/v1/assets/{asset_path}/ingestion', params={'as_of': datetime.now(timezone.utc).isoformat()}).json()
        assert state['published']
        graph = client.get('/api/v1/knowledge/graph', params={'manifest_id': active.version_manifest_id, 'as_of': datetime.now(timezone.utc).isoformat()})
        assert graph.status_code == 200 and receipt['asset_version_id'] in graph.text


def test_upload_failure_retry_and_rejected_candidate(tmp_path, monkeypatch):
    settings = load_settings(profile='dev', overrides={'storage.enabled': True,
        'storage.sqlite': str(tmp_path / 'catalog.db'), 'storage.graph': str(tmp_path / 'graph'),
        'storage.series': str(tmp_path / 'series'), 'actions.reviewers': ['reviewer']})
    services = build_services(settings)
    registry = services.registry
    provider = registry.rest['POST', '/assets:ingest'].__self__
    headers = {'x-plantkg-operator-token': services.version_run.operator.token, 'x-plantkg-actor': 'reviewer'}
    body = {'filename': 'manual.json', 'text': '{"note":"不支持自动解释的资料"}', 'expert': 'energy', 'idempotency_key': 'retry'}
    original = registry.tools['extract_rules'].handler
    def fail(*args, **kwargs):
        raise RuntimeError('local injected processing failure')
    with TestClient(create_app(services)) as client:
        assert client.post('/api/v1/assets:ingest', json=body | {'text': 'invalid json'}, headers=headers).status_code == 400
        def fail_storage(*args, **kwargs):
            raise OSError('local injected storage failure')
        with monkeypatch.context() as failed_storage:
            failed_storage.setattr(provider.catalog, 'ingest_detail', fail_storage)
            failed = client.post('/api/v1/assets:ingest', json=body, headers=headers)
            assert failed.status_code == 500 and 'error' in failed.json()
        monkeypatch.setitem(registry.tools, 'extract_rules', replace(registry.tools['extract_rules'], handler=fail))
        response = client.post('/api/v1/assets:ingest', json=body, headers=headers)
        assert response.status_code == 200 and response.json()['status'] == 'processing_failed'
        first = response.json()
        path = quote(first['asset_version_id'], safe='')
        assert body['text'] in client.get(f'/api/v1/assets/{path}/fragments', params={'as_of': datetime.now(timezone.utc).isoformat()}).json()['items'][0]['text']
        monkeypatch.setitem(registry.tools, 'extract_rules', replace(registry.tools['extract_rules'], handler=original))
        receipt = client.post('/api/v1/assets:ingest', json=body, headers=headers).json()
        assert receipt['asset_version_id'] == first['asset_version_id'] and receipt['candidate_ids']
        context = services.guard.anonymous_read_context().model_copy(update={
            'as_of': datetime.now(timezone.utc), 'actor': Actor(actor_id='reviewer', roles=(Role.reviewer,)), 'channel': 'rest'})
        candidate = receipt['candidate_ids'][0]
        proposal = registry.tools['propose_review_decision'].handler(context,
            review_id=candidate, decision='reject', rationale='无模板，保留资料')
        action = registry.resolve(PendingActionStore).get(proposal.data['action_id'])
        result = client.post(f'/api/v1/actions/{action.action_id}/confirm', headers=headers,
            json={'reason': '拒绝候选', 'expected_revision': action.revision, 'idempotency_key': 'reject-review'})
        assert result.status_code == 200
        state = client.get(f'/api/v1/assets/{path}/ingestion', params={'as_of': datetime.now(timezone.utc).isoformat()}).json()
        assert state['status'] == 'rejected' and not state['published']
        with pytest.raises(PlantkgError):
            registry.tools['propose_publish'].handler(context, candidate_id=candidate, rationale='禁止发布')
        with pytest.raises(PlantkgError):
            provider.ingest(context.model_copy(update={'plant_id': 'other'}), {}, {}, body=body, headers=headers)
