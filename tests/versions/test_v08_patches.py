"""B46 旧模块补丁：版本清单存取并激活 policy 组件、“知识 vN”、统一确认登记新的待确认动作类型。"""

import pytest

from plantkg.core.actions import SqlitePendingActionStore
from plantkg.core.contracts import PlantkgError
from tests.versions.conftest import PLANT, REVIEWER, component, context


def _publish(versions, n, reason):
    m = versions.prepare({"rules": component("t:rules", n), "policy": component("t:policy", n)}, reason)
    versions.check_integrity(m.version_manifest_id)
    expected = versions.active().version_manifest_id if n > 1 else None
    return versions.activate(expected, m.version_manifest_id, REVIEWER, reason)


def test_policy_component_and_knowledge_version(versions):
    v1 = _publish(versions, 1, "首版")
    v2 = _publish(versions, 2, "新电价")
    assert v2.components["policy"].ref == "t:policy"
    assert versions.knowledge_version(v1.version_manifest_id) == 1 and versions.knowledge_version(v2.version_manifest_id) == 2
    versions.activate(v2.version_manifest_id, v1.version_manifest_id, REVIEWER, "回滚")
    assert versions.knowledge_version(v1.version_manifest_id) == 1  # 回滚不改编号
    draft = versions.prepare({"rules": component("t:rules", 9)}, "未激活")
    with pytest.raises(PlantkgError):
        versions.knowledge_version(draft.version_manifest_id)


@pytest.mark.parametrize("kind", ["work_order", "correction", "sampling", "human_decision"])
def test_new_pending_action_kinds(writer, kind):
    store = SqlitePendingActionStore(writer, PLANT, {})
    action = store.propose(kind, {"target_ref": "ticket://tk-0001"}, context(idempotency_key=f"v08-{kind}"))
    assert action.kind == kind and action.status == "proposed"
