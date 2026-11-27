import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:  # B02 的 pyproject 落地前，契约测试自行定位仓库根
    sys.path.insert(0, str(ROOT))

CONTRACTS = ROOT / "contracts"


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def openapi():
    return load_json(CONTRACTS / "openapi" / "plantkg.openapi.json")


@pytest.fixture(scope="session")
def mcp_tools():
    return {t["name"]: t for t in load_json(CONTRACTS / "mcp" / "tools.json")["tools"]}
