"""B43 命令行：demo（一键演示）与 history（打印某 Skill 的版本记录）。

    python -m plantkg.evolution.skills demo [--db PATH]
    python -m plantkg.evolution.skills history <name> [--db PATH] [--plant ID]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m plantkg.evolution.skills",
                                     description="B43 候选 Skill 的挖掘、评估、发布与版本记录")
    sub = parser.add_subparsers(dest="command", required=True)
    demo = sub.add_parser("demo", help="用合成处置记录跑完 挖掘→候选→评估→发布 v1→v2→回滚，并打印版本记录")
    demo.add_argument("--db", default=None, help="把演示结果写进指定 SQLite 库；缺省用临时库（跑完删除）")
    history = sub.add_parser("history", help="打印某个 Skill 的版本记录")
    history.add_argument("name", help="Skill 名称，如 compressor-pressure-drop-procedure")
    history.add_argument("--db", default="runtime/demo/catalog.db", help="SQLite 库路径（默认 demo profile 的库）")
    history.add_argument("--plant", default=None, help="厂区编号（缺省取 configs 的 plant_id）")
    args = parser.parse_args(argv)

    if args.command == "demo":
        result = _run_demo(args.db)
        if not result.get("ok"):
            print("演示未跑通：", result.get("reason"), file=sys.stderr)
            return 1
        if not args.db:
            print("提示：本次用临时库，跑完已删除；要保留版本记录请加 --db runtime/b43-demo.db")
        return 0
    return _print_history(args.name, db=args.db, plant=args.plant)


def _run_demo(db: str | None) -> dict:
    """装配 dev + 临时/指定库跑演示：装配只能在装配层做，本文件是模块命令行入口。"""
    import tempfile
    from pathlib import Path

    from plantkg.bootstrap import build_services
    from plantkg.core.config import load_settings
    from plantkg.evolution.skills.demo import run_demo

    with tempfile.TemporaryDirectory(prefix="plantkg-b43-demo-") as tmp:
        root = Path(db) if db else None
        directory = root.parent if root else Path(tmp)
        sqlite = root or (directory / "catalog.db")
        settings = load_settings(profile="dev", overrides={
            "storage.enabled": True, "storage.sqlite": str(sqlite),
            "storage.graph": str(directory / "graph"), "storage.series": str(directory / "series")})
        services = build_services(settings)
        try:
            return run_demo(services)
        finally:
            services.close()


def _print_history(name: str, *, db: str, plant: str | None) -> int:
    from plantkg.core.config import load_settings
    from plantkg.core.versions import SqliteVersionStore
    from plantkg.evolution.skills.history import skill_history
    from plantkg.evolution.store import EvolutionStore
    from plantkg.storage.sqlite import SQLiteWriter

    path = Path(db)
    if not path.is_file():
        print(f"找不到数据库：{path}。先跑 `python -m plantkg.evolution.skills demo --db {db}`，"
              f"或用 --db 指定已有库", file=sys.stderr)
        return 2
    plant_id = plant or load_settings().plant_id
    writer = SQLiteWriter(path)
    try:
        records = skill_history(SqliteVersionStore(writer, plant_id), EvolutionStore(writer, plant_id), name)
    finally:
        writer.close()
    if not records:
        print(f"没有找到 skill {name} 的版本记录", file=sys.stderr)
        return 1
    from plantkg.evolution.skills.demo import print_history

    print_history(records, print)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
