"""Explicit, bounded model collection through the B23 AgentResultSource."""
from __future__ import annotations

import json
import math
import os
import time
import threading
from datetime import datetime, timezone
from pathlib import Path

from plantkg.core.contracts import AgentResultKind
from plantkg.evaluation.cache import ArbitrationCollector


class BudgetedCollector:
    """Reserve a conservative per-attempt ceiling before each live request.

    The ceiling must include all server-side steps, tokens, tools and retries. It
    is a reservation, not a claim about billed cost; missing usage stays unknown.
    """
    cache_source = 'nexent'

    def __init__(self, source, *, model, budget_cny, per_call_cny, max_calls, log_path):
        if (not model or not math.isfinite(budget_cny) or not math.isfinite(per_call_cny) or
                budget_cny <= 0 or per_call_cny <= 0 or per_call_cny > budget_cny or max_calls <= 0):
            raise ValueError('model and positive budget, per-call ceiling and call limit are required')
        self.source, self.model_version = source, model
        self.budget, self.per_call, self.max_calls = budget_cny, per_call_cny, max_calls
        self.path = Path(log_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_path = self.path.with_suffix(self.path.suffix + '.lock')
        self.lock_fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            self.records = json.loads(self.path.read_text(encoding='utf-8-sig')) if self.path.exists() else []
        except Exception:  # 账本读不了就别把锁留着，否则后续每次启动都被自己挡住
            os.close(self.lock_fd)
            self.lock_fd = None
            self.lock_path.unlink(missing_ok=True)
            raise
        self.reserved = sum(float(x['reserved_cny']) for x in self.records)
        self.lock = threading.RLock()

    def close(self):
        if self.lock_fd is not None:
            os.close(self.lock_fd)
            self.lock_fd = None
            self.lock_path.unlink()

    def get(self, kind, binding, payload):
        with self.lock:
            if len(self.records) >= self.max_calls or self.reserved + self.per_call > self.budget + 1e-9:
                raise ValueError('collection call or reserved budget limit reached')
            record = {'at': datetime.now(timezone.utc).isoformat(), 'kind': AgentResultKind(kind).value,
                      'model': self.model_version, 'source': self.cache_source, 'request_id': None,
                      'model_calls': 1, 'reserved_cny': self.per_call, 'actual_cny': None,
                      'usage': None, 'status': 'started'}
            self.records.append(record)
            self.reserved += self.per_call
            self._save()  # reserve before a request, including crashes or timeouts
        started = time.monotonic()
        try:
            if AgentResultKind(kind) == AgentResultKind.arbitration:
                captured = []

                class Capture:
                    def get(inner, result_kind, run_binding, request):
                        response = dict(self.source.get(result_kind, run_binding, request))
                        captured.append(response)
                        return response

                result = ArbitrationCollector(Capture(), 1).get(kind, binding, payload)
                record['response'] = captured[0].get('final_answer') if captured else None
            else:
                request = dict(payload)
                if AgentResultKind(kind) != AgentResultKind.expert and not request.get('query'):
                    request['query'] = '根据以下输入完成任务并输出结果：' + json.dumps(request, ensure_ascii=False, default=str)[:6500]
                result = dict(self.source.get(kind, binding, request))
            record['status'] = str(result.get('status') or 'unknown')
            record['agent_run_id'] = result.get('agent_run_id')
            # X-Request-Id 就是网关发给 Nexent 的 agent_run_id；Nexent 内部逐轮模型调用不对外暴露，
            # 这里只记我们自己发起的北向运行次数（1 + 重试），不含下游轮数。
            record['request_id'] = result.get('agent_run_id')
            record['model_calls'] = 1 + int(result.get('retries') or 0)
            if 'usage' in result:
                record['usage'] = result.get('usage')
            if 'response' not in record:
                record['response'] = result.get('final_answer')
            record['terminal_reason'] = result.get('terminal_reason')
            return result
        except Exception as exc:
            record['status'] = 'failed'
            record['error_type'] = type(exc).__name__
            raise
        finally:
            with self.lock:
                record['elapsed_ms'] = round((time.monotonic() - started) * 1000)
                self._save()

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + '.tmp')
        temporary.write_text(json.dumps(self.records, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(self.path)
