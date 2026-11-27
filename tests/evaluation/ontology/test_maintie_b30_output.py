"""#241：maintie_b30 CLI 输出排他新建，重跑要显式 --overwrite 才覆盖。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
FIXTURE = "fixtures/ontology/maintie/sample.json"


def _run(*args, cwd=ROOT):
    env = dict(os.environ, PYTHONPATH=str(ROOT), PYTHONIOENCODING="utf-8")
    return subprocess.run([sys.executable, "-m", "plantkg.evaluation.ontology.maintie_b30", *args], cwd=cwd, env=env,
                          capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)


def test_maintie_cli_refuses_overwrite_then_allows_it(tmp_path):
    output = tmp_path / "maintie.json"

    first = _run(FIXTURE, "--output", str(output))
    assert first.returncode == 0, first.stderr
    assert output.exists()
    original = output.read_bytes()

    again = _run(FIXTURE, "--output", str(output))
    assert again.returncode == 2
    assert "已存在" in again.stderr
    assert output.read_bytes() == original

    overwrite = _run(FIXTURE, "--output", str(output), "--overwrite")
    assert overwrite.returncode == 0, overwrite.stderr
    json.loads(output.read_text(encoding="utf-8"))
