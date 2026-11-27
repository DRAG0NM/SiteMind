"""下载 D05 原始数据到仓库外，记 SHA-256，转成窄表，并生成仓库内一周小样本（B18）。

    python scripts/data/fetch_d05.py --data-dir D:\\data\\d05
    python scripts/data/fetch_d05.py --data-dir D:\\data\\d05 --sample-out fixtures/sites/factory/d05_week.parquet

数据：Flynn et al. 2026，Industrial Asset-Level Electrical Energy Dataset from a Manufacturing Facility
（15-Minute Aggregates）v1.0，Zenodo 19180972，CC BY 4.0。原包约 218 MB，不进仓库。
--url 可换成 Hugging Face 等镜像地址；无论来源，都按 Zenodo 公布的 MD5 校验。

产物（都在 --data-dir）：d05_zenodo.zip、d05_all.parquet（ALL 相窄表：asset_id、asset_type、time、
energy_kwh、is_reliable_window，schema 元数据带资产类型与 24 条计量故障）、d05_manifest.json（文件散列）。
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import sys
import urllib.request
import zipfile
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

RECORD = "19180972"
ZIP_NAME = "Industrial Asset-Level Electrical Energy Dataset from a Manufacturing Facility (15-Minute Aggregates).zip"
URL = f"https://zenodo.org/api/records/{RECORD}/files/{urllib.request.quote(ZIP_NAME)}/content"
ZENODO_MD5 = "acc03fa5cb4b9c54a426440fffe49e74"
CITATION = ("Flynn et al. (2026). Industrial Asset-Level Electrical Energy Dataset from a Manufacturing Facility "
            "(15-Minute Aggregates), v1.0. Zenodo. https://doi.org/10.5281/zenodo.19180972 ; "
            "论文 Data 2026, 11, 101, https://doi.org/10.3390/data11050101 。CC BY 4.0。")
DUBLIN = ZoneInfo("Europe/Dublin")


def _hashes(path: Path) -> tuple[str, str]:
    md5, sha = hashlib.md5(), hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            md5.update(chunk)
            sha.update(chunk)
    return md5.hexdigest(), sha.hexdigest()


def download(data_dir: Path, url: str) -> Path:
    target = data_dir / "d05_zenodo.zip"
    if not target.exists():
        print(f"下载 {url} → {target}")
        part = target.with_suffix(".part")
        with urllib.request.urlopen(url) as r, open(part, "wb") as f:
            while chunk := r.read(1 << 20):
                f.write(chunk)
        part.rename(target)
    md5, sha = _hashes(target)
    if md5 != ZENODO_MD5:
        raise SystemExit(f"MD5 不符：{md5}，Zenodo 公布 {ZENODO_MD5}")
    (data_dir / "d05_manifest.json").write_text(json.dumps(
        {"record": RECORD, "url": url, "file": target.name, "bytes": target.stat().st_size, "md5": md5, "sha256": sha,
         "citation": CITATION}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"MD5 与 Zenodo 一致；SHA-256 {sha}")
    return target


def _parse_log_time(text: str) -> str:
    return datetime.strptime(text, "%d/%m/%Y %H:%M").replace(tzinfo=timezone.utc).isoformat()


def convert(archive: Path, out: Path) -> None:
    import pyarrow.parquet as pq

    z = zipfile.ZipFile(archive)
    names = z.namelist()
    member = lambda suffix: next(n for n in names if n.endswith(suffix))
    types = {r["asset_id"]: r["asset_type"] for r in csv.DictReader(io.StringIO(z.read(member("metadata/AssetList.csv")).decode("utf-8-sig")))}
    events = [{"event_id": i + 1, "asset_id": r["asset_id"], "issue_type": r["issue_type"],
               "start": _parse_log_time(r["issue_start"]), "end": _parse_log_time(r["issue_end"])}
              for i, r in enumerate(csv.DictReader(io.StringIO(z.read(member("metadata/meter_data_quality_log.csv")).decode("utf-8-sig"))))]
    cols = {"asset_id": [], "asset_type": [], "time": [], "energy_kwh": [], "is_reliable_window": []}
    parts = sorted(n for n in names if "/data_parquet/" in n and n.endswith(".parquet"))
    for i, n in enumerate(parts):
        t = pq.read_table(io.BytesIO(z.read(n)), columns=["AssetId", "Phase", "window_start_utc", "Energy_kWh_15m", "IsReliableWindow"])
        for a, ph, ts, e, ok in zip(*(t.column(c).to_pylist() for c in t.column_names)):
            if ph == "ALL":
                cols["asset_id"].append(a)
                cols["asset_type"].append(types[a])
                cols["time"].append(ts)
                cols["energy_kwh"].append(e)
                cols["is_reliable_window"].append(ok)
        if i % 2000 == 0:
            print(f"  {i}/{len(parts)} 个分区")
    write(cols, types, events, out, {"source": "d05_zenodo.zip", "phase": "ALL"})


def write(cols: dict, types: dict, events: list, out: Path, extra: dict) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq

    table = pa.table({"asset_id": pa.array(cols["asset_id"], pa.string()), "asset_type": pa.array(cols["asset_type"], pa.string()),
                      "time": pa.array(cols["time"], pa.timestamp("us")), "energy_kwh": pa.array(cols["energy_kwh"], pa.float64()),
                      "is_reliable_window": pa.array(cols["is_reliable_window"], pa.int8())})
    table = table.sort_by([("asset_id", "ascending"), ("time", "ascending")])
    meta = {"asset_types": types, "quality_events": events, "citation": CITATION, "license": "CC BY 4.0",
            "columns": "time=窗口起点 UTC；energy_kwh=Energy_kWh_15m（ALL 相）；is_reliable_window=IsReliableWindow", **extra}
    table = table.replace_schema_metadata({b"d05": json.dumps(meta, ensure_ascii=False).encode()})
    out.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, out, compression="zstd")
    print(f"写出 {out}（{table.num_rows} 行，{out.stat().st_size} 字节）")


def sample(full: Path, out: Path, week_start: date, days: int) -> None:
    """一周小样本：Europe/Dublin 当地完整日，总进线 mi_a 每天 96 个时段才接受。"""
    import pyarrow.parquet as pq

    start = datetime.combine(week_start, time.min, DUBLIN).astimezone(timezone.utc).replace(tzinfo=None)
    end = datetime.combine(week_start + timedelta(days=days), time.min, DUBLIN).astimezone(timezone.utc).replace(tzinfo=None)
    t = pq.read_table(full, filters=[("time", ">=", start), ("time", "<", end)])
    meta = json.loads(t.schema.metadata[b"d05"])
    rows = t.to_pylist()
    per_day: dict[date, int] = {}
    for r in rows:
        if r["asset_id"] == "mi_a":
            d = r["time"].replace(tzinfo=timezone.utc).astimezone(DUBLIN).date()
            per_day[d] = per_day.get(d, 0) + 1
    incomplete = {d.isoformat(): per_day.get(d, 0) for d in (week_start + timedelta(days=i) for i in range(days)) if per_day.get(d, 0) != 96}
    if incomplete:
        raise SystemExit(f"总进线不完整的日期：{incomplete}")
    cols = {k: [r[k] for r in rows] for k in ("asset_id", "asset_type", "time", "energy_kwh", "is_reliable_window")}
    write(cols, meta["asset_types"], meta["quality_events"], out,
          {"source": "d05_all.parquet", "phase": "ALL", "week_start_dublin": week_start.isoformat(), "days": days,
           "full_sha256": _hashes(full)[1]})


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--data-dir", required=True, type=Path, help="仓库外的数据目录")
    p.add_argument("--url", default=URL, help="下载地址（默认 Zenodo，可换镜像）")
    p.add_argument("--sample-out", type=Path, help="一周小样本输出路径")
    p.add_argument("--week-start", default="2025-06-04", help="小样本起始日（Europe/Dublin 当地，须在 2025-05-23 之后）")
    p.add_argument("--days", type=int, default=7)
    a = p.parse_args(argv)
    a.data_dir.mkdir(parents=True, exist_ok=True)
    archive = download(a.data_dir, a.url)
    full = a.data_dir / "d05_all.parquet"
    if not full.exists():
        convert(archive, full)
    if a.sample_out:
        week = date.fromisoformat(a.week_start)
        if week < date(2025, 5, 23):
            raise SystemExit("小样本须选 2025-05-23 之后的完整日（总进线 2025-05-22 13:45 才开始）")
        sample(full, a.sample_out, week, a.days)
    return 0


if __name__ == "__main__":
    sys.exit(main())
