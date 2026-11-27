"""Issue #244 目录探测脚本：非 2xx 不能崩，且必须记下状态码与响应内容、写出 summary。

用本机 HTTP 桩代替百炼（不发真实云端请求、不用真 Key）：共享域名桩恒回 401（模拟 Key 无效），
工作空间桩对 `does-not-exist` 与单模型路由回 404、对清单回 200。
pwsh 7 下 401/404 走 `-SkipHttpErrorCheck`，5.1 下走 try/catch，两条路径都必须通过本测试。
"""

import json
import os
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "nexent_assets" / "probes" / "p_catalog" / "query_catalog.ps1"
SHELL = shutil.which("pwsh") or shutil.which("powershell")

pytestmark = pytest.mark.skipif(not SCRIPT.is_file(), reason="目录探测脚本不在本检出里")


class _Stub(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - http.server 的约定方法名
        if self.server.mode == "unauthorized":
            self._send(401, {"code": "InvalidApiKey", "message": "Invalid API-key provided."})
        elif "does-not-exist" in self.path:
            self._send(404, {"code": "model_not_found", "message": "model not found"})
        elif self.path.startswith("/compatible-mode/v1/models/"):
            self._send(404, {"error": {"message": "The model does not exist"}})
        elif self.path.startswith("/compatible-mode/v1/models"):
            self._send(200, {"data": [{"id": "qwen4-max"}]})
        else:
            self._send(200, {"output": {"total": 1, "models": [{"model": "qwen4-max"}]}})

    def _send(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # 测试里不要往 stderr 刷访问日志
        pass


def _serve(mode):
    server = HTTPServer(("127.0.0.1", 0), _Stub)
    server.mode = mode
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    """跑一次脚本：401 桩 + 404 桩，返回 (exit code, summary 行, 输出目录)。"""
    if SHELL is None:
        pytest.skip("没有 pwsh / powershell，跳过目录脚本验证")
    unauthorized, shared_url = _serve("unauthorized")
    mixed, ws_url = _serve("mixed")
    out = tmp_path_factory.mktemp("catalog")
    env = {**os.environ, "DASHSCOPE_API_KEY": "sk-test-not-a-real-key"}
    try:
        proc = subprocess.run(
            [SHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(SCRIPT),
             "-WorkspaceId", "ws-test0000000000", "-OutputDir", str(out),
             "-SharedBaseUrl", shared_url, "-WorkspaceBaseUrl", ws_url],
            env=env, capture_output=True, text=True, timeout=120,
        )
    finally:
        unauthorized.shutdown()
        mixed.shutdown()

    summaries = sorted(out.glob("*_summary.json"))
    return proc, summaries, out


def test_non_2xx_does_not_crash_and_writes_summary(run):
    proc, summaries, _ = run
    assert proc.returncode == 0, proc.stderr
    assert summaries, "非 2xx 也必须写出 summary 文件"


def test_script_keeps_the_powershell7_branch():
    """本机只有 5.1，桩只能跑 5.1 路径；这条静态断言守住 pwsh 7 的读取方式不被改回去。"""
    text = SCRIPT.read_text(encoding="utf-8-sig")
    assert "SkipHttpErrorCheck" in text  # pwsh 7 用它将 4xx/5xx 当普通响应取回
    assert "$PSVersionTable.PSVersion.Major" in text  # 版本分支：7 走上面，5.1 走 try/catch
    assert text.count(".GetResponseStream()") == 1  # 只允许留在 5.1 的兜底读取里
    assert "ErrorDetails.Message" in text  # 5.1 的响应体实际来自它（流已被读空）


def test_401_and_404_record_status_and_body(run):
    _, summaries, out = run
    rows = json.loads(summaries[0].read_text(encoding="utf-8-sig"))
    by_target = {row["target"]: row for row in rows}

    # 共享域名桩恒回 401：状态码与响应体都要记下来
    shared = [row for name, row in by_target.items() if name.startswith("shared_")]
    assert shared and all(row["http"] == 401 for row in shared)
    assert all(row["body_file"] for row in shared)
    assert "InvalidApiKey" in (out / shared[0]["body_file"]).read_text(encoding="utf-8-sig")

    # 工作空间桩：清单 200、缺模型与单模型路由 404
    assert by_target["ws_api_exact"]["http"] == 200
    assert "total=1" in by_target["ws_api_exact"]["note"]
    expected_body = {"ws_api_missing": "model not found", "ws_compat_one": "does not exist"}
    for name, fragment in expected_body.items():
        assert by_target[name]["http"] == 404
        assert by_target[name]["body_file"]
        assert fragment in (out / by_target[name]["body_file"]).read_text(encoding="utf-8-sig")
