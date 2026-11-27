"""把 B26 清单里的资料按专家上传到 Nexent 知识库：python scripts/nexent_kb_upload.py --credentials <json>

一位专家一个索引（显示名取 `nexent_assets/knowledge/catalog.yaml` 的 `experts.<专家>.display_name`），
上传、切块、等待入库完成，然后把真实索引名与文件路径写进 `nexent_assets/knowledge/indexes.json`。
重复运行是幂等的：索引按显示名复用，已完成的同名文件跳过。不删除、不改租户里其他索引。

凭据：`--credentials` 指向含 email、password 的 json（仓库外），或环境变量 NEXENT_ADMIN_EMAIL / NEXENT_ADMIN_PASSWORD。
本脚本只连本机 Nexent，HTTP 客户端不读代理环境变量（本机 no_proxy 里带 `[::1]` 会让 httpx 解析失败）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from plantkg.knowledge.documents import load_library  # noqa: E402
DONE = ("COMPLETED", "completed", "SUCCESS", "FAILED", "failed")


class NexentAdmin:
    def __init__(self, base_url: str, token: str) -> None:
        self.base = base_url.rstrip("/")
        self.client = httpx.Client(timeout=180, trust_env=False, headers={"Authorization": f"Bearer {token}"})

    @staticmethod
    def signin(base_url: str, email: str, password: str) -> "NexentAdmin":
        with httpx.Client(timeout=30, trust_env=False) as client:
            data = client.post(base_url.rstrip("/") + "/user/signin", json={"email": email, "password": password}).json()
        token = ((data.get("data") or {}).get("session") or {}).get("access_token")
        if not token:
            raise SystemExit("Nexent 管理端登录失败")
        return NexentAdmin(base_url, token)

    def call(self, method: str, path: str, body: Any = None) -> Any:
        for attempt in range(3):
            try:
                response = self.client.request(method, self.base + path, json=body)
                break
            except httpx.TransportError:
                # 切块或建索引时 config 服务偶发断开连接：等几秒重试，不当作上传失败
                if attempt == 2:
                    raise
                time.sleep(5)
        try:
            data = response.json()
        except ValueError:
            data = response.text
        if response.status_code >= 400:
            raise SystemExit(f"Nexent {method} {path} 返回 {response.status_code}：{json.dumps(data, ensure_ascii=False)[:300]}")
        return data

    def indices(self) -> list[dict[str, Any]]:
        return (self.call("GET", "/indices?include_stats=true") or {}).get("indices_info") or []

    def embedding_model(self) -> list[dict[str, Any]]:
        return [m for m in (self.call("GET", "/model/list").get("data") or []) if m.get("model_type") == "embedding"]

    def ensure_index(self, display_name: str, embedding_model_id: int) -> str:
        for index in self.indices():
            if index.get("display_name") == display_name:
                return str(index["name"])
        self.call("POST", f"/indices/{display_name}", {"embedding_model_id": embedding_model_id})
        for _ in range(20):
            for index in self.indices():
                if index.get("display_name") == display_name:
                    return str(index["name"])
            time.sleep(1)
        raise SystemExit(f"索引 {display_name} 创建后在列表里查不到")

    def files(self, index_name: str) -> list[dict[str, Any]]:
        return (self.call("GET", f"/indices/{index_name}/files") or {}).get("files") or []

    def upload(self, index_name: str, files: list[tuple[str, bytes, str]]) -> tuple[list[str], list[str]]:
        response = self.client.post(f"{self.base}/file/upload",
                                    data={"destination": "minio", "folder": "knowledge_base", "index_name": index_name},
                                    files=[("file", (name, content, mime)) for name, content, mime in files])
        body = response.json()
        if response.status_code >= 400:
            raise SystemExit(f"上传 {index_name} 失败：HTTP {response.status_code} {json.dumps(body, ensure_ascii=False)[:300]}")
        return body.get("uploaded_file_paths") or [], body.get("uploaded_filenames") or [name for name, _, _ in files]

    def process(self, index_name: str, uploaded: list[dict[str, str]]) -> None:
        self.call("POST", "/file/process", {"files": uploaded, "index_name": index_name, "destination": "minio",
                                            "chunking_strategy": "basic"})

    def wait(self, index_name: str, timeout_s: float = 300) -> list[dict[str, Any]]:
        deadline = time.monotonic() + timeout_s
        while True:
            files = self.files(index_name)
            if files and all(str(f.get("status")) in DONE for f in files):
                return files
            if time.monotonic() > deadline:
                return files
            time.sleep(5)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python scripts/nexent_kb_upload.py")
    parser.add_argument("--admin-url", default="http://127.0.0.1:5010")
    parser.add_argument("--credentials", default=None, help="含 email、password 的 json（仓库外）")
    parser.add_argument("--out", default=str(ROOT / "nexent_assets" / "knowledge" / "indexes.json"))
    parser.add_argument("--timeout-s", type=float, default=300)
    args = parser.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")

    email, password = _credentials(args.credentials)
    library = load_library(ROOT)
    admin = NexentAdmin.signin(args.admin_url, email, password)
    embeddings = admin.embedding_model()
    if not embeddings:
        raise SystemExit("租户里没有 embedding 模型，无法建知识库索引")
    embedding = embeddings[0]
    print(f" embedding 模型 {embedding.get('model_name')}（id={embedding.get('model_id')}）")

    report: dict[str, Any] = {"created_at": datetime.now(timezone.utc).isoformat(), "admin_url": args.admin_url,
                              "embedding_model": embedding.get("model_name"), "experts": {}, "documents": {}, "problems": []}
    for expert, spec in library.experts.items():
        documents = library.documents(expert)
        index_name = admin.ensure_index(spec.display_name, int(embedding["model_id"]))
        report["experts"][expert] = {"display_name": spec.display_name, "agent": spec.agent, "index_name": index_name}
        print(f"\n[{expert}] 索引 {index_name}（{spec.display_name}），清单 {len(documents)} 份")
        done = {Path(str(f.get("file"))).name for f in admin.files(index_name)}
        pending = [doc for doc in documents if library.upload_file(doc).name not in done]
        if pending:
            payload = [(library.upload_file(doc).name, library.upload_file(doc).read_bytes(), _mime(library.upload_file(doc)))
                       for doc in pending]
            paths, names = admin.upload(index_name, payload)
            by_name = {name: path for name, path in zip(names, paths)}
            admin.process(index_name, [{"path_or_url": by_name.get(library.upload_file(doc).name, ""),
                                        "filename": library.upload_file(doc).name} for doc in pending])
            files = admin.wait(index_name, args.timeout_s)
        else:
            files = admin.files(index_name)
        by_file = {str(f.get("file")): f for f in files}
        for document in documents:
            name = library.upload_file(document).name
            entry = next((f for key, f in by_file.items() if Path(key).name == name), None)
            if entry is None:
                report["problems"].append(f"{expert}/{document.document_id}：上传后查不到 {name}")
                continue
            status = str(entry.get("status"))
            if status not in ("COMPLETED", "completed", "SUCCESS"):
                report["problems"].append(f"{expert}/{document.document_id}：处理状态 {status}")
            report["documents"][document.document_id] = {"filename": name,
                                                         "nexent_path": entry.get("path_or_url") or entry.get("file"),
                                                         "file": entry.get("file"), "status": status}
            print(f"  {document.document_id:32} {status:10} {entry.get('path_or_url') or entry.get('file')}")
        report["experts"][expert]["files"] = len(files)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n索引与路径写入 {out}")
    if report["problems"]:
        print("\n问题：")
        for problem in report["problems"]:
            print("  -", problem)
        return 1
    return 0


def _credentials(path: str | None) -> tuple[str, str]:
    if path:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return data["email"], data["password"]
    try:
        return os.environ["NEXENT_ADMIN_EMAIL"], os.environ["NEXENT_ADMIN_PASSWORD"]
    except KeyError:
        raise SystemExit("缺少管理端凭据：设置 NEXENT_ADMIN_EMAIL / NEXENT_ADMIN_PASSWORD，或用 --credentials") from None


def _mime(path: Path) -> str:
    return {".md": "text/markdown", ".csv": "text/csv", ".pdf": "application/pdf",
            ".png": "image/png", ".jpg": "image/jpeg"}.get(path.suffix.lower(), "application/octet-stream")


if __name__ == "__main__":
    raise SystemExit(main())
