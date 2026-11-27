"""下载 MetroPT-3 原始包到仓库外，记 SHA-256，切出两个 10 秒采样小样本（B22）。

    python scripts/data/fetch_metropt3.py --data-dir D:\\data\\metropt3
    python scripts/data/fetch_metropt3.py --data-dir D:\\data\\metropt3 --out fixtures/equipment/metropt3

数据：UCI 791 MetroPT-3（地铁空压机 APU，2020-02～08，10 秒采样，CC BY 4.0）。
官方 4 次空气泄漏中，本卡只用第 2 次 2020-05-29 23:30～05-30 06:00，对照取前两晚同一时段
2020-05-27 23:30～05-28 06:00（夜间对夜间）。07-15 泄漏前几小时运转占比已近 100%，不作对照。

原始包约 208 MB，不支持断点续传；下载到 --data-dir（仓库外），zip 与 SHA-256 写进
metropt3_download.json。仓库只放两个小样本与 manifest.json。本轮 fixtures 里是 synthetic 仿写，
跑本脚本会把它们替换成 measured 真实切片，列名与研判逻辑不变。
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
from datetime import datetime
from pathlib import Path

URL = "https://archive.ics.uci.edu/static/public/791/metropt+3+dataset.zip"
MEMBER_SUFFIX = "MetroPT3(AirCompressor).csv"
CITATION = ("Davari, N., Veloso, B., Ribeiro, R., & Gama, J. (2021). MetroPT-3 Dataset [Dataset]. "
            "UCI Machine Learning Repository. https://doi.org/10.24432/C5VW3R. CC BY 4.0.")
WINDOWS = {"baseline": (datetime(2020, 5, 27, 23, 30), datetime(2020, 5, 28, 6, 0)),
           "leak": (datetime(2020, 5, 29, 23, 30), datetime(2020, 5, 30, 6, 0))}
FILES = {"baseline": "baseline_2020-05-27_2330_to_2020-05-28_0600.csv",
         "leak": "leak_2020-05-29_2330_to_2020-05-30_0600.csv"}
ROWS = ("timestamp", "motor_current", "oil_temperature", "reservoir_pressure_bar", "compressor_on")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def download(data_dir: Path, url: str) -> Path:
    target = data_dir / "metropt3_uci_791.zip"
    if target.exists():
        print(f"已存在 {target}，跳过下载")
    else:
        print(f"下载 {url} → {target}（约 208 MB，不支持断点续传）")
        part = target.with_suffix(".part")
        with urllib.request.urlopen(url, timeout=120) as response, part.open("wb") as f:
            while chunk := response.read(1 << 20):
                f.write(chunk)
        part.replace(target)
    digest = _sha256(target)
    (data_dir / "metropt3_download.json").write_text(json.dumps(
        {"dataset": "MetroPT-3", "uci_id": 791, "url": url, "file": target.name, "bytes": target.stat().st_size,
         "sha256": digest, "license": "CC BY 4.0", "citation": CITATION}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"SHA-256 {digest}")
    return target


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.strip().replace("Z", "+00:00")).replace(tzinfo=None)


def _stats(rows: list[dict]) -> dict:
    n = len(rows)
    ratio = sum(r["compressor_on"] for r in rows) / n
    current = sum(r["motor_current"] for r in rows) / n
    oil = sum(r["oil_temperature"] for r in rows) / n
    t0 = datetime.fromisoformat(rows[0]["timestamp"])
    xs = [(datetime.fromisoformat(r["timestamp"]) - t0).total_seconds() / 3600 for r in rows]
    ys = [r["reservoir_pressure_bar"] for r in rows]
    mx, my = sum(xs) / n, sum(ys) / n
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sum((x - mx) ** 2 for x in xs)
    return {"samples": n, "minutes": round(n * 10 / 60, 1), "running_ratio": round(ratio, 4), "current_mean_a": round(current, 3),
            "oil_mean_c": round(oil, 2), "oil_start_c": round(rows[0]["oil_temperature"], 2),
            "oil_end_c": round(rows[-1]["oil_temperature"], 2), "pressure_start_bar": round(rows[0]["reservoir_pressure_bar"], 3),
            "pressure_end_bar": round(rows[-1]["reservoir_pressure_bar"], 3), "pressure_drop_bar_per_h": round(slope, 4)}


def slice_windows(archive: Path, out_dir: Path) -> dict:
    zf = zipfile.ZipFile(archive)
    member = next((name for name in zf.namelist() if name.endswith(MEMBER_SUFFIX)), None)
    if member is None:
        raise SystemExit(f"压缩包里没有 {MEMBER_SUFFIX}；实际成员：{zf.namelist()[:5]}")
    wanted: dict[str, list[dict]] = {key: [] for key in WINDOWS}
    with zf.open(member) as raw, io.TextIOWrapper(raw, encoding="utf-8", newline="") as text:
        reader = csv.DictReader(text)
        fields = {name.lower(): name for name in (reader.fieldnames or ())}
        need = {key: fields.get(key.lower()) for key in ("timestamp", "Motor_current", "Oil_temperature", "Reservoirs")}
        if any(value is None for value in need.values()):
            raise SystemExit(f"CSV 列名不符合预期：{reader.fieldnames}")
        for row in reader:
            at = _parse(row[need["timestamp"]])
            kind = next((key for key, (start, end) in WINDOWS.items() if start <= at < end), None)
            if kind is None:
                continue
            current = float(row[need["Motor_current"]])
            wanted[kind].append({"timestamp": at.isoformat(sep=" "), "motor_current": current,
                                 "oil_temperature": float(row[need["Oil_temperature"]]),
                                 "reservoir_pressure_bar": float(row[need["Reservoirs"]]),
                                 "compressor_on": 1 if current > 2.0 else 0})
    windows = {}
    for kind, rows in wanted.items():
        if len(rows) < 2:
            raise SystemExit(f"窗口 {kind} 没切到数据（检查时间戳时区）")
        path = out_dir / FILES[kind]
        header = (f"# nature=measured; source=MetroPT-3 (UCI 791, CC BY 4.0); window={WINDOWS[kind][0]}～{WINDOWS[kind][1]}; "
                  f"note=真实切片来自 MetroPT-3，地铁空压机，非本厂设备\n")
        with path.open("w", encoding="utf-8", newline="") as f:
            f.write(header)
            writer = csv.DictWriter(f, fieldnames=list(ROWS))
            writer.writeheader()
            for row in rows:
                writer.writerow({**row, "motor_current": f"{row['motor_current']:.3f}",
                                 "oil_temperature": f"{row['oil_temperature']:.3f}",
                                 "reservoir_pressure_bar": f"{row['reservoir_pressure_bar']:.3f}"})
        windows[kind] = {"file": path.name, "sha256": _sha256(path),
                         "start": rows[0]["timestamp"], "end": rows[-1]["timestamp"], "stats": _stats(rows)}
        print(f"{path}：{len(rows)} 行")
    return windows


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data-dir", required=True, type=Path, help="仓库外的数据目录")
    parser.add_argument("--out", type=Path, default=Path("fixtures/equipment/metropt3"), help="仓库内小样本目录")
    parser.add_argument("--url", default=URL, help="下载地址（默认 UCI，可换镜像）")
    parser.add_argument("--force", action="store_true", help="已有 measured 切片时仍重新生成")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args.data_dir.mkdir(parents=True, exist_ok=True)
    args.out.mkdir(parents=True, exist_ok=True)
    manifest_path = args.out / "manifest.json"
    old = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else {}
    if old.get("nature") == "measured" and not args.force:
        print(f"{manifest_path} 已是 measured 真实切片；要重新生成加 --force")
        return 0
    archive = download(args.data_dir, args.url)
    windows = slice_windows(archive, args.out)
    manifest = {
        "dataset": "MetroPT-3", "uci_id": 791, "license": "CC BY 4.0", "citation": CITATION, "sampling_seconds": 10,
        "nature": "measured", "note": "真实切片来自 MetroPT-3，地铁空压机，非本厂设备",
        "source_note": "本厂只借用泄漏时段的信号形状与统计，按额定参数缩放后注入",
        "official_leak_window": "2020-05-29T23:30:00～2020-05-30T06:00:00",
        "baseline_window": "2020-05-27T23:30:00～2020-05-28T06:00:00（前两晚同一时段，夜间对夜间）",
        "excluded_window_note": "2020-07-15 泄漏前几小时运转占比已近 100%，不作对照",
        "target_statistics": old.get("target_statistics", {"running_ratio": [0.23, 1.0], "current_mean_a": [1.0, 5.6], "oil_c": [65, 76]}),
        "columns": {"timestamp": "文件原时间戳", "motor_current": "A", "oil_temperature": "℃", "reservoir_pressure_bar": "bar",
                    "compressor_on": "motor_current > 2 A 判为运转"},
        "source_zip_sha256": json.loads((args.data_dir / "metropt3_download.json").read_text(encoding="utf-8"))["sha256"],
        "windows": windows,
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"写出 {manifest_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
