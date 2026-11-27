"""Versioned offline AgentResultSource cache for B33/B32."""
from __future__ import annotations

import hashlib
import json
import copy
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from plantkg.core.contracts import AgentResultKind
from plantkg.core.ports import AgentResultSource


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), default=str)


def fingerprint(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode('utf-8')).hexdigest()


class ArbitrationCollector:
    """Translate the B28 arbitration contract to the B23 live source.

    The caller must enforce a monetary budget before invoking this adapter. A hard
    call count here prevents accidental unbounded collection and retries.
    """
    def __init__(self, live_source: Any, max_calls: int):
        if max_calls < 1:
            raise ValueError('max_calls must be positive')
        self.live_source, self.remaining = live_source, max_calls

    def get(self, kind, binding, payload):
        if AgentResultKind(kind) != AgentResultKind.arbitration or self.remaining <= 0:
            raise ValueError('collection is limited to authorized arbitration calls')
        self.remaining -= 1
        proposals = payload.get('proposals') or []
        prompt = ('从候选提案中选择一个，输出 JSON：{"selected_proposal_id":"候选 ID",'
                  '"summary":"选择理由"}。只选已有 ID。候选：' + _canonical(proposals))
        if len(prompt) > 8000:
            raise ValueError('arbitration query exceeds gateway limit')
        response = dict(self.live_source.get(kind, binding, dict(payload) | {'query': prompt}))
        if response.get('status') != 'finished':
            raise ValueError('live arbitration did not finish')
        answer = str(response.get('final_answer') or '').strip()
        if answer.startswith('```'):
            answer = answer.split('\n', 1)[-1].rsplit('```', 1)[0].strip()
        decision = json.loads(answer)
        valid = {p.get('proposal_id') for p in proposals if isinstance(p, Mapping)}
        if not isinstance(decision, dict) or decision.get('selected_proposal_id') not in valid:
            raise ValueError('live arbitration did not select a current proposal')
        return {'status': 'finished', 'selected_proposal_id': decision['selected_proposal_id'],
                'summary': str(decision.get('summary') or ''), 'agent_run_id': response.get('agent_run_id')}


class ResultCache:
    """Persistent entries are immutable within one version unless explicitly refreshed."""
    def __init__(self, path: Path | str, version: str):
        self.path, self.version = Path(path), version
        self.entries = json.loads(self.path.read_text(encoding='utf-8')) if self.path.exists() else {}
        self._identities = {}
        self._state_components = {}
        self._lock = threading.RLock()
        self._collection_locks = {}

    def key(self, *, scenario: str, event_id: str, knowledge_version: Any, prompt_version: str,
            data_version: str, proposal: Any, state: Any, kind: str, model_version: str = '') -> str:
        identity = {'cache_version': self.version, 'scenario': scenario, 'event_id': event_id,
                            'knowledge_version': knowledge_version, 'prompt_version': prompt_version,
                            'data_version': data_version, 'model_version': model_version, 'proposal_fingerprint': fingerprint(proposal),
                            'state_fingerprint': fingerprint(state), 'kind': kind}
        key = fingerprint(identity)
        self._identities[key] = identity
        return key

    def get(self, key: str) -> dict | None:
        with self._lock:
            return copy.deepcopy(self.entries.get(key))

    def collection_lock(self, key: str):
        with self._lock:
            return self._collection_locks.setdefault(key, threading.Lock())

    def put(self, key: str, result: Mapping[str, Any], *, kind: str, source: str) -> None:
        with self._lock:
            if key in self.entries:
                raise ValueError('cache entry already exists; use refresh explicitly')
            self.refresh(key, result, kind=kind, source=source)

    def refresh(self, key: str, result: Mapping[str, Any], *, kind: str, source: str) -> None:
        if source not in ('nexent', 'deterministic'):
            raise ValueError('unknown source')
        with self._lock:
            now = datetime.now(timezone.utc).isoformat()
            first_collected_at = self.entries.get(key, {}).get('first_collected_at', now)
            self.entries[key] = {'version': self.version, 'kind': AgentResultKind(kind).value, 'source': source,
                                 'collected_at': now, 'first_collected_at': first_collected_at, 'identity': self._identities.get(key),
                                 'state_components': self._state_components.get(key),
                                 'result': dict(result)}
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_suffix(self.path.suffix + '.tmp')
            temp.write_text(_canonical(self.entries), encoding='utf-8')
            temp.replace(self.path)


