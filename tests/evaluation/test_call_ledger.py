from types import SimpleNamespace

import pytest

from plantkg.core.contracts import AgentResultKind
from plantkg.core.ports import AgentResultSource
from plantkg.core.registry import Registry
from plantkg.evaluation.collection import BudgetedCollector
from plantkg.evaluation.ledger import CallLedger, LedgeredSource, resolve_ledger_default


class Source:
    def __init__(self, result=None, error=None):
        self.result = result if result is not None else {'status': 'finished', 'final_answer': 'ok', 'retries': 0}
        self.error = error
        self.calls = 0

    def get(self, kind, binding, payload):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.result


def test_reserve_happens_before_request_and_is_finished_after(tmp_path):
    seen = {}
    source = Source({'status': 'finished', 'final_answer': 'ok', 'retries': 0, 'agent_run_id': 'ar-1'})

    class Spying:
        def get(self, kind, binding, payload):
            seen['records'] = len(ledger.records)
            seen['status'] = ledger.records[0]['status']
            return source.get(kind, binding, payload)

    ledger = CallLedger(tmp_path / 'calls.json', model='qwen4-max', budget_cny=2, per_call_cny=1, max_calls=2,
                        source='b23')
    try:
        result = LedgeredSource(ledger, Spying(), kind='expert').get(AgentResultKind.expert, None, {})
        assert result['final_answer'] == 'ok'
        assert seen == {'records': 1, 'status': 'started'}  # 请求发出前已经落盘预留
        record = ledger.records[0]
        assert record['status'] == 'finished' and record['source'] == 'b23' and record['kind'] == 'expert'
        assert record['request_id'] == 'ar-1' and record['model_calls'] == 1
        assert record['actual_cny'] is None  # 预留不是账单
        assert ledger.reserved == 1
    finally:
        ledger.close()


def test_limit_refuses_before_any_request(tmp_path):
    ledger = CallLedger(tmp_path / 'calls.json', model='qwen4-max', budget_cny=2, per_call_cny=1, max_calls=1)
    source = Source()
    try:
        LedgeredSource(ledger, source, kind='expert').get(AgentResultKind.expert, None, {})
        with pytest.raises(ValueError, match='limit'):
            LedgeredSource(ledger, source, kind='expert').get(AgentResultKind.expert, None, {})
        assert source.calls == 1
        with pytest.raises(FileExistsError):
            CallLedger(tmp_path / 'calls.json', model='qwen4-max', budget_cny=2, per_call_cny=1, max_calls=1)
    finally:
        ledger.close()


def test_error_is_recorded_and_not_retried(tmp_path):
    ledger = CallLedger(tmp_path / 'calls.json', model='qwen4-max', budget_cny=2, per_call_cny=1, max_calls=2)
    source = Source(error=RuntimeError('northbound down'))
    try:
        with pytest.raises(RuntimeError, match='northbound down'):
            LedgeredSource(ledger, source, kind='expert').get(AgentResultKind.expert, None, {})
        assert source.calls == 1
        assert ledger.records[0]['status'] == 'error'
        assert 'northbound down' in ledger.records[0]['error']
    finally:
        ledger.close()


def test_b23_and_b33_share_one_cumulative_ledger(tmp_path):
    path = tmp_path / 'batch.json'
    binding = SimpleNamespace(ticket_id='t')
    collector = BudgetedCollector(Source(), model='qwen4-max', budget_cny=2, per_call_cny=1, max_calls=2, log_path=path)
    try:
        collector.get(AgentResultKind.follow_up, binding, {'query': 'q'})
    finally:
        collector.close()
    ledger = CallLedger(path, model='qwen4-max', budget_cny=2, per_call_cny=1, max_calls=2, source='b23')
    try:
        assert ledger.reserved == 1 and ledger.remaining_calls == 1  # 读到 B33 的预留，不重置额度
        LedgeredSource(ledger, Source(), kind='expert').get(AgentResultKind.expert, None, {})
        with pytest.raises(ValueError, match='limit'):
            LedgeredSource(ledger, Source(), kind='expert').get(AgentResultKind.expert, None, {})
    finally:
        ledger.close()


def test_entry_defaults_share_one_ledger_unless_override(monkeypatch):
    # 独立租户实验靠 ISSUE244_LEDGER 把四条入口指向同一本账；未设置时保持入口自己锚定的仓库路径
    monkeypatch.delenv('ISSUE244_LEDGER', raising=False)
    assert resolve_ledger_default('/repo/runtime/issue244-batch-budget.json') == '/repo/runtime/issue244-batch-budget.json'
    monkeypatch.setenv('ISSUE244_LEDGER', '/tenant/runtime/issue244-batch-budget.tenant.json')
    assert resolve_ledger_default('/repo/runtime/issue244-batch-budget.json') == '/tenant/runtime/issue244-batch-budget.tenant.json'


def test_wrap_live_with_ledger_only_wraps_live_sources(tmp_path):
    from scripts.nexent_live_scenario import wrap_live_with_ledger

    registry = Registry()
    live, trial, evaluation = Source(), Source(), Source()
    registry.mode_ports[(AgentResultSource, 'live', 'expert')] = live
    registry.mode_ports[(AgentResultSource, 'trial', 'expert')] = trial
    registry.mode_ports[(AgentResultSource, 'evaluation', 'expert')] = evaluation
    ledger = CallLedger(tmp_path / 'calls.json', model='qwen4-max', budget_cny=2, per_call_cny=1, max_calls=2,
                        source='b23')
    try:
        wrapped = wrap_live_with_ledger(SimpleNamespace(registry=registry), ledger)
        assert wrapped == ['expert']
        assert isinstance(registry.mode_ports[(AgentResultSource, 'live', 'expert')], LedgeredSource)
        assert registry.mode_ports[(AgentResultSource, 'trial', 'expert')] is trial
        assert registry.mode_ports[(AgentResultSource, 'evaluation', 'expert')] is evaluation
    finally:
        ledger.close()
