from __future__ import annotations

import os
import subprocess
import sys


def test_first_act_demo_outputs_conflict_conversion_and_result() -> None:
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    result = subprocess.run(
        [sys.executable, "-m", "plantkg.agent.arbitration.demo", "第一幕剧本"],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
    )

    assert result.returncode == 0, result.stderr
    assert "冲突" in result.stdout
    assert "换算" in result.stdout
    assert "裁决结果" in result.stdout