class CachedSource:
    """Use deterministic source on miss. Per-call counters expose coverage gaps."""
    name = 'b33-cache'

    def __init__(self, registry: Any, cache: ResultCache, key: str, context: Mapping[str, Any]):
        self.registry, self.cache, self.registry_key, self.context = registry, cache, key, dict(context)
        self.hits = self.misses = self.collected = 0
        self.fallbacks = 0  # 未命中且没采集到、实际走了确定性回退的次数（misses 还含“未命中但随后采集成功”）
        self.miss_reasons = {}
        self.collection_failures = 0
        self.collection_errors = []
        self.miss_differences = []
        self.source_kind = 'deterministic'
        self._stats_lock = threading.Lock()

    def _key(self, kind, binding, payload):
        kind = AgentResultKind(kind)
        event = payload.get('event') or {}
        if hasattr(event, 'model_dump'):
            event = event.model_dump(mode='json')
        event_id = event.get('event_id') or payload.get('event_id')
        state = payload.get('state')
        resolver = self.context.get('resolve')
        if resolver:
            resolved_event, resolved_state = resolver(binding)
            event_id = event_id or resolved_event
            state = {'payload': state, 'scenario_state': resolved_state} if resolved_state is not None else None
        # Missing identity is a miss, never a shared wildcard cache entry.
        if event_id and state is not None:
            proposals = payload.get('proposals')
            if kind == AgentResultKind.arbitration and proposals is not None:
                proposals = [{k: v for k, v in p.items() if k not in ('proposal_id', 'ticket_id', 'binding', 'timing')}
                             if isinstance(p, Mapping) else p for p in proposals]
            state_identity = {'expert_key': self.registry_key, 'expert': payload.get('expert'), 'value': state,
                              'round': getattr(binding, 'round', None),
                              'state_version': getattr(binding, 'state_version', None),
                              'event': event, 'reason': payload.get('reason'),
                              'trigger_at': str(getattr(binding, 'trigger_at', ''))}
            if kind in (AgentResultKind.follow_up, AgentResultKind.investigate):
                state_identity['query'] = payload.get('query')
            if payload.get('items'):
                items = []
                for item in payload['items']:
                    normalized = {k: v for k, v in item.items() if k != 'ticket_id'}
                    if normalized.get('proposals'):
                        lookup = self.context.get('resolve_proposals')
                        if lookup is None:
                            return None  # Never drop proposal identity to manufacture a hit.
                        try:
                            normalized['proposals'] = sorted(fingerprint({k: v for k, v in lookup(pid).model_dump(mode='json').items()
                                if k not in ('proposal_id', 'ticket_id', 'binding', 'timing')}) for pid in normalized['proposals'])
                        except Exception:
                            return None
                    items.append(normalized)
                state_identity['items'] = items
            key = self.cache.key(scenario=self.context['scenario'], event_id=str(event_id),
                                 knowledge_version=binding.knowledge_version,
                                 prompt_version=self.context['prompt_version'], data_version=self.context['data_version'],
                                 model_version=self.context.get('model_version', ''),
                                 proposal=proposals, state=state_identity, kind=kind.value)
            world = (state.get('scenario_state') or {}).get('world', {}) if isinstance(state, Mapping) else {}
            self.cache._state_components[key] = {k: fingerprint(v) for k, v in world.items()}
            self.cache._state_components[key]['energy_kwh'] = fingerprint((state.get('scenario_state') or {}).get('energy_kwh')) if isinstance(state, Mapping) else ''
            self.cache._state_components[key]['items'] = fingerprint(state_identity.get('items'))
            for index, item in enumerate(state_identity.get('items') or ()):
                for field, value in item.items():
                    self.cache._state_components[key][f'item.{index}.{field}'] = fingerprint(value)
            return key
        return None

    def collect(self, kind, binding, payload, live_source, *, refresh=False, proposal_lookup=None):
        """Explicit first collection or refresh through a caller-supplied live source.

        The caller owns authorization and budget enforcement. Proposal bindings are
        checked before identifiers are detached; unsafe results are rejected.
        """
        kind = AgentResultKind(kind)
        key = self._key(kind, binding, payload)
        if key is None:
            raise ValueError('event and state are required for collection')
        if self.cache.get(key) is not None and not refresh:
            raise ValueError('cache entry exists; explicitly refresh it')
        result = dict(live_source.get(kind, binding, payload))
        if result.get('status') != 'finished':
            raise ValueError('only completed live results may be cached')
        ids = result.pop('proposal_ids', []) or []
        result.pop('agent_run_id', None)
        if ids:
            if kind != AgentResultKind.expert or len(ids) != 1 or proposal_lookup is None:
                raise ValueError('bound proposal requires an explicit lookup')
            proposal = proposal_lookup(ids[0])
            proposal = proposal.model_dump(mode='json') if hasattr(proposal, 'model_dump') else dict(proposal)
            original = proposal.get('binding') or {}
            if original != binding.model_dump(mode='json'):
                raise ValueError('proposal binding does not match the collection run')
            proposal.update(proposal_id=None, binding=None, timing=None)
            result['proposal'] = proposal
            result.pop('final_answer', None)
        proposal = result.get('proposal') or {}
        if proposal.get('binding') or proposal.get('timing'):
            raise ValueError('bound proposal cannot be replayed')
        if kind == AgentResultKind.expert and not proposal:
            raise ValueError('expert result has no proposal')
        if kind == AgentResultKind.investigate:
            from plantkg.agent.router.triage import parse_investigation
            parsed = parse_investigation(result)
            if not parsed.get('diagnosis') or not isinstance(parsed.get('experts'), list) or not isinstance(parsed.get('evidence_refs'), list):
                raise ValueError('investigation result is incomplete')
        if kind in (AgentResultKind.triage, AgentResultKind.follow_up) and not isinstance(result.get('final_answer'), str):
            raise ValueError('result has no textual answer')
        selected = result.get('selected_proposal_id')
        current_ids = {p.get('proposal_id') for p in payload.get('proposals') or () if isinstance(p, Mapping)}
        if selected is not None and selected not in current_ids:
            raise ValueError('selected proposal does not belong to this input')
        if selected is not None:
            result['selected_proposal_index'] = next(i for i, p in enumerate(payload['proposals'])
                                                     if p.get('proposal_id') == selected)
            result.pop('selected_proposal_id')
        answer = str(result.get('final_answer') or '')
        def identifier_pattern(value):
            return r'(?<![A-Za-z0-9_-])' + re.escape(str(value)) + r'(?![A-Za-z0-9_-])'

        if binding.run_id and re.search(identifier_pattern(binding.run_id), answer):
            raise ValueError('answer contains an original run identifier')
        items = payload.get('items') or ()
        if '__B33_TICKET' in answer:
            raise ValueError('answer contains a reserved placeholder')
        for index, item in enumerate(items):
            if isinstance(item, Mapping) and item.get('ticket_id') and item['ticket_id'] != binding.ticket_id:
                answer = re.sub(identifier_pattern(item['ticket_id']), f'__B33_TICKET_ITEM_{index}__', answer)
        if binding.ticket_id and isinstance(result.get('final_answer'), str):
            answer = re.sub(identifier_pattern(binding.ticket_id), '__B33_TICKET__', answer)
            if re.search(r'(?<![A-Za-z0-9_-])TK-\d+(?![A-Za-z0-9_-])', answer):
                raise ValueError('answer contains an unknown ticket identifier')
            result['final_answer'] = answer
        self.cache.refresh(key, result, kind=kind.value, source=getattr(live_source, 'cache_source', 'nexent'))
        return result

    def get(self, kind, binding, payload):
        kind = AgentResultKind(kind)
        key = self._key(kind, binding, payload)
        entry = self.cache.get(key) if key is not None else None
        missing = entry is None
        reason = 'missing_identity' if key is None else 'key_not_found' if missing else 'unsafe_entry'
        if missing and key is not None:
            current = self.cache._identities.get(key) or {}
            with self.cache._lock:
                candidates = [e for e in self.cache.entries.values()
                              if e.get('identity') and e['identity'].get('scenario') == current.get('scenario')
                              and e['identity'].get('event_id') == current.get('event_id')
                              and e['identity'].get('kind') == current.get('kind')]
            if candidates:
                closest = min(candidates, key=lambda old: sum(old['identity'].get(k) != v for k, v in current.items()))
                components = self.cache._state_components.get(key) or {}
                old_components = closest.get('state_components') or {}
                with self._stats_lock:
                    self.miss_differences.append({'kind': kind.value, 'event_id': current.get('event_id'),
                                                  'fields': [k for k, value in current.items() if closest['identity'].get(k) != value],
                                                  'state_components': [k for k, value in components.items() if old_components.get(k) != value]})
        collected, collection_ms = False, 0
        collector = self.context.get('collector')
        if collector == 'deterministic':
            collector = OfflineCollector(self.registry, self.registry_key)
        if (collector is not None and key is not None and
                (entry is None or self.context.get('refresh', False))):
            try:
                with self.cache.collection_lock(key):
                    current = self.cache.get(key)
                    if current is not None and not self.context.get('refresh', False):
                        entry = current
                    else:
                        started = time.monotonic()
                        self.collect(kind, binding, payload, collector,
                                     refresh=current is not None, proposal_lookup=self.context.get('proposal_lookup') or getattr(collector, 'proposal_lookup', None))
                        entry = self.cache.get(key)
                        collected = True
                        collection_ms = round((time.monotonic() - started) * 1000)
                        with self._stats_lock:
                            self.collected += 1
            except Exception as exc:  # Collection failure is visible and falls back to deterministic behavior.
                raced = missing and isinstance(exc, ValueError) and str(exc) == 'cache entry exists; explicitly refresh it'
                if raced:
                    entry = self.cache.get(key)
                else:
                    with self._stats_lock:
                        self.collection_failures += 1
                        self.collection_errors.append({'kind': kind.value, 'error': type(exc).__name__, 'message': str(exc)[:200]})
        if entry and entry['kind'] == kind.value:
            # Existing result may bind to a different run; only unbound drafts can be replayed safely.
            result = copy.deepcopy(entry['result'])
            if isinstance(result.get('final_answer'), str):
                result['final_answer'] = result['final_answer'].replace('__B33_TICKET__', str(binding.ticket_id))
                for index, item in enumerate(payload.get('items') or ()):
                    if isinstance(item, Mapping) and item.get('ticket_id'):
                        result['final_answer'] = result['final_answer'].replace(f'__B33_TICKET_ITEM_{index}__', str(item['ticket_id']))
            proposal = result.get('proposal') or {}
            selected_index = result.pop('selected_proposal_index', None)
            if selected_index is not None:
                proposals = payload.get('proposals') or ()
                if not isinstance(selected_index, int) or selected_index < 0 or selected_index >= len(proposals):
                    entry = None
                elif not isinstance(proposals[selected_index], Mapping) or not proposals[selected_index].get('proposal_id'):
                    entry = None
                else:
                    result['selected_proposal_id'] = proposals[selected_index]['proposal_id']
            selected = result.get('selected_proposal_id')
            proposals = payload.get('proposals') or ()
            current_ids = {p.get('proposal_id') for p in proposals if isinstance(p, Mapping)}
            unsafe_ids = bool(result.get('proposal_ids') or result.get('agent_run_id') or
                              (selected is not None and selected not in current_ids))
            if entry is not None and not proposal.get('binding') and not proposal.get('timing') and not unsafe_ids:
                if proposal:
                    proposal['ticket_id'] = binding.ticket_id
                    proposal['proposal_id'] = None
                if missing:
                    with self._stats_lock:
                        self.misses += 1
                        self.miss_reasons[reason] = self.miss_reasons.get(reason, 0) + 1
                elif not collected:
                    with self._stats_lock:
                        self.hits += 1
                if collected and entry['source'] == 'nexent':
                    source_kind = 'nexent'  # The call measured by this collection is a live run.
                elif entry['source'] == 'nexent':
                    source_kind = 'cache'
                else:
                    source_kind = 'deterministic'
                self.source_kind = source_kind  # Compatibility for callers without result metadata.
                extra = {'latency_ms': result.get('latency_ms') or collection_ms} if collected and source_kind == 'nexent' else {}
                return dict(result) | {'source': source_kind, 'coverage_gap': entry['source'] != 'nexent'} | extra
        with self._stats_lock:
            self.misses += 1
            self.fallbacks += 1
            self.miss_reasons[reason] = self.miss_reasons.get(reason, 0) + 1
        self.source_kind = 'deterministic'
        result = self.registry.resolve_mode(AgentResultSource, 'deterministic', key=self.registry_key).get(kind, binding, payload)
        return dict(result) | {'source': 'deterministic', 'coverage_gap': True}


class OfflineCollector:
    """Exercise collection and persistence using the registered offline source."""
    cache_source = 'deterministic'

    def __init__(self, registry, key):
        self.registry, self.key = registry, key

    def get(self, kind, binding, payload):
        result = dict(self.registry.resolve_mode(AgentResultSource, 'deterministic', key=self.key).get(kind, binding, payload))
        result.setdefault('status', 'finished')
        return result


def install_cache(registry: Any, cache: ResultCache, context: Mapping[str, str], keys: list[str]) -> list[CachedSource]:
    """Register without touching live or deterministic modes."""
    sources = []
    for mode in ('evaluation', 'trial'):
        for key in keys:
            source = CachedSource(registry, cache, key, context)
            registry.register_mode(AgentResultSource, mode, source, key=key)
            sources.append(source)
    return sources
