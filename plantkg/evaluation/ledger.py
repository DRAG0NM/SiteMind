"""Issue #244：实验入口共用的调用账本（预算门禁 + 调用记录）。

`BudgetedCollector` 只覆盖 `scripts/evaluate_scenarios.py` 一条入口；B23（`scripts/nexent_live_scenario.py`）、
weird_combo（`nexent_assets/probes/p_b27_slow/suite.py`）和模型健康检查直接调 Nexent / HTTP，绕过了预算。
本模块给出一个与 `BudgetedCollector` 账本文件同构的 `CallLedger`：同一个 JSON 数组、同样以
`reserved_cny` / `actual_cny` / `status` / `kind` / `model` 记账，因此 B33/D54 与 B23/weird_combo
可以共用一个账本文件，两条路径看到的是同一份"已预留 / 已调用"计数。

约定（与 #244 的费用口径一致）：

* 请求前先预留并落盘（tmp + replace + fsync），失败也保留记录并写 `error`，不在失败后自动继续批量调用；
* `actual_cny` 保持 `None`：客户端只有"预留"，没有阿里云账单，不能把预留冒充真实费用；
* `request_id` 记我们自己发出的请求号；`model_calls` 记这次入口调用触发的北向运行次数
  （Nexent 内部逐轮模型调用不对外暴露，无法计数，`usage` 同理只有响应真的给了 token 用量才写）。
"""

from __future__ import annotations

import json
import math
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

DEFAULT_BUDGET_CNY = 5.0
DEFAULT_PER_CALL_CNY = 0.30
DEFAULT_MAX_CALLS = 15
LIMIT_MESSAGE = "collection call or reserved budget limit reached"  # 与 BudgetedCollector 同一句话，便于统一判读
LEDGER_ENV = "ISSUE244_LEDGER"  # 独立租户实验的统一账本位置；四条入口都先读它，避免漏传参数就另起一本账


def resolve_ledger_default(default: str | Path) -> str:
    """入口账本的默认值：环境变量 `ISSUE244_LEDGER` 优先，否则用入口自己锚定的仓库内路径。

    路径写错不会报错（构造账本时会建目录），所以只认显式环境变量与固定默认值，
    不跟随 CWD，也不因为换工作树凭空多出一本账。
    """
    override = os.environ.get(LEDGER_ENV)
    return override if override else str(default)


