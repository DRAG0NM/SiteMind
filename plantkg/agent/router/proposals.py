"""提案存取（B46 的 ProposalSink，B27 实现）：落库时分配 proposal_id，回答经 attach_reply 挂到同一提案上。

B23 的专家工具包装层只认这个端口；提案就绪钩子按（事件单, 轮次）取本轮提案，旧轮提案不拼入新一轮。
"""

from __future__ import annotations

from typing import Any

from plantkg.core.contracts import ErrorCode, ExpertProposal, ExpertReply, PlantkgError, RunBinding
from plantkg.core.versions import atomic, read

PREFIX = "proposal://"


class SqliteProposalSink:
    """与 B03 共用写者，表 b27_proposals（migrations/proposals）。

    分叉后的子运行库用 parent 接父运行库：序号在数据库内递增，按 ID 召回与事件单历史沿链解析，
    父运行提案在子运行里仍按原 ID 可读、可回复。落库作用域按提案的 binding.run_id 定：在途的 live 调用
    跨分叉返回时仍写回父运行库，不跟着当前运行换库。
    """

    def __init__(self, writer: Any, plant_id: str, parent: SqliteProposalSink | None = None,
                 *, run_id: str | None = None) -> None:
        self.writer, self.plant_id, self.parent, self.run_id = writer, plant_id, parent, run_id

    def _scopes(self) -> tuple[str, ...]:
        """整条运行链的作用域：本运行在前，父运行历史依次在后。"""
        return tuple(sink.plant_id for sink in self._chain())

    def _chain(self) -> tuple[SqliteProposalSink, ...]:
        return (self, *self.parent._chain()) if self.parent else (self,)

    def _owning_scope(self, binding: RunBinding) -> str:
        """提案所属运行的作用域；链路里没有该运行（如装配时的厂区级库）就用本库作用域。"""
        for sink in self._chain():
            if sink.run_id is not None and sink.run_id == binding.run_id:
                return sink.plant_id
        return self.plant_id

    def save(self, proposal: ExpertProposal, binding: RunBinding) -> str:
        stored = proposal.model_copy(update={"proposal_id": None, "binding": binding})
        stored = ExpertProposal.model_validate(stored.model_dump())
        scope = self._owning_scope(binding)

        def write(con):
            # 旧父库引用也可能迟到保存；在同一写事务里分配编号，不能只看当前库与祖先。
            seq = con.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM b27_proposals").fetchone()[0]
            proposal_id = f"{PREFIX}P-{seq:04d}"
            record = stored.model_copy(update={"proposal_id": proposal_id}).model_dump_json()
            con.execute("INSERT INTO b27_proposals(plant_id,proposal_id,seq,ticket_id,round,run_id,knowledge_version,expert,"
                        "trigger_at,replied,record) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (scope, proposal_id, seq, binding.ticket_id, binding.round, binding.run_id,
                         binding.knowledge_version, stored.expert, binding.trigger_at.isoformat(), 0, record))
            return proposal_id
        return atomic(self.writer, write)

    def attach_reply(self, proposal_id: str, reply: ExpertReply) -> ExpertProposal:
        stored = self.get(proposal_id)
        evidence = stored.evidence_refs + tuple(r for r in reply.evidence_refs if r not in stored.evidence_refs)
        updated = stored.model_copy(update={"diagnosis": reply.diagnosis or stored.diagnosis, "evidence_refs": evidence})
        self._write(proposal_id, ExpertProposal.model_validate(updated.model_dump()), replied=True)
        return updated

    def get(self, proposal_id: str) -> ExpertProposal:
        scopes = self._scopes()
        marks = ",".join("?" * len(scopes))
        row = read(self.writer, lambda con: con.execute(
            f"SELECT record FROM b27_proposals WHERE plant_id IN ({marks}) AND proposal_id=?",
            (*scopes, proposal_id)).fetchone())
        if row is None:
            raise PlantkgError(ErrorCode.NOT_FOUND, f"提案 {proposal_id} 不存在")
        return ExpertProposal.model_validate_json(row[0])

    def list(self, ticket_id: str, round_: int | None = None) -> list[ExpertProposal]:
        scopes = self._scopes()
        marks = ",".join("?" * len(scopes))
        sql = f"SELECT record FROM b27_proposals WHERE plant_id IN ({marks}) AND ticket_id=?"
        params: list[Any] = [*scopes, ticket_id]
        if round_ is not None:
            sql += " AND round=?"
            params.append(round_)
        rows = read(self.writer, lambda con: con.execute(sql + " ORDER BY seq", params).fetchall())
        return [ExpertProposal.model_validate_json(r[0]) for r in rows]

    def replied(self, proposal_id: str) -> bool:
        scopes = self._scopes()
        marks = ",".join("?" * len(scopes))
        row = read(self.writer, lambda con: con.execute(
            f"SELECT replied FROM b27_proposals WHERE plant_id IN ({marks}) AND proposal_id=?",
            (*scopes, proposal_id)).fetchone())
        if row is None:
            raise PlantkgError(ErrorCode.NOT_FOUND, f"提案 {proposal_id} 不存在")
        return bool(row[0])

    def _write(self, proposal_id: str, proposal: ExpertProposal, *, replied: bool) -> None:
        scopes = self._scopes()
        marks = ",".join("?" * len(scopes))
        atomic(self.writer, lambda con: con.execute(
            f"UPDATE b27_proposals SET record=?, replied=? WHERE plant_id IN ({marks}) AND proposal_id=?",
            (proposal.model_dump_json(), int(replied), *scopes, proposal_id)))
