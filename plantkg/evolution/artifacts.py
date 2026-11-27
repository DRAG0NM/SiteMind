"""B32 版本化产物：policy 组件与调查 Skill 模板，内容寻址、可隔离复制。"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from plantkg.core.contracts import Actor, ComponentRef, ErrorCode, PlantkgError, Role
from plantkg.core.versions import canonical, read


class BodyArtifacts:
    """与 B04 同一数据库的不可变产物；ref 前缀区分组件类型，摘要作版本引用。"""

    def __init__(self, writer: Any, plant_id: str, table: str, prefix: str):
        self.writer, self.plant_id, self.table, self.prefix = writer, plant_id, table, prefix
        writer.submit(lambda con: con.execute(
            f"CREATE TABLE IF NOT EXISTS {table} "
            "(plant_id TEXT NOT NULL, digest TEXT NOT NULL, body TEXT NOT NULL, "
            "PRIMARY KEY (plant_id, digest))"))

    def prepare(self, body: dict[str, Any], actor: Actor) -> ComponentRef:
        if Role.reviewer not in actor.roles:
            raise PlantkgError(ErrorCode.FORBIDDEN, "候选产物须由复核员确认后写入")
        encoded = canonical(body)
        sha = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        self.writer.submit(lambda con: con.execute(
            f"INSERT OR IGNORE INTO {self.table} (plant_id,digest,body) VALUES (?,?,?)",
            (self.plant_id, sha, encoded)))
        return ComponentRef(ref=self.prefix + sha, content_hash="sha256:" + sha)

    def load(self, component: ComponentRef) -> dict[str, Any]:
        if not component.ref.startswith(self.prefix):
            raise PlantkgError(ErrorCode.PRECONDITION, f"不是 {self.prefix} 产物")
        sha = component.ref.removeprefix(self.prefix)
        row = read(self.writer, lambda con: con.execute(
            f"SELECT body FROM {self.table} WHERE plant_id=? AND digest=?", (self.plant_id, sha)).fetchone())
        if row is None or component.content_hash != "sha256:" + sha or hashlib.sha256(row[0].encode("utf-8")).hexdigest() != sha:
            raise PlantkgError(ErrorCode.DEPENDENCY_UNAVAILABLE, "产物缺失或摘要不符")
        return json.loads(row[0])

    def verify(self, component: ComponentRef) -> str:
        try:
            self.load(component)
            return "complete"
        except PlantkgError:
            return "corrupt"

    def copy(self, component: ComponentRef, writer: Any, plant_id: str) -> ComponentRef:
        """把产物正文复制到隔离库，返回同一 ref；trial 清单的校验和摘要仍能对上。"""
        target = BodyArtifacts(writer, plant_id, self.table, self.prefix)
        body = self.load(component)
        target.writer.submit(lambda con: con.execute(
            f"INSERT OR IGNORE INTO {self.table} (plant_id,digest,body) VALUES (?,?,?)",
            (plant_id, component.ref.removeprefix(self.prefix), canonical(body))))
        return component


def policy_artifacts(writer: Any, plant_id: str) -> BodyArtifacts:
    return BodyArtifacts(writer, plant_id, "b32_policy_artifacts", "policy:")


def skill_artifacts(writer: Any, plant_id: str) -> BodyArtifacts:
    return BodyArtifacts(writer, plant_id, "b32_skill_artifacts", "b32-skill:")


def sensor_artifacts(writer: Any, plant_id: str) -> BodyArtifacts:
    return BodyArtifacts(writer, plant_id, "b32_sensor_artifacts", "b32-sensor:")
