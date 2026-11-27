"""校验“一天”数据：python -m plantkg.contracts.validate contracts/examples/day.sample.json

先按 contracts/day.schema.json 校验结构（装了 jsonschema 时），再用 DayRecord 校验跨字段规则。

链模式（#238 方案 5）：python -m plantkg.contracts.validate --chain runtime/scenario.db --run RUN_ID
逐日导出该运行链覆盖的所有日期，检查链内并集与引用完整性；实现在 world/scenario/export.py，
这里只做命令行薄封装，单文件模式不依赖世界平面。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from pydantic import ValidationError

from plantkg.core.contracts import DayRecord, safe_error_details

SCHEMA = Path(__file__).resolve().parents[2] / "contracts" / "day.schema.json"


def validate(path: Path) -> list[str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    problems = []
    try:
        from jsonschema import Draft202012Validator, FormatChecker
    except ImportError:
        print("未装 jsonschema，跳过 schema 结构校验", file=sys.stderr)
    else:
        validator = Draft202012Validator(json.loads(SCHEMA.read_text(encoding="utf-8")), format_checker=FormatChecker())
        problems += [f"schema {'/'.join(map(str, e.absolute_path))}: {e.message}" for e in validator.iter_errors(data)]
    try:
        DayRecord.model_validate(data)
    except ValidationError as exc:
        problems += [f"contract {'.'.join(map(str, d['loc']))}: {d['msg']}" for d in safe_error_details(exc)]
    return problems


def validate_chain_file(db_path: Path, run_id: str) -> list[str]:
    from plantkg.world.scenario.__main__ import catalog_metrics_provider, day_exports, open_chain_db
    from plantkg.world.scenario.export import validate_chain

    db = open_chain_db(db_path, run_id)
    try:
        return validate_chain(db, run_id, evidence_for_day=lambda runs, as_of: day_exports(db, runs, as_of),
                              asset_metrics_provider=catalog_metrics_provider(db))
    finally:
        db.close()


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m plantkg.contracts.validate", description="校验 day.json，或按运行链校验场景库的逐日导出")
    parser.add_argument("path", nargs="?", help="day.json 文件")
    parser.add_argument("--chain", metavar="DB", help="场景库 SQLite 路径；与 --run 一起按运行链逐日校验")
    parser.add_argument("--run", help="链末运行 ID（--chain 时必填）")
    args = parser.parse_args(argv)
    if args.chain:
        if not args.run:
            parser.print_usage(sys.stderr)
            print("--chain 需要同时给出 --run", file=sys.stderr)
            return 2
        problems = validate_chain_file(Path(args.chain), args.run)
        label = f"{args.chain}（运行链 {args.run}）"
    elif args.path:
        problems = validate(Path(args.path))
        label = args.path
    else:
        parser.print_usage(sys.stderr)
        return 2
    for line in problems:
        print(line)
    print(f"{label}：{'通过' if not problems else f'{len(problems)} 处问题'}")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