class CallLedger:
    """一个批次一份账本：累计预留、调用上限与逐条调用记录。

    参数与 `plantkg.evaluation.collection.BudgetedCollector` 对齐（budget_cny / per_call_cny / max_calls），
    多一个 `model` 与 `source`：记录里写清模型名与调用来源（b23 / weird-combo / healthcheck / b33）。
    """

    def __init__(self, path: str | Path, *, model: str, budget_cny: float = DEFAULT_BUDGET_CNY,
                 per_call_cny: float = DEFAULT_PER_CALL_CNY, max_calls: int = DEFAULT_MAX_CALLS,
                 source: str = "nexent") -> None:
        budget_cny = float(budget_cny)
        per_call_cny = float(per_call_cny)
        if not math.isfinite(budget_cny) or budget_cny <= 0:
            raise ValueError("budget_cny 必须是正的有限数")
        if not math.isfinite(per_call_cny) or per_call_cny <= 0:
            raise ValueError("per_call_cny 必须是正的有限数")
        if per_call_cny > budget_cny:
            raise ValueError("per_call_cny 不能大于 budget_cny")
        if int(max_calls) <= 0:
            raise ValueError("max_calls 必须是正整数")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        self.model = model
        self.source = source
        self.budget = budget_cny
        self.per_call = per_call_cny
        self.max_calls = int(max_calls)
        self._guard = threading.RLock()
        self._lock_fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.write(self._lock_fd, f"{os.getpid()} {datetime.now(timezone.utc).isoformat()}\n".encode("utf-8"))
        try:
            self.records = self._load()
        except Exception:  # 账本读不了就别把锁留在磁盘上，否则后续每次启动都被自己挡住
            os.close(self._lock_fd)
            self._lock_fd = None
            self.lock_path.unlink(missing_ok=True)
            raise
        self.reserved = sum(float(record.get("reserved_cny") or 0.0) for record in self.records)

    # ---- 账本文件 -------------------------------------------------------
    def _load(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8-sig"))  # Windows 工具可能写 BOM，读时容忍
        except ValueError as exc:
            raise ValueError(f"调用账本不是合法 JSON：{self.path}（{exc}）") from exc
        if not isinstance(data, list):
            raise ValueError(f"调用账本必须是 JSON 数组：{self.path}")
        return data

    def _save(self) -> None:
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(self.records, handle, ensure_ascii=False, indent=1)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, self.path)

    def close(self) -> None:
        try:
            if self._lock_fd is not None:
                os.close(self._lock_fd)
                self._lock_fd = None
        finally:
            self.lock_path.unlink(missing_ok=True)

    def __enter__(self) -> "CallLedger":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    # ---- 预算门禁 -------------------------------------------------------
    @property
    def remaining_cny(self) -> float:
        return self.budget - self.reserved

    @property
    def remaining_calls(self) -> int:
        return self.max_calls - len(self.records)

    def check(self) -> None:
        """只查不写：调用方可以在真正发请求前先问一句"还能不能调"。"""
        with self._guard:
            if len(self.records) >= self.max_calls or self.reserved + self.per_call > self.budget + 1e-9:
                raise ValueError(LIMIT_MESSAGE)

    def reserve(self, kind: str, *, request_id: str | None = None, model_calls: int = 1,
                note: str | None = None) -> dict[str, Any]:
        """先记账再发请求：超限直接拒绝，没有"失败了重试一次再说"的余地。"""
        with self._guard:
            self.check()
            record = {
                "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "kind": str(kind),
                "model": self.model,
                "source": self.source,
                "request_id": request_id,
                "model_calls": int(model_calls),
                "reserved_cny": self.per_call,
                "actual_cny": None,
                "usage": None,
                "status": "started",
                "note": note,
            }
            self.records.append(record)
            self.reserved += self.per_call
            self._save()
            return record

    def finish(self, record: Mapping[str, Any], *, status: str, usage: Any = None, error: str | None = None,
               request_id: str | None = None, model_calls: int | None = None,
               elapsed_ms: int | None = None) -> None:
        """回填结果；`actual_cny` 一律不写，费用口径仍以阿里云账单为准。"""
        with self._guard:
            for item in self.records:
                if item is record or (isinstance(item, dict) and item.get("at") == record.get("at")
                                      and item.get("kind") == record.get("kind")):
                    item["status"] = str(status)
                    item["usage"] = usage
                    if error is not None:
                        item["error"] = str(error)[:2000]
                    if request_id is not None:
                        item["request_id"] = request_id
                    if model_calls is not None:
                        item["model_calls"] = int(model_calls)
                    if elapsed_ms is not None:
                        item["elapsed_ms"] = int(elapsed_ms)
                    self._save()
                    return
            raise ValueError(f"账本里找不到这条预留：{record.get('at')} {record.get('kind')}")

    def snapshot(self) -> dict[str, Any]:
        with self._guard:
            return {
                "path": str(self.path),
                "model": self.model,
                "source": self.source,
                "budget_cny": self.budget,
                "per_call_cny": self.per_call,
                "max_calls": self.max_calls,
                "reserved_cny": self.reserved,
                "remaining_cny": self.remaining_cny,
                "calls": len(self.records),
                "remaining_calls": self.remaining_calls,
                # 预留不是账单：这里只给"客户端预留的金额上限"
                "actual_cny_visible": False,
            }


class LedgeredSource:
    """把任意 AgentResultSource 包一层账本：每次 get() 先预留，失败也留证据。

    一次 get() 可能触发多次北向运行（`LiveAgentResultSource` 在 timeout/error/stream_lost 时会重试），
    结果里的 `retries` 就是这个次数-1，据此回填 `model_calls`。
    """

    def __init__(self, ledger: CallLedger, source: Any, *, kind: str, note: str | None = None) -> None:
        self._ledger = ledger
        self._source = source
        self._kind = kind
        self._note = note

    def get(self, kind: Any, binding: Any, payload: Mapping[str, Any]) -> Any:
        record = self._ledger.reserve(self._kind, note=self._note)
        started = time.perf_counter()
        try:
            result = self._source.get(kind, binding, payload)
        except Exception as exc:
            self._ledger.finish(record, status="error", error=f"{type(exc).__name__}: {exc}",
                                elapsed_ms=int((time.perf_counter() - started) * 1000))
            raise
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        if isinstance(result, Mapping):
            retries = result.get("retries")
            self._ledger.finish(record, status=str(result.get("status") or "unknown"),
                                usage=result.get("usage"),
                                # 网关实际发出去的 X-Request-Id 就是 agent_run_id
                                request_id=result.get("request_id") or result.get("agent_run_id"),
                                model_calls=(int(retries) + 1) if isinstance(retries, int) else None,
                                elapsed_ms=elapsed_ms)
        else:
            self._ledger.finish(record, status="ok", elapsed_ms=elapsed_ms)
        return result
