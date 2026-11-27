"""目录与边界：探针实测样例与 Fake 事件分开；导入扫描能发现违规。"""

import json

from plantkg.core import contracts as c
from scripts.check_boundaries import check
from tests.skeleton.conftest import ROOT

PROBES = ROOT / "nexent_assets" / "probes"
FAKES = ROOT / "plantkg" / "testing" / "fake_events"


def test_probe_samples_and_fakes_are_separate():
    assert (PROBES / "SANITIZED.md").exists() and (PROBES / "REPORT.md").exists()
    for path in PROBES.rglob("*.json*"):
        assert '"_fake"' not in path.read_text(encoding="utf-8", errors="ignore"), f"{path} 混入了 Fake 事件"
    fakes = list(FAKES.glob("*.fake.jsonl"))
    assert fakes
    for path in fakes:
        for line in path.read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            assert row["_fake"] is True
            c.UIEvent.model_validate(row["event"])


def test_repository_passes_boundary_check():
    assert check(ROOT) == []


def _write(root, rel, text):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_boundary_check_detects_violations(tmp_path):
    _write(tmp_path, "plantkg/core/bad.py", "from plantkg.packs.pv import x\nimport pyomo\n")
    _write(tmp_path, "plantkg/packs/pv/pack.py", "from plantkg.packs.storage import y\nfrom . import z\n")
    _write(tmp_path, "plantkg/decision/planner.py", "from plantkg.world.replay import state\nimport pyomo\n")
    _write(tmp_path, "plantkg/api.py", "from plantkg.decision import planner\n")
    _write(tmp_path, "web/src/client.ts", "const k = import.meta.env.VITE_NEXENT_KEY\n")
    found = "\n".join(check(tmp_path))
    assert "core/bad.py:1" in found and "core/bad.py:2" in found  # core→packs、core→第三方
    assert "packs/pv/pack.py:1" in found and "packs/pv/pack.py:2" not in found  # 包内相对导入合法
    assert "decision/planner.py:1" in found and "decision/planner.py:2" not in found  # 平面可用第三方
    assert "api.py:1" in found and "web/src/client.ts:1" in found
