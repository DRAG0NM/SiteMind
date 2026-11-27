"""启动入口：python -m plantkg --profile mock

--dry-run 只装配并打印健康状态与已登记工具，不监听端口（用于自检与 CI）。
python -m plantkg [--profile ...] [--set ...] actions list|confirm|reject：调用本机运行中的服务确认待确认动作（B04）。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys

from typing import get_args

from plantkg.core.config import ConfigError, Profile, check_runtime_secrets, configure_logging, load_settings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m plantkg")
    parser.add_argument("--profile", choices=list(get_args(Profile)), default=None)
    parser.add_argument("--scenario", default=None, help="configs/scenarios/<名称>.yaml")
    parser.add_argument("--set", action="append", default=[], metavar="KEY=VALUE", help="非敏感配置覆盖，如 server.port=8710")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("command", nargs="?", choices=["actions"], help="actions：待确认动作命令行")
    parser.add_argument("rest", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    try:
        overrides = dict(item.split("=", 1) for item in args.set)
        settings = load_settings(profile=args.profile, scenario=args.scenario, overrides=overrides)
        check_runtime_secrets(settings)
    except (ConfigError, ValueError) as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2

    if args.command == "actions":
        from plantkg.core.actions import cli
        return cli(args.rest, settings)

    configure_logging(settings)
    from plantkg.api import _health, create_app
    from plantkg.bootstrap import build_services
    from plantkg.core.contracts import PlantkgError
    from plantkg.core.registry import RegistryError
    from plantkg.mcp_server import create_mcp_server

    try:
        services = build_services(settings)
    except (ConfigError, RegistryError, PlantkgError, ValueError, OSError, RuntimeError, ImportError, sqlite3.Error) as exc:
        print(f"装配失败：{exc}", file=sys.stderr)
        return 2

    if args.dry_run:
        try:
            report = {
                "health": _health(services).model_dump(mode="json"),
                "tools": sorted(b.spec.nexent_name for b in services.registry.tools.values()),
                "fakes": services.registry.fake_bindings(),
                "placeholders": services.registry.placeholder_bindings(),
                "packs": sorted(services.registry.packs),
                "config_sources": list(settings.sources),
                "mcp_url": f"http://{settings.server.host}:{settings.server.port}{settings.server.mcp_path}",
            }
            print(json.dumps(report, ensure_ascii=False, indent=2))
        finally:
            services.close()
        return 0

    import uvicorn

    token = None
    if services.version_run is not None:
        from plantkg.core.actions import write_token
        token = write_token(settings, services.version_run.operator)
    try:
        uvicorn.run(create_app(services, create_mcp_server(services)), host=settings.server.host, port=settings.server.port, log_level="info")
    finally:
        if token is not None:
            token.unlink(missing_ok=True)
        services.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
