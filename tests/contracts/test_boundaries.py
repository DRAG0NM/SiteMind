"""依赖方向、端口可表达性、生成物最新、任务卡覆盖。"""

import ast
import inspect
import sys
import typing

import pytest

from plantkg.core import contracts as c
from plantkg.core import ports
from tests.contracts.conftest import ROOT

CORE = ROOT / "plantkg" / "core"
ALLOWED_THIRD_PARTY = {"pydantic", "yaml"}  # yaml 供 core.config 读分层配置（B02）


def imported_roots(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            yield node.module


@pytest.mark.parametrize("path", sorted(CORE.glob("*.py")), ids=lambda p: p.name)
def test_core_imports_only_stdlib_pydantic_core(path):
    for module in imported_roots(path):
        top = module.split(".")[0]
        if top in sys.stdlib_module_names or top in ALLOWED_THIRD_PARTY or top == "__future__":
            continue
        assert module.startswith("plantkg.core"), f"{path.name} 导入了 {module}：core 不得依赖平面、包或后端"


def port_protocols():
    return {
        name: obj
        for name, obj in vars(ports).items()
        if inspect.isclass(obj) and getattr(obj, "_is_protocol", False) and obj.__module__ == ports.__name__
    }


def test_all_ports_expressible_with_contract_types():
    contract_names = set(c.record_models()) | set(c.enum_types()) | set(port_protocols())
    for name, proto in port_protocols().items():
        for member_name, member in vars(proto).items():
            if member_name.startswith("_"):
                continue
            func = member.fget if isinstance(member, property) else member
            if not callable(func):
                continue
            hints = typing.get_type_hints(func, globalns=vars(ports))  # 解析失败即端口引用了不存在的类型
            for hint in hints.values():
                for arg in _flatten(hint):
                    module = getattr(arg, "__module__", "")
                    if module.startswith("plantkg"):
                        assert arg.__name__ in contract_names, f"{name}.{member_name} 引用了非契约类型 {arg}"


def _flatten(hint):
    yield hint
    for arg in typing.get_args(hint):
        yield from _flatten(arg)


def test_required_ports_present():
    required = {"DomainPack", "Adapter", "Judge", "RuleKind", "CandidateGenerator", "StateProvider", "Executor", "Forecaster",
                "Planner", "Gate", "Repository", "VersionStore", "EvidenceSink", "ModelBuilder", "CtxResolver",
                "RunSessionStore", "JobRunner", "PendingActionStore", "Gateway", "Evaluator", "Backtester", "Explainer", "Calibrator"}
    assert required <= set(port_protocols())


def test_model_builder_contract():
    """04 规划模型契约：包只能经这些方法接触模型。"""
    members = {m for m in vars(ports.ModelBuilder) if not m.startswith("_")}
    assert members == {"add_var", "add_constraint", "register_balance", "register_cost", "time_set", "grid_import", "grid_export",
                       "pv_available", "total_load"}


def test_generated_artifacts_up_to_date():
    sys.path.insert(0, str(ROOT / "contracts"))
    import build

    for rel, text in build.generate().items():
        current = (ROOT / "contracts" / rel).read_text(encoding="utf-8")
        assert current == text, f"{rel} 过期，运行 python contracts/build.py"
