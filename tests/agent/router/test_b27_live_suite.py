"""Exercise the live-suite harness against a local SSE substitute, never a model."""
import importlib.util
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
PROBE = ROOT / "nexent_assets" / "probes" / "p_b27_slow"
sys.path.insert(0, str(PROBE))
spec = importlib.util.spec_from_file_location("b27_live_suite", PROBE / "suite.py")
suite = importlib.util.module_from_spec(spec)
spec.loader.exec_module(suite)
sys.path.remove(str(PROBE))


@pytest.fixture
def fake_nexent(monkeypatch):
    seen = []
    answers = {r["id"]: r["answer"] for r in json.loads((PROBE / "suite_results.json").read_text(encoding="utf-8"))["runs"]}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append((self.headers["X-Request-Id"], body["agent_name"]))
            if body["agent_name"] == "fake_failure":
                self.send_response(503)
                self.end_headers()
                self.wfile.write(b"unavailable")
                return
            case = next(c for c in suite.CASES if body["query"].startswith(c["query"]))
            events = [{"type": "tool", "tool_name": "plantkg_trace_impact", "tool_call_id": "tool-1",
                       "tool_arguments": {"test": "fake-local-only-key"}},
                      {"type": "tool", "tool_name": "read_skill_md", "tool_call_id": "tool-2"},
                      {"type": "final_answer", "answer_phase": "complete", "content": answers[case["id"]]}]
            if body["agent_name"] == "fake_incomplete":
                events.pop()
            content = "".join("data: " + json.dumps(e, ensure_ascii=False) + "\n\n" for e in events).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    original = suite.http.client.HTTPConnection
    monkeypatch.setattr(suite.http.client, "HTTPConnection", lambda host, port, timeout: original("127.0.0.1", server.server_port, timeout=timeout))
    monkeypatch.setenv("NEXENT_NB_KEY", "fake-local-only-key")
    yield seen
    server.shutdown()
    server.server_close()
    thread.join()


def test_isolated_cli_evidence_and_required_12_sample_summary(tmp_path, fake_nexent, capsys):
    root = tmp_path / "results"
    ledger = str(tmp_path / "budget.json")
    budget = ["--budget-ledger", ledger, "--budget-cny", "10", "--max-calls", "25"]
    assert suite.main(["--agent", "isolated_test", "--output-dir", str(root), "--phase", "standalone",
                       "--cases", "weird_combo", "--runs", "10", "--request-id-prefix", "trial", *budget]) == 0
    assert suite.main(["--agent", "isolated_test", "--output-dir", str(root), "--phase", "regression",
                       "--runs", "2", *budget]) == 0
    paths = sorted(root.glob("*/results.json"))
    assert len(paths) == 2
    docs = [json.loads(p.read_text(encoding="utf-8")) for p in paths]
    runs = [r for d in docs for r in d["runs"]]
    assert len(runs) == 22 and len({r["request_id"] for r in runs}) == 22
    assert len(fake_nexent) == 22 and {a for _, a in fake_nexent} == {"isolated_test"}
    assert len(list(root.glob("*/*.sse.txt"))) == 22
    assert all(r["tool_count"] == 2 and len(r["tools"]) == 1 and r["error"] is None for r in runs)
    assert all("fake-local-only-key" not in p.read_text(encoding="utf-8") for p in root.glob("*/*"))
    summary = suite.summarize(paths)
    assert summary["standalone_samples"] == 10 and summary["regression_rounds"] == 2
    assert summary["p90"] == 2 and summary["acceptance"] is True
    only_one = next(p for p, d in zip(paths, docs) if d["phase"] == "standalone")
    assert suite.summarize([only_one])["acceptance"] is None
    assert suite.main(["--summarize", str(only_one)]) == 1
    assert '"p90": null' in capsys.readouterr().out
    with pytest.raises(ValueError, match="duplicate"):
        suite.summarize([only_one, only_one])
    for doc, path in zip(docs, paths):
        if doc["phase"] == "standalone":
            for index, run in enumerate(doc["runs"]):
                run["tool_count"] = 9 if index == 0 else 8
                if index == 0:
                    run["checks"]["budget"] = run["checks"]["pass"] = False
            path.write_text(json.dumps(doc), encoding="utf-8")
    assert suite.summarize(paths)["p90"] == 8  # ceil(0.9 * 12) = 11
    for doc, path in zip(docs, paths):
        if doc["phase"] == "standalone":
            doc["runs"][1]["tool_count"] = 9
            doc["runs"][1]["checks"]["budget"] = doc["runs"][1]["checks"]["pass"] = False
            path.write_text(json.dumps(doc), encoding="utf-8")
    assert suite.summarize(paths)["p90"] == 9
    assert suite.summarize(paths)["acceptance"] is False


def test_failure_and_comparison_are_recorded_without_overwriting(tmp_path, fake_nexent):
    root = tmp_path / "results"
    ledger = str(tmp_path / "budget.json")
    comparison = tmp_path / "comparison.json"
    comparison.write_text(json.dumps({"runs": []}), encoding="utf-8")
    assert suite.main(["--agent", "fake_failure", "--output-dir", str(root), "--phase", "standalone",
                       "--cases", "weird_combo", "--runs", "10", "--compare-file", str(comparison),
                       "--budget-ledger", ledger, "--budget-cny", "10", "--max-calls", "25"]) == 1
    assert len(fake_nexent) == 1
    result = json.loads(next(root.glob("*/results.json")).read_text(encoding="utf-8"))["runs"][0]
    assert result["status"] == 503 and result["error"] == "unavailable"
    assert result["checks"]["pass"] is False and "HTTP 503" in Path(result["raw_path"]).read_text(encoding="utf-8")
    assert json.loads(comparison.read_text(encoding="utf-8")) == {"runs": []}
    assert suite.main(["--agent", "fake_incomplete", "--output-dir", str(root), "--phase", "standalone",
                       "--cases", "weird_combo", "--budget-ledger", ledger, "--budget-cny", "10",
                       "--max-calls", "25"]) == 1
    incomplete = next(json.loads(p.read_text(encoding="utf-8"))["runs"][0]
                      for p in root.glob("*/results.json")
                      if json.loads(p.read_text(encoding="utf-8"))["agent"] == "fake_incomplete")
    assert incomplete["status"] == 200 and "without successful completion" in incomplete["error"]
    with pytest.raises(SystemExit):
        suite.main(["--output-dir", str(root)])


def test_preflight_only_never_sends_requests(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("NEXENT_ADMIN_JWT", raising=False)
    monkeypatch.setenv("NEXENT_NB_KEY", "fake-local-only-key")
    code = suite.main(["--preflight-only", "--agent", "i244_plantkg_main", "--phase", "standalone",
                       "--cases", "weird_combo", "--runs", "10", "--output-dir", str(tmp_path / "out")])
    report = json.loads(capsys.readouterr().out)
    assert code == 1  # 没有管理员 JWT 就无法确认租户/Agent，预检不当作"就绪"
    assert report["requests_sent"] == 0 and report["agent"] == "i244_plantkg_main"
    assert "NEXENT_ADMIN_JWT" in report["note"]
    assert not (tmp_path / "out").exists()  # 预检不建运行目录，避免留下空批次
