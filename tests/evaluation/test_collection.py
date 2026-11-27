from types import SimpleNamespace

import pytest

from plantkg.core.contracts import AgentResultKind
from plantkg.evaluation.collection import BudgetedCollector


def test_budget_is_reserved_before_request_and_persisted(tmp_path):
    class Source:
        def get(self, kind, binding, payload):
            return {'status': 'finished', 'final_answer': 'ok', 'agent_run_id': 'r'}

    path = tmp_path / 'calls.json'
    collector = BudgetedCollector(Source(), model='model-v1', budget_cny=2, per_call_cny=1, max_calls=1, log_path=path)
    binding = SimpleNamespace(ticket_id='t')
    try:
        assert collector.get(AgentResultKind.follow_up, binding, {'query': 'q'})['status'] == 'finished'
        with pytest.raises(ValueError, match='limit'):
            collector.get(AgentResultKind.follow_up, binding, {'query': 'q'})
        assert collector.records[0]['actual_cny'] is None
        with pytest.raises(FileExistsError):
            BudgetedCollector(Source(), model='model-v1', budget_cny=2, per_call_cny=1, max_calls=1, log_path=path)
    finally:
        collector.close()
    restored = BudgetedCollector(Source(), model='model-v1', budget_cny=2, per_call_cny=1, max_calls=1, log_path=path)
    try:
        with pytest.raises(ValueError, match='limit'):
            restored.get(AgentResultKind.follow_up, binding, {'query': 'q'})
    finally:
        restored.close()
