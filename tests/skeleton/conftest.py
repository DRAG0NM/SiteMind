import shutil
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from plantkg.api import create_app  # noqa: E402
from plantkg.bootstrap import build_services  # noqa: E402
from plantkg.core.config import load_settings  # noqa: E402
from plantkg.mcp_server import create_mcp_server  # noqa: E402


@pytest.fixture
def settings():
    return load_settings(profile="mock")


@pytest.fixture
def services(settings):
    return build_services(settings)


@pytest.fixture
def client(services):
    from starlette.testclient import TestClient

    with TestClient(create_app(services, create_mcp_server(services))) as c:
        yield c


@pytest.fixture
def config_dir(tmp_path):
    """可修改的配置目录副本。"""
    target = tmp_path / "configs"
    shutil.copytree(ROOT / "configs", target)
    return target


@pytest.fixture
def edit_config(config_dir):
    """edit_config(文件名, 函数)：读出 yaml、交给函数修改、写回。"""

    def edit(name, mutate):
        path = config_dir / name
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        mutate(data)
        path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")

    return edit
