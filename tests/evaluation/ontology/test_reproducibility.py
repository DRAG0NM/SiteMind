"""B31/B48 评测可复现性回归（#179）：换行归一、包发现、SQLite 关闭与 CLI 目录策略。"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

from setuptools import find_packages

from plantkg.core.hashing import hash_file, hash_text
from plantkg.evaluation.ontology.batch_report import canonical_input_sha256, main as batch_report_main, read_batches
from plantkg.evaluation.ontology.evaluation import evaluate_ontology
from plantkg.knowledge.evidence.evaluation import _corpus

ROOT = Path(__file__).resolve().parents[3]
FIXTURE = ROOT / "fixtures/ontology/synthetic.json"
LF_BLOB_HASH = "0f2116bc7f033de1a8a7b6278a5754e652964cd9a32e7f455f363ea36c84285a"


def _run(module, *args, cwd=ROOT):
    env = dict(os.environ, PYTHONPATH=str(ROOT))
    return subprocess.run([sys.executable, "-m", module, *args], cwd=cwd, env=env,
                          capture_output=True, text=True, timeout=300)


def test_source_hash_is_line_ending_independent(tmp_path):
    text = FIXTURE.read_text(encoding="utf-8")
    lf = tmp_path / "lf.json"
    lf.write_text(text, encoding="utf-8", newline="\n")
    crlf = tmp_path / "crlf.json"
    crlf.write_bytes(text.replace("\n", "\r\n").encode("utf-8"))
    assert hash_file(lf) == hash_file(crlf) == LF_BLOB_HASH
    assert evaluate_ontology(lf, 42)["source_sha256"] == evaluate_ontology(crlf, 42)["source_sha256"] == LF_BLOB_HASH
    committed = json.loads((ROOT / "plantkg/evaluation/ontology/demo_output/results.json").read_text(encoding="utf-8"))
    assert committed["source_sha256"] == LF_BLOB_HASH


def test_hash_text_ignores_bom_and_line_ending_spellings(tmp_path):
    # #205：同一份文本带 BOM、CRLF 或孤立 CR 时哈希必须一致
    text = FIXTURE.read_text(encoding="utf-8")
    assert hash_text(text) == LF_BLOB_HASH
    assert hash_text("\ufeff" + text) == LF_BLOB_HASH
    assert hash_text(text.replace("\n", "\r\n")) == LF_BLOB_HASH
    assert hash_text(text.replace("\n", "\r")) == LF_BLOB_HASH
    bom = tmp_path / "bom.json"
    bom.write_bytes(b"\xef\xbb\xbf" + text.encode("utf-8"))
    assert hash_file(bom) == LF_BLOB_HASH


def test_b31_required_versions_use_lf_hash(tmp_path):
    _library, questions = _corpus(tmp_path)
    first = questions[0]
    for document_id, digest in first["required_versions"].items():
        assert hash_file(tmp_path / f"{document_id}.md") == digest
    crlf = tmp_path / "crlf-copy.md"
    crlf.write_bytes((tmp_path / f"{first['required_documents'][0]}.md").read_bytes().replace(b"\n", b"\r\n"))
    assert hash_file(crlf) == first["required_versions"][first["required_documents"][0]]
    committed = json.loads((ROOT / "plantkg/knowledge/evidence/results/results.json").read_text(encoding="utf-8"))
    for question in committed["questions"]:
        for document_id in question["required_documents"]:
            citation = next(item for item in committed["results"]
                            if document_id in item["documents"] and any(c["id"] == document_id for c in item["citations"]))
            version = next(c["version"] for c in citation["citations"] if c["id"] == document_id)
            assert version == question["required_versions"][document_id]


def test_evaluation_packages_are_discoverable_and_cli_runs():
    packages = find_packages(where=str(ROOT), include=["plantkg*"])
    assert "plantkg.evaluation" in packages and "plantkg.evaluation.ontology" in packages
    finished = _run("plantkg.evaluation.ontology", "--help")
    assert finished.returncode == 0, finished.stderr
    assert "maintie-predictions" in finished.stdout


def _batch_db(path):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE b30_batches (plant_id TEXT NOT NULL, batch_id TEXT NOT NULL, body TEXT NOT NULL, "
                "PRIMARY KEY(plant_id,batch_id))")
    con.execute("INSERT INTO b30_batches VALUES (?,?,?)", ("factory", "batch-a", json.dumps(
        {"items": 2, "agent_fraction": 0.5, "review_minutes_per_100": 30.0, "tokens": 10, "elapsed_seconds": 60.0})))
    con.commit()
    con.close()


def test_read_batches_closes_sqlite_and_hash_ignores_file_layout(tmp_path):
    database = tmp_path / "batches.db"
    _batch_db(database)
    rows = read_batches(database, "factory")
    assert rows[0]["batch_id"] == "batch-a"
    # Windows 上连接没关会锁住文件，立刻改名/删除应成功（#153）
    moved = tmp_path / "moved.db"
    os.replace(database, moved)
    os.remove(moved)

    _batch_db(database)
    copy = tmp_path / "vacuumed.db"
    shutil.copyfile(database, copy)
    con = sqlite3.connect(copy)
    con.execute("VACUUM")
    con.commit()
    con.close()
    assert database.read_bytes() != copy.read_bytes()
    first, second = read_batches(database, "factory"), read_batches(copy, "factory")
    assert canonical_input_sha256(first) == canonical_input_sha256(second)

    out = tmp_path / "report"
    argv = sys.argv
    try:
        sys.argv = ["batch_report", str(database), "--plant-id", "factory", "--output", str(out)]
        batch_report_main()
    finally:
        sys.argv = argv
    report = json.loads((out / "batches.json").read_text(encoding="utf-8"))
    assert report["schema_version"] == 1 and report["tool_version"]
    assert report["input_sha256"] == canonical_input_sha256(first)


def test_maintie_cli_creates_parent_directory(tmp_path):
    output = tmp_path / "nested" / "results" / "maintie.json"
    finished = _run("plantkg.evaluation.ontology.maintie_b30", "fixtures/ontology/maintie/sample.json", "--output", str(output))
    assert finished.returncode == 0, finished.stderr
    result = json.loads(output.read_text(encoding="utf-8"))
    assert result["source_sha256"] == hash_file(ROOT / "fixtures/ontology/maintie/sample.json")
