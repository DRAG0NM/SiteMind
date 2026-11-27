"""B31 offline entry points."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .evaluation import evaluate


def main():
    parser = argparse.ArgumentParser(prog="python -m plantkg.knowledge.evidence")
    commands = parser.add_subparsers(dest="command", required=True)
    evaluation = commands.add_parser("eval", help="Run the offline synthetic comparison")
    evaluation.add_argument("--out", type=Path, required=True)
    commands.add_parser("demo", help="Run the synthetic scenario and inspect action evidence")
    args = parser.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if args.command == "eval":
        print(evaluate(args.out))
    else:
        from plantkg.world.scenario.__main__ import build_world, open_db
        from plantkg.world.scenario.runner import ScenarioRunner
        from plantkg.world.scenario.script import load_scenario
        from .demo import run_demo
        run_demo(build_world, open_db, ScenarioRunner, load_scenario)


if __name__ == "__main__":
    main()
